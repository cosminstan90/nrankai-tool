"""
Etapa 1 of docs/IMPROVEMENTS_PLAN.md: gsc_page_history / gsc_query_history
give GSC data a real time axis. gsc_page_rows / gsc_query_rows have no date
column at all, and every sync (CSV upload or OAuth API sync) deletes and
replaces them, so there was exactly one flat snapshot per property -- no way
to tell whether a page's traffic changed since last month, and every month
not captured is lost for good once GSC's 16-month API window rolls past it.

These are additive tables, not columns bolted onto the existing ones: 8 call
sites across the app filter GscPageRow/GscQueryRow by property_id alone,
several doing func.avg/func.sum or order_by(clicks.desc()), all assuming
exactly one row per page/query. Turning those tables into a time series
would silently corrupt every one of those aggregates.

This test drives the real /api/gsc/properties/{id}/upload endpoint through a
minimal app (just the gsc router, not api.main.app's full lifespan/workers)
and checks three things end to end:
  1. two uploads with different period_start/period_end accumulate as two
     distinct history rows, while GscPageRow (current snapshot) still holds
     only the latest -- proving the additive design holds under the real
     route, not just in isolation
  2. re-uploading the same period_start/period_end updates that history row
     rather than duplicating it (the whole point of the unique constraint
     plus upsert -- re-syncing today should not multiply rows forever)
  3. the /history endpoint returns the accumulated series in order, and the
     no-arg aggregate form sums correctly across pages
"""
import asyncio
import io
import unittest
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.models._base import AsyncSessionLocal, DATABASE_PATH
from api.models.database import GscProperty, GscPageRow, GscPageHistory, GscQueryHistory
from api.routes.gsc import router as gsc_router
from api.routes.gsc.oauth_sync import upsert_gsc_history

_app = FastAPI()
_app.include_router(gsc_router)
_client = TestClient(_app)

_CSV = (
    b"Top pages,Clicks,Impressions,CTR,Position\n"
    b"https://example.com/a,10,100,10.00%,3.5\n"
    b"https://example.com/b,5,50,10.00%,7.2\n"
)
_CSV_UPDATED = (
    b"Top pages,Clicks,Impressions,CTR,Position\n"
    b"https://example.com/a,20,150,13.33%,2.8\n"
    b"https://example.com/b,8,60,13.33%,6.0\n"
)


def _upload(property_id, csv_bytes, period_start=None, period_end=None):
    data = {}
    if period_start:
        data["period_start"] = period_start
    if period_end:
        data["period_end"] = period_end
    return _client.post(
        f"/api/gsc/properties/{property_id}/upload",
        files={"file": ("pages.csv", io.BytesIO(csv_bytes), "text/csv")},
        data=data,
    )


class TestGscHistoryAccumulates(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with AsyncSessionLocal() as db:
            prop = GscProperty(name="test-gsc-history-property", site_url="sc-domain:history-test.example.com")
            db.add(prop)
            await db.commit()
            await db.refresh(prop)
            self.property_id = prop.id

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            prop = await db.get(GscProperty, self.property_id)
            if prop:
                await db.delete(prop)  # cascades to GscPageRow/GscPageHistory
                await db.commit()

    def test_two_uploads_different_periods_accumulate_in_history(self):
        r1 = _upload(self.property_id, _CSV, "2026-06-01", "2026-06-30")
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r1.json()["period"], {"start": "2026-06-01", "end": "2026-06-30", "source": "user"})

        r2 = _upload(self.property_id, _CSV_UPDATED, "2026-07-01", "2026-07-31")
        self.assertEqual(r2.status_code, 200, r2.text)

        async def _check():
            async with AsyncSessionLocal() as db:
                current = (await db.execute(
                    select(GscPageRow).where(GscPageRow.property_id == self.property_id,
                                              GscPageRow.page == "https://example.com/a")
                )).scalars().all()
                # current snapshot: exactly one row, holding the LATEST upload's numbers
                self.assertEqual(len(current), 1)
                self.assertEqual(current[0].clicks, 20)

                history = (await db.execute(
                    select(GscPageHistory)
                    .where(GscPageHistory.property_id == self.property_id,
                           GscPageHistory.page == "https://example.com/a")
                    .order_by(GscPageHistory.period_start)
                )).scalars().all()
                # history: BOTH periods present, neither erased by the other
                self.assertEqual(len(history), 2)
                self.assertEqual(history[0].period_start, "2026-06-01")
                self.assertEqual(history[0].clicks, 10)
                self.assertEqual(history[1].period_start, "2026-07-01")
                self.assertEqual(history[1].clicks, 20)

        asyncio.get_event_loop().run_until_complete(_check())

    def test_reuploading_same_period_updates_not_duplicates(self):
        r1 = _upload(self.property_id, _CSV, "2026-06-01", "2026-06-30")
        self.assertEqual(r1.status_code, 200, r1.text)
        r2 = _upload(self.property_id, _CSV_UPDATED, "2026-06-01", "2026-06-30")
        self.assertEqual(r2.status_code, 200, r2.text)

        async def _check():
            async with AsyncSessionLocal() as db:
                history = (await db.execute(
                    select(GscPageHistory)
                    .where(GscPageHistory.property_id == self.property_id,
                           GscPageHistory.page == "https://example.com/a")
                )).scalars().all()
                self.assertEqual(len(history), 1)          # updated, not duplicated
                self.assertEqual(history[0].clicks, 20)     # holds the SECOND upload's numbers

        asyncio.get_event_loop().run_until_complete(_check())

    def test_missing_period_defaults_to_today_and_flags_assumed(self):
        r = _upload(self.property_id, _CSV)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(body["period"], {"start": today, "end": today, "source": "assumed"})

    def test_history_endpoint_returns_accumulated_series_in_order(self):
        _upload(self.property_id, _CSV, "2026-06-01", "2026-06-30")
        _upload(self.property_id, _CSV_UPDATED, "2026-07-01", "2026-07-31")

        r = _client.get(
            f"/api/gsc/properties/{self.property_id}/history",
            params={"entity": "page", "key": "https://example.com/a", "days": 3650},
        )
        self.assertEqual(r.status_code, 200, r.text)
        points = r.json()["points"]
        self.assertEqual([p["period_start"] for p in points], ["2026-06-01", "2026-07-01"])
        self.assertEqual([p["clicks"] for p in points], [10, 20])

    def test_history_aggregate_sums_across_pages(self):
        _upload(self.property_id, _CSV, "2026-06-01", "2026-06-30")

        r = _client.get(
            f"/api/gsc/properties/{self.property_id}/history",
            params={"entity": "page", "days": 3650},
        )
        self.assertEqual(r.status_code, 200, r.text)
        points = r.json()["points"]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["clicks"], 15)         # 10 (a) + 5 (b)
        self.assertEqual(points[0]["impressions"], 150)   # 100 + 50


