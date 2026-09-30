"""
Pasul 15 of docs/superpowers/plans/2026-09-30-next-steps.md.

Integration test for GET /api/gsc/properties/{id}/decay, seeding real
gsc_page_history rows (Pasul 2).
"""
import unittest
import uuid
from datetime import date, timedelta

from fastapi.testclient import TestClient

from api.models._base import AsyncSessionLocal
from api.models.database import GscPageHistory, GscProperty


def _seed_daily_rows(db, property_id, page, weekly_clicks, start_date, impressions_per_day=100):
    d = start_date
    for week_clicks in weekly_clicks:
        per_day = week_clicks // 7
        remainder = week_clicks - per_day * 7
        for i in range(7):
            clicks = per_day + (1 if i < remainder else 0)
            db.add(GscPageHistory(
                property_id=property_id, page=page, clicks=clicks,
                impressions=impressions_per_day, ctr=(clicks / impressions_per_day if impressions_per_day else None),
                position=5.0, period_start=d.isoformat(), period_end=d.isoformat(), source="api",
            ))
            d += timedelta(days=1)


class TestContentDecayEndpoint(unittest.TestCase):
    def setUp(self):
        import asyncio
        self.property_id = str(uuid.uuid4())

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(GscProperty(id=self.property_id, name="t", site_url="sc-domain:example.com"))
                await db.flush()

                # A page with a real, ongoing 16-week decline.
                _seed_daily_rows(db, self.property_id, "/decaying-page",
                                 [1000 - i * 40 for i in range(16)], date(2026, 1, 1))
                # A healthy, flat page with the same amount of history.
                _seed_daily_rows(db, self.property_id, "/healthy-page",
                                 [500] * 16, date(2026, 1, 1))
                # A page with too little history to say anything.
                _seed_daily_rows(db, self.property_id, "/too-new-page",
                                 [500, 480, 460], date(2026, 4, 1))
                await db.commit()

        asyncio.run(seed())
        from api.main import app
        self.client = TestClient(app)

    def tearDown(self):
        import asyncio

        async def clean():
            async with AsyncSessionLocal() as db:
                prop = await db.get(GscProperty, self.property_id)
                if prop:
                    await db.delete(prop)   # cascades to gsc_page_history
                await db.commit()

        asyncio.run(clean())

    def test_the_decaying_page_is_flagged(self):
        body = self.client.get(f"/api/gsc/properties/{self.property_id}/decay").json()
        by_page = {r["page"]: r for r in body["results"]}
        self.assertTrue(by_page["/decaying-page"]["is_decaying"])

    def test_the_healthy_page_is_not_flagged(self):
        body = self.client.get(f"/api/gsc/properties/{self.property_id}/decay").json()
        by_page = {r["page"]: r for r in body["results"]}
        self.assertFalse(by_page["/healthy-page"]["is_decaying"])

    def test_the_too_new_page_reports_insufficient_history(self):
        body = self.client.get(f"/api/gsc/properties/{self.property_id}/decay").json()
        by_page = {r["page"]: r for r in body["results"]}
        self.assertTrue(by_page["/too-new-page"]["insufficient_history"])
        self.assertIsNone(by_page["/too-new-page"]["is_decaying"])

    def test_decaying_pages_are_sorted_first(self):
        body = self.client.get(f"/api/gsc/properties/{self.property_id}/decay").json()
        self.assertEqual(body["results"][0]["page"], "/decaying-page")

    def test_filtering_by_a_single_page(self):
        body = self.client.get(f"/api/gsc/properties/{self.property_id}/decay",
                               params={"page": "/decaying-page"}).json()
        self.assertEqual(body["pages_checked"], 1)
        self.assertEqual(body["results"][0]["page"], "/decaying-page")

    def test_unknown_property_is_404(self):
        resp = self.client.get(f"/api/gsc/properties/{uuid.uuid4()}/decay")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
