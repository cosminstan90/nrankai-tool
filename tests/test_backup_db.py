"""
Pasul 6 of docs/superpowers/plans/2026-09-30-next-steps.md.

analyzer.db had no automatic backup (last one from 2026-09-04) and now holds
time series that can't be reconstructed if lost. This exercises
scripts/backup_db.py against the test suite's own temp database (never the
real analyzer.db) to prove the mechanism: WAL-safe backup, verification,
rotation that only ever touches its own naming pattern.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from api.models._base import DATABASE_PATH

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts import backup_db


class TestBackupDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="backup_test_", dir=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".pytest_tmp"))
        self.root = os.path.join(self.tmp, "backups")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _source_row_count(self, table="audits"):
        conn = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
        try:
            return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_backup_opens_and_has_the_same_rows_as_the_source(self):
        dest = os.path.join(self.tmp, "one.db")
        backup_db.create_backup(DATABASE_PATH, dest)

        self.assertTrue(os.path.exists(dest))
        conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            backup_count = conn.execute("SELECT count(*) FROM audits").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(backup_count, self._source_row_count())

    def test_verify_backup_passes_on_a_good_backup(self):
        dest = os.path.join(self.tmp, "good.db")
        backup_db.create_backup(DATABASE_PATH, dest)
        ok, detail = backup_db.verify_backup(dest)
        self.assertTrue(ok, detail)
        self.assertIn("integrity_check ok", detail)

    def test_verify_backup_fails_on_a_truncated_file(self):
        dest = os.path.join(self.tmp, "bad.db")
        backup_db.create_backup(DATABASE_PATH, dest)
        with open(dest, "r+b") as f:
            f.truncate(200)   # corrupt: chop off most of the file
        ok, detail = backup_db.verify_backup(dest)
        self.assertFalse(ok)

    def test_verify_backup_fails_on_a_missing_sanity_table(self):
        dest = os.path.join(self.tmp, "empty.db")
        conn = sqlite3.connect(dest)
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
        conn.close()
        ok, detail = backup_db.verify_backup(dest, sanity_table="audits")
        self.assertFalse(ok)
        self.assertIn("sanity query failed", detail)

    def test_run_backup_writes_a_verified_daily_backup_and_status_file(self):
        now = datetime(2026, 9, 30, tzinfo=timezone.utc)   # a Wednesday
        result = backup_db.run_backup(DATABASE_PATH, self.root, now=now)

        self.assertTrue(result["ok"], result)
        self.assertTrue(os.path.exists(result["daily_path"]))
        self.assertIsNone(result["weekly_path"])   # not a Sunday

        status_path = os.path.join(self.root, "last_run.json")
        self.assertTrue(os.path.exists(status_path))
        with open(status_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["daily_path"], result["daily_path"])

    def test_run_backup_on_sunday_also_writes_a_weekly_backup(self):
        sunday = datetime(2026, 10, 4, tzinfo=timezone.utc)
        result = backup_db.run_backup(DATABASE_PATH, self.root, now=sunday)

        self.assertTrue(result["ok"], result)
        self.assertIsNotNone(result["weekly_path"])
        self.assertTrue(result["weekly_verified"])
        self.assertTrue(os.path.exists(result["weekly_path"]))

    def test_rotation_keeps_exactly_seven_daily_and_four_weekly(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)   # 30 days spans several Sundays regardless of start day
        for i in range(30):
            backup_db.run_backup(DATABASE_PATH, self.root, now=base + timedelta(days=i))

        daily_dir = os.path.join(self.root, "daily")
        weekly_dir = os.path.join(self.root, "weekly")
        daily_files = [f for f in os.listdir(daily_dir) if backup_db._NAME_RE.match(f)]
        weekly_files = [f for f in os.listdir(weekly_dir) if backup_db._NAME_RE.match(f)]

        self.assertEqual(len(daily_files), backup_db.DAILY_KEEP)
        self.assertEqual(len(weekly_files), backup_db.WEEKLY_KEEP)

        # The kept daily files must be the most recent 7 of the 30 days.
        expected_last_days = {(base + timedelta(days=i)).strftime("%Y%m%d") for i in range(23, 30)}
        kept_days = {backup_db._NAME_RE.match(f).group(1) for f in daily_files}
        self.assertEqual(kept_days, expected_last_days)

    def test_rotation_never_touches_files_outside_its_naming_pattern(self):
        daily_dir = os.path.join(self.root, "daily")
        os.makedirs(daily_dir, exist_ok=True)
        stray = os.path.join(daily_dir, "notes.txt")
        with open(stray, "w") as f:
            f.write("do not delete me")

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(10):
            backup_db.run_backup(DATABASE_PATH, self.root, now=base + timedelta(days=i))

        self.assertTrue(os.path.exists(stray))

    def test_a_failed_backup_is_not_rotated_or_promoted_to_weekly(self):
        result = backup_db.run_backup("/no/such/database.db", self.root,
                                      now=datetime(2026, 10, 4, tzinfo=timezone.utc))
        self.assertFalse(result["ok"])
        self.assertIsNone(result["weekly_path"])
        self.assertEqual(result["rotated_daily"], [])


if __name__ == "__main__":
    unittest.main()
