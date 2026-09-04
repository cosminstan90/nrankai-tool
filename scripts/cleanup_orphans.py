"""
Delete rows whose foreign-key parent no longer exists.

These accumulated while SQLite FK enforcement was off on the async engine, so
every ON DELETE CASCADE in the schema was inert and deleting a parent left its
children behind. That hole is fixed (see api/models/database.py), but the rows
already stranded are still there, invisible to the app and to every UI listing.

Nothing is hardcoded: violations are discovered with PRAGMA foreign_key_check,
so this works unchanged against the VPS copy, which has its own separate
database with a different set of orphans.

    python scripts/cleanup_orphans.py                 # dry run (default)
    python scripts/cleanup_orphans.py --apply         # delete, after a backup
    python scripts/cleanup_orphans.py --db path.db    # target another database
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
from collections import defaultdict

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "api", "data", "analyzer.db")


def find_violations(conn):
    """(child_table, parent_table) -> [rowid, ...]  via SQLite's own checker."""
    groups = defaultdict(list)
    for child, rowid, parent, _fkid in conn.execute("PRAGMA foreign_key_check"):
        groups[(child, parent)].append(rowid)
    return groups


def snapshot(conn, table, rowids):
    """Full contents of the rows about to go, so a mistake stays recoverable."""
    out = []
    for i in range(0, len(rowids), 500):
        chunk = rowids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        cur = conn.execute(f"SELECT rowid, * FROM {table} WHERE rowid IN ({placeholders})", chunk)
        cols = [d[0] for d in cur.description]
        out.extend(dict(zip(cols, row)) for row in cur.fetchall())
    return out


def backup(db_path):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(os.path.dirname(db_path), f"pre_orphan_cleanup_{ts}.db")
    src = sqlite3.connect(db_path)
    dest = sqlite3.connect(dst)
    with dest:                      # backup API, not a file copy: the DB is in
        src.backup(dest)            # WAL mode and may be open by the server
    dest.close()
    src.close()
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    ap.add_argument("--dump", default=None, help="where to write the JSON of deleted rows")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"no such database: {args.db}")

    conn = sqlite3.connect(args.db)
    groups = find_violations(conn)

    if not groups:
        print(f"{args.db}\nNo orphan rows. Nothing to do.")
        return

    total = sum(len(v) for v in groups.values())
    print(f"{args.db}")
    print(f"{'DRY RUN -- nothing will be deleted' if not args.apply else 'APPLYING'}\n")
    print(f"{'child table':<22} {'parent table':<22} {'orphans':>8}  {'table total':>12}")
    print("-" * 70)
    for (child, parent), rowids in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        tot = conn.execute(f"SELECT COUNT(*) FROM {child}").fetchone()[0]
        note = "  <- empties the table" if len(rowids) == tot else ""
        print(f"{child:<22} {parent:<22} {len(rowids):>8}  {tot:>12}{note}")
    print("-" * 70)
    print(f"{'TOTAL':<45} {total:>8}\n")

    if not args.apply:
        print("Re-run with --apply to delete these rows.")
        return

    dst = backup(args.db)
    print(f"backup written: {dst}")

    dump = {}
    for (child, parent), rowids in groups.items():
        dump[f"{child}->{parent}"] = snapshot(conn, child, rowids)
    dump_path = args.dump or os.path.join(os.path.dirname(args.db), "deleted_orphans.json")
    with open(dump_path, "w", encoding="utf-8") as fh:
        json.dump(dump, fh, indent=2, default=str)
    print(f"rows dumped to:  {dump_path}")

    deleted = 0
    for (child, _parent), rowids in groups.items():
        for i in range(0, len(rowids), 500):
            chunk = rowids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            cur = conn.execute(f"DELETE FROM {child} WHERE rowid IN ({placeholders})", chunk)
            deleted += cur.rowcount
    conn.commit()
    print(f"\ndeleted {deleted} rows")

    remaining = sum(len(v) for v in find_violations(conn).values())
    print(f"orphans remaining: {remaining}")
    print("integrity_check:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    if remaining:
        sys.exit("WARNING: orphans remain -- investigate before trusting this run")


if __name__ == "__main__":
    main()
