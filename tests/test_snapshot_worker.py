"""
Etapa 6 of docs/IMPROVEMENTS_PLAN.md -- capturing and comparing snapshot runs.

Uses a temporary directory of HTML rather than a real site directory, so the
tests neither depend on nor disturb the scraped data on disk.
"""
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import PageSnapshot, SnapshotRun
from api.workers.snapshot_worker import capture_snapshot, compare_runs

FIXTURES = Path(__file__).parent / "fixtures" / "pages"


class TestCaptureSnapshot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.website = f"https://t{uuid.uuid4().hex[:8]}.example"
        self.tmp = Path(tempfile.mkdtemp(prefix="snaptest_"))
        shutil.copy(FIXTURES / "credit_v1.html", self.tmp / "example.com_credit.html")
        shutil.copy(FIXTURES / "credit_v1.html", self.tmp / "example.com_alte.html")
        self.run_ids = []

    async def asyncTearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        async with AsyncSessionLocal() as db:
            runs = (await db.execute(
                select(SnapshotRun).where(SnapshotRun.website == self.website)
            )).scalars().all()
            for r in runs:
                await db.delete(r)
            await db.commit()

    async def test_captures_one_row_per_html_file(self):
        run_id = await capture_snapshot(self.website, str(self.tmp))
        self.run_ids.append(run_id)

        async with AsyncSessionLocal() as db:
            run = (await db.execute(
                select(SnapshotRun).where(SnapshotRun.id == run_id)
            )).scalar_one()
            pages = (await db.execute(
                select(PageSnapshot).where(PageSnapshot.run_id == run_id)
            )).scalars().all()

        self.assertEqual(run.status, "completed")
        self.assertEqual(run.pages_captured, 2)
        self.assertEqual(len(pages), 2)
        self.assertTrue(all(p.word_count and p.word_count > 0 for p in pages))

    async def test_empty_directory_completes_rather_than_failing(self):
        empty = Path(tempfile.mkdtemp(prefix="snapempty_"))
        try:
            run_id = await capture_snapshot(self.website, str(empty))
            async with AsyncSessionLocal() as db:
                run = (await db.execute(
                    select(SnapshotRun).where(SnapshotRun.id == run_id)
                )).scalar_one()
            self.assertEqual(run.status, "completed")
            self.assertEqual(run.pages_captured, 0)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    async def test_missing_directory_is_recorded_as_a_failed_run(self):
        """A typo in the path must be visible, not silently produce nothing."""
        with self.assertRaises(FileNotFoundError):
            await capture_snapshot(self.website, str(self.tmp / "does-not-exist"))

        async with AsyncSessionLocal() as db:
            runs = (await db.execute(
                select(SnapshotRun).where(SnapshotRun.website == self.website)
            )).scalars().all()
        self.assertTrue(any(r.status == "failed" for r in runs))


class TestCompareRuns(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.website = f"https://t{uuid.uuid4().hex[:8]}.example"
        self.tmp_a = Path(tempfile.mkdtemp(prefix="snapA_"))
        self.tmp_b = Path(tempfile.mkdtemp(prefix="snapB_"))
        # Same page name in both runs, different content: the client edited it.
        shutil.copy(FIXTURES / "credit_v1.html", self.tmp_a / "example.com_credit.html")
        shutil.copy(FIXTURES / "credit_v2.html", self.tmp_b / "example.com_credit.html")
        # Present in the first run only: the page was removed.
        shutil.copy(FIXTURES / "credit_v1.html", self.tmp_a / "example.com_gone.html")
        # Present in the second run only: the page is new.
        shutil.copy(FIXTURES / "credit_v2.html", self.tmp_b / "example.com_new.html")

        self.run_a = await capture_snapshot(self.website, str(self.tmp_a))
        self.run_b = await capture_snapshot(self.website, str(self.tmp_b))

    async def asyncTearDown(self):
        shutil.rmtree(self.tmp_a, ignore_errors=True)
        shutil.rmtree(self.tmp_b, ignore_errors=True)
        async with AsyncSessionLocal() as db:
            runs = (await db.execute(
                select(SnapshotRun).where(SnapshotRun.website == self.website)
            )).scalars().all()
            for r in runs:
                await db.delete(r)
            await db.commit()

    async def test_reports_changed_pages_with_their_findings(self):
        result = await compare_runs(self.run_a, self.run_b)

        changed = {p["url"]: p for p in result["changed"]}
        self.assertEqual(len(changed), 1)
        page = next(iter(changed.values()))
        kinds = {c["kind"] for c in page["changes"]}
        self.assertIn("h1_changed", kinds)
        self.assertIn("internal_links_removed", kinds)

    async def test_reports_added_and_removed_pages_separately(self):
        """
        A page that disappeared is a different problem from one that was
        edited, and lumping them together hides both.
        """
        result = await compare_runs(self.run_a, self.run_b)

        self.assertEqual(len(result["removed"]), 1)
        self.assertEqual(len(result["added"]), 1)
        self.assertTrue(result["removed"][0].endswith("_gone"))
        self.assertTrue(result["added"][0].endswith("_new"))

    async def test_unchanged_pages_are_counted_not_listed(self):
        """Listing every untouched page would drown the findings."""
        result = await compare_runs(self.run_a, self.run_a)
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["unchanged_count"], 2)

    async def test_hash_short_circuits_pages_that_did_not_change(self):
        result = await compare_runs(self.run_a, self.run_a)
        self.assertEqual(result["summary"]["total"], 0)