class TestUpsertGscHistoryApiPath(unittest.IsolatedAsyncioTestCase):
    """
    Covers the OAuth API sync path directly, without mocking
    google-api-python-client: upsert_gsc_history only needs rows shaped like
    the Search Console API's {"keys": [key, date], "clicks": ...} response,
    which is exactly what dimensions=[dimension, "date"] returns in
    oauth_sync.py's sync_property. The CSV path above already exercises the
    same gsc_page_history/gsc_query_history tables end to end through the
    real route; this covers the other producer of rows into those tables.
    """

    async def asyncSetUp(self):
        async with AsyncSessionLocal() as db:
            prop = GscProperty(name="test-gsc-history-api-property", site_url="sc-domain:history-api-test.example.com")
            db.add(prop)
            await db.commit()
            await db.refresh(prop)
            self.property_id = prop.id

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            prop = await db.get(GscProperty, self.property_id)
            if prop:
                await db.delete(prop)
                await db.commit()

    async def test_same_day_resynced_updates_not_duplicates(self):
        day1_rows = [
            {"keys": ["https://example.com/a", "2026-08-01"], "clicks": 3, "impressions": 30, "ctr": 0.1, "position": 4.0},
        ]
        upsert_gsc_history(DATABASE_PATH, self.property_id, [], day1_rows)

        # Google re-served the SAME day with revised numbers (this genuinely
        # happens -- GSC data settles over a few days) -- must update in
        # place, not accumulate a second row for 2026-08-01.
        day1_revised = [
            {"keys": ["https://example.com/a", "2026-08-01"], "clicks": 5, "impressions": 40, "ctr": 0.125, "position": 3.5},
        ]
        upsert_gsc_history(DATABASE_PATH, self.property_id, [], day1_revised)

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(GscPageHistory).where(GscPageHistory.property_id == self.property_id,
                                              GscPageHistory.page == "https://example.com/a")
            )).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].period_start, "2026-08-01")
        self.assertEqual(rows[0].period_end, "2026-08-01")
        self.assertEqual(rows[0].clicks, 5)

    async def test_multiple_distinct_days_all_persist(self):
        rows = [
            {"keys": ["query one", "2026-08-01"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 5.0},
            {"keys": ["query one", "2026-08-02"], "clicks": 2, "impressions": 20, "ctr": 0.1, "position": 4.5},
            {"keys": ["query one", "2026-08-03"], "clicks": 3, "impressions": 30, "ctr": 0.1, "position": 4.0},
        ]
        upsert_gsc_history(DATABASE_PATH, self.property_id, rows, [])

        async with AsyncSessionLocal() as db:
            history = (await db.execute(
                select(GscQueryHistory)
                .where(GscQueryHistory.property_id == self.property_id, GscQueryHistory.query == "query one")
                .order_by(GscQueryHistory.period_start)
            )).scalars().all()
        self.assertEqual(len(history), 3)
        self.assertEqual([h.clicks for h in history], [1, 2, 3])
        self.assertEqual([h.period_start for h in history], ["2026-08-01", "2026-08-02", "2026-08-03"])


if __name__ == "__main__":
    unittest.main()
