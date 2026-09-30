"""
Pasul 2 of docs/superpowers/plans/2026-09-30-next-steps.md.

gsc_page_history / gsc_query_history had 0 rows in the real database: the
sync endpoint fills them only when someone manually triggers a sync, and
nothing else did. Google keeps only the trailing 16 months, so every month
nobody manually syncs is gone for good. This worker backfills them on its own.

GSC returns no rows at all for a day with zero impressions, so "which days
are missing" can't be inferred from what's already stored -- an unfetched day
and a fetched-but-silent day look identical. GscProperty.history_synced_through
(migration 0020) tracks fetch coverage explicitly instead.
"""
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text

from api.models._base import AsyncSessionLocal
from api.models.database import GscProperty
from api.workers import gsc_archive_worker as worker


def _fake_creds():
    return object()  # never inspected; only passed through to the mocked fetch


class TestGscArchiveWorker(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.property_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(GscProperty(
                id=self.property_id, name="test property", site_url="sc-domain:example.com",
                sync_type="api", history_synced_through=None,
            ))
            await db.commit()
        worker.LAST_RUN_STATUS.update({"ran_at": None, "properties": {}, "error": None})

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            prop = await db.get(GscProperty, self.property_id)
            if prop:
                await db.delete(prop)
                await db.commit()
            await db.execute(text("DELETE FROM gsc_query_history WHERE property_id = :p"), {"p": self.property_id})
            await db.execute(text("DELETE FROM gsc_page_history WHERE property_id = :p"), {"p": self.property_id})
            await db.commit()

    async def _history_counts(self):
        async with AsyncSessionLocal() as db:
            q = (await db.execute(text(
                "SELECT count(*) FROM gsc_query_history WHERE property_id = :p"), {"p": self.property_id})).scalar()
            p = (await db.execute(text(
                "SELECT count(*) FROM gsc_page_history WHERE property_id = :p"), {"p": self.property_id})).scalar()
        return q, p

    def _one_row(self, key: str, date: str):
        return {"keys": [key, date], "clicks": 3, "impressions": 10, "ctr": 0.3, "position": 4.5}

    async def test_no_oauth_writes_nothing_and_does_not_raise(self):
        with patch.object(worker, "_get_gsc_credentials", AsyncMock(return_value=None)), \
             patch.object(worker, "fetch_daily_gsc_rows") as fetch:
            await worker.run_archive_once()   # must not raise

        fetch.assert_not_called()
        self.assertEqual(await self._history_counts(), (0, 0))
        self.assertEqual(worker.LAST_RUN_STATUS["error"], "not connected")

    async def test_only_the_missing_window_is_requested(self):
        """A property already synced through recently should only ask for what's new."""
        today = datetime.now(timezone.utc).date()
        synced_through = today - timedelta(days=worker.FRESHNESS_DELAY_DAYS + 5)
        async with AsyncSessionLocal() as db:
            prop = await db.get(GscProperty, self.property_id)
            prop.history_synced_through = synced_through.isoformat()
            await db.commit()

        requested_ranges = []

        def _fake_fetch(creds, site_url, dimension, start_str, end_str):
            requested_ranges.append((dimension, start_str, end_str))
            return [], False

        with patch.object(worker, "_get_gsc_credentials", AsyncMock(return_value=_fake_creds())), \
             patch.object(worker, "fetch_daily_gsc_rows", side_effect=_fake_fetch):
            await worker.run_archive_once()

        expected_start = (synced_through + timedelta(days=1)).isoformat()
        expected_end = (today - timedelta(days=worker.FRESHNESS_DELAY_DAYS)).isoformat()
        self.assertTrue(requested_ranges, "fetch_daily_gsc_rows was never called")
        for dimension, start_str, end_str in requested_ranges:
            self.assertEqual(start_str, expected_start)
            self.assertEqual(end_str, expected_end)

        async with AsyncSessionLocal() as db:
            prop = await db.get(GscProperty, self.property_id)
            self.assertEqual(prop.history_synced_through, expected_end)

    async def test_second_run_does_not_duplicate_rows_and_skips_already_covered_days(self):
        today = datetime.now(timezone.utc).date()
        synced_through = today - timedelta(days=worker.FRESHNESS_DELAY_DAYS + 2)
        async with AsyncSessionLocal() as db:
            prop = await db.get(GscProperty, self.property_id)
            prop.history_synced_through = synced_through.isoformat()
            await db.commit()

        day = (synced_through + timedelta(days=1)).isoformat()
        call_count = {"n": 0}

        def _fake_fetch(creds, site_url, dimension, start_str, end_str):
            call_count["n"] += 1
            key = "some query" if dimension == "query" else "https://example.com/"
            return [self._one_row(key, day)], False

        with patch.object(worker, "_get_gsc_credentials", AsyncMock(return_value=_fake_creds())), \
             patch.object(worker, "fetch_daily_gsc_rows", side_effect=_fake_fetch):
            await worker.run_archive_once()
            first_counts = await self._history_counts()
            first_call_count = call_count["n"]

            await worker.run_archive_once()   # same day, cursor already caught up
            second_counts = await self._history_counts()

        self.assertEqual(first_counts, (1, 1))
        self.assertEqual(second_counts, first_counts, "second run must not duplicate rows")
        self.assertEqual(call_count["n"], first_call_count, "second run must not re-fetch an already-covered day")
        self.assertEqual(worker.LAST_RUN_STATUS["properties"][self.property_id]["note"], "up to date")

    async def test_a_failed_chunk_is_reported_and_does_not_crash_the_run(self):
        with patch.object(worker, "_get_gsc_credentials", AsyncMock(return_value=_fake_creds())), \
             patch.object(worker, "fetch_daily_gsc_rows", side_effect=RuntimeError("quota exceeded")):
            await worker.run_archive_once()   # must not raise

        status = worker.LAST_RUN_STATUS["properties"][self.property_id]
        self.assertFalse(status["ok"])
        self.assertIn("quota exceeded", status["error"])


if __name__ == "__main__":
    unittest.main()