class TestCapturesAreSerialized(unittest.IsolatedAsyncioTestCase):
    """
    Captures must not overlap.

    api/models/_base.py builds the engine with poolclass=StaticPool, so every
    session in the process shares ONE physical SQLite connection. Two captures
    running at once over it corrupted a real 545-page run: both stalled at
    exactly 445 rows, one hung in "running" forever and the other reported
    "completed, 545 pages" against 445 stored rows.

    This asserts the lock actually serializes them. It does not assert anything
    about the underlying hazard, which is app-wide and lives in the engine
    configuration -- a test for that was tried first and was too timing
    dependent to be worth keeping: it passed alone and failed inside the full
    suite, which is the kind of test that teaches people to ignore red.
    """

    async def asyncSetUp(self):
        self.website = f"https://t{uuid.uuid4().hex[:8]}.example"
        self.tmp = Path(tempfile.mkdtemp(prefix="snaplock_"))
        for i in range(6):
            shutil.copy(FIXTURES / "credit_v1.html", self.tmp / f"page_{i}.html")

    async def asyncTearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        async with AsyncSessionLocal() as db:
            runs = (await db.execute(
                select(SnapshotRun).where(SnapshotRun.website == self.website)
            )).scalars().all()
            for r in runs:
                await db.delete(r)
            await db.commit()

    async def test_a_second_capture_waits_for_the_first(self):
        import asyncio

        import api.workers.snapshot_worker as worker

        order = []
        original = worker._capture_snapshot_locked

        async def traced(website, source_dir):
            order.append("start")
            await asyncio.sleep(0.05)      # keep the lock held
            run_id = await original(website, source_dir)
            order.append("end")
            return run_id

        worker._capture_snapshot_locked = traced
        try:
            await asyncio.gather(
                worker.capture_snapshot(self.website, str(self.tmp)),
                worker.capture_snapshot(self.website, str(self.tmp)),
            )
        finally:
            worker._capture_snapshot_locked = original

        # Serialized means start,end,start,end -- never two starts in a row.
        self.assertEqual(order, ["start", "end", "start", "end"])

    async def test_both_serialized_captures_store_all_their_rows(self):
        import asyncio

        run_a, run_b = await asyncio.gather(
            capture_snapshot(self.website, str(self.tmp)),
            capture_snapshot(self.website, str(self.tmp)),
        )

        async with AsyncSessionLocal() as db:
            for run_id in (run_a, run_b):
                run = (await db.execute(
                    select(SnapshotRun).where(SnapshotRun.id == run_id)
                )).scalar_one()
                rows = (await db.execute(
                    select(PageSnapshot).where(PageSnapshot.run_id == run_id)
                )).scalars().all()
                self.assertEqual(run.status, "completed")
                self.assertEqual(
                    len(rows), run.pages_captured,
                    f"run claimed {run.pages_captured} pages but {len(rows)} rows persisted",
                )


if __name__ == "__main__":
    unittest.main()
