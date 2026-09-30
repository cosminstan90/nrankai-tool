"""
Automatic backup for analyzer.db.

Pasul 6 of docs/superpowers/plans/2026-09-30-next-steps.md: the database (48MB
on 2026-09-30) had no automatic backup -- the last one in
D:\\Projects\\_geo_tool_backups\\ was from 2026-09-04. It now holds time series
that can't be reconstructed if lost: GSC history, page snapshots, SERP
positions.

analyzer.db is in WAL mode. Never back it up by copying the file -- that
misses whatever is still sitting in the -wal file. This uses sqlite3's own
.backup() API (src.backup(dst)), which is WAL-safe by construction: it reads
through SQLite's own page cache, the same one that reconciles -wal with the
main file.

Every backup is verified immediately after being written (PRAGMA
integrity_check + the audits table is queryable) -- an unverified backup file
doesn't count as a backup, it's just a file that might restore.

Layout:
  <backup_root>/daily/analyzer_YYYYMMDD.db    -- every run, 7 kept
  <backup_root>/weekly/analyzer_YYYYMMDD.db   -- Sunday runs, 4 kept
  <backup_root>/last_run.json                 -- read by the health panel (Pasul 7)

This script does NOT schedule itself. Windows Task Scheduler is persistent
system configuration; scripts/backup_db.py --print-schtasks prints the exact
command to register it, for the user to run themselves.

    python scripts/backup_db.py                        # run a backup now
    python scripts/backup_db.py --db path.db --root dir # target another database/location
    python scripts/backup_db.py --print-schtasks        # print (don't run) the schtasks command
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "api", "data", "analyzer.db")
DEFAULT_BACKUP_ROOT = r"D:\Projects\_geo_tool_backups"

DAILY_KEEP = 7
WEEKLY_KEEP = 4
_NAME_RE = re.compile(r"^analyzer_(\d{8})\.db$")


def create_backup(src_path: str, dest_path: str) -> None:
    """WAL-safe backup via SQLite's own .backup() API -- never a file copy."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def verify_backup(path: str, sanity_table: str = "audits") -> tuple:
    """
    (ok, detail). ok requires PRAGMA integrity_check == 'ok' and sanity_table
    to be queryable. Doesn't require sanity_table to be non-empty -- a fresh
    install with zero audits is a valid backup, not a corrupt one; the CLI
    runner below warns (not fails) on a zero count instead.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"could not open backup: {exc}"
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            return False, f"integrity_check failed: {integrity}"
        count = conn.execute(f"SELECT count(*) FROM {sanity_table}").fetchone()[0]
        return True, f"integrity_check ok, {sanity_table}={count}"
    except sqlite3.Error as exc:
        return False, f"sanity query failed: {exc}"
    finally:
        conn.close()


def rotate_backups(directory: str, keep_n: int) -> list:
    """
    Keep the newest keep_n analyzer_YYYYMMDD.db files in directory, delete the
    rest. Sorts by the date IN THE FILENAME, not mtime (a restored or copied
    file can have a newer mtime than its name implies). Never touches a file
    that doesn't match the naming pattern -- anything else in the directory
    is left alone. Returns the paths deleted.
    """
    if not os.path.isdir(directory):
        return []
    dated = []
    for name in os.listdir(directory):
        m = _NAME_RE.match(name)
        if m:
            dated.append((m.group(1), os.path.join(directory, name)))
    dated.sort(key=lambda t: t[0], reverse=True)  # newest first, YYYYMMDD sorts lexically
    deleted = []
    for _, path in dated[keep_n:]:
        os.remove(path)
        deleted.append(path)
    return deleted


def _write_status(backup_root: str, result: dict) -> None:
    status_path = os.path.join(backup_root, "last_run.json")
    with open(status_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)


def run_backup(src_path: str = DEFAULT_DB, backup_root: str = DEFAULT_BACKUP_ROOT,
              now=None, offsite_dir: str = None) -> dict:
    """Run one backup pass. Never raises -- failures are reported in the result dict."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d")
    daily_dir = os.path.join(backup_root, "daily")
    weekly_dir = os.path.join(backup_root, "weekly")

    result = {
        "ran_at": now.isoformat(), "ok": False,
        "daily_path": None, "daily_verified": False, "daily_detail": None,
        "weekly_path": None, "weekly_verified": None,
        "rotated_daily": [], "rotated_weekly": [],
        "offsite_path": None, "offsite_error": None,
    }

    daily_path = os.path.join(daily_dir, f"analyzer_{stamp}.db")
    try:
        create_backup(src_path, daily_path)
    except Exception as exc:
        result["daily_detail"] = f"backup failed: {exc}"
        _write_status(backup_root, result)
        return result

    ok, detail = verify_backup(daily_path)
    result.update(daily_path=daily_path, daily_verified=ok, daily_detail=detail, ok=ok)

    if not ok:
        # Keep the bad file for inspection; don't rotate or promote a backup
        # that didn't pass verification.
        _write_status(backup_root, result)
        return result

    if now.weekday() == 6:  # Sunday
        weekly_path = os.path.join(weekly_dir, f"analyzer_{stamp}.db")
        try:
            create_backup(src_path, weekly_path)
            w_ok, _ = verify_backup(weekly_path)
        except Exception:
            w_ok = False
        result.update(weekly_path=weekly_path, weekly_verified=w_ok)

    result["rotated_daily"] = rotate_backups(daily_dir, DAILY_KEEP)
    result["rotated_weekly"] = rotate_backups(weekly_dir, WEEKLY_KEEP)

    offsite_dir = offsite_dir or os.environ.get("GEO_TOOL_BACKUP_OFFSITE")
    if offsite_dir:
        try:
            os.makedirs(offsite_dir, exist_ok=True)
            dest = os.path.join(offsite_dir, os.path.basename(daily_path))
            shutil.copy2(daily_path, dest)
            result["offsite_path"] = dest
        except Exception as exc:
            result["offsite_error"] = str(exc)

    _write_status(backup_root, result)
    return result


def _schtasks_command(script_path: str, hour: int = 3) -> str:
    python_exe = sys.executable
    return (
        f'schtasks /Create /TN "GeoToolDbBackup" /TR "\\"{python_exe}\\" \\"{script_path}\\"" '
        f'/SC DAILY /ST {hour:02d}:00 /F'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to analyzer.db")
    parser.add_argument("--root", default=DEFAULT_BACKUP_ROOT, help="Backup root directory")
    parser.add_argument("--offsite", default=None, help="Also copy the daily backup here")
    parser.add_argument("--print-schtasks", action="store_true",
                        help="Print (don't run) the Windows Task Scheduler command to register this script")
    args = parser.parse_args()

    if args.print_schtasks:
        print(_schtasks_command(os.path.abspath(__file__)))
        return

    result = run_backup(args.db, args.root, offsite_dir=args.offsite)
    print(json.dumps(result, indent=2))
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
