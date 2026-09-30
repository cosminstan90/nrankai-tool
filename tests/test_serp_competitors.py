"""
Pasul 8 of docs/superpowers/plans/2026-09-30-next-steps.md.

serp_rank_observations kept only the tracked site's own rank and a yes/no for
"does the AI Overview cite us", throwing away the rest of an already-paid-for
SERP -- the same pattern the plan criticised at AI Overviews before that was
fixed. This tests that the full organic list and AI Overview references are
now saved and exposed, using the real Romanian SERP fixture
(tests/fixtures/serp/ro_banca.json, 17 organic results, 5 AIO references,
captured 2026-09-29) rather than a synthetic one.
"""
import json
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import (
    CitationTracker, SerpRankObservation, SerpOrganicResult, SerpAioReference,
)
from api.workers.rank_tracking import record_observation
from core.serp_client import parse_serp_response

FIXTURE = Path(__file__).parent / "fixtures" / "serp" / "ro_banca.json"


def _serp(keyword="cea mai buna banca din romania"):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_serp_response(data, keyword, 2642, "ro", 20)


class TestRecordObservationSavesTheFullSerp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tracker_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(CitationTracker(
                id=self.tracker_id, name="t", website="hotnews.ro",
                url_patterns="[]", tracking_queries="[]", providers_config="{}",
            ))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            t = (await db.execute(select(CitationTracker).where(CitationTracker.id == self.tracker_id))).scalar_one_or_none()
            if t:
                await db.delete(t)
                await db.commit()

    async def _observation_id(self):
        async with AsyncSessionLocal() as db:
            obs = (await db.execute(
                select(SerpRankObservation).where(SerpRankObservation.tracker_id == self.tracker_id)
            )).scalar_one()
            return obs.id

    async def test_all_17_organic_results_are_saved(self):
        await record_observation(self.tracker_id, None, "hotnews.ro", "q", _serp())
        obs_id = await self._observation_id()
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(SerpOrganicResult).where(SerpOrganicResult.observation_id == obs_id)
            )).scalars().all()
        self.assertEqual(len(rows), 17)
        # hotnews.ro itself must be among them, at the rank build_observation already asserts elsewhere.
        self.assertIn("hotnews.ro", {r.domain for r in rows})

    async def test_all_5_aio_references_are_saved(self):
        await record_observation(self.tracker_id, None, "hotnews.ro", "q", _serp())
        obs_id = await self._observation_id()
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(SerpAioReference).where(SerpAioReference.observation_id == obs_id)
            )).scalars().all()
        self.assertEqual(len(rows), 5)
        self.assertEqual({r.position for r in rows}, {1, 2, 3, 4, 5})

    async def test_deleting_the_tracker_cascades_through_observation_to_results_and_references(self):
        """Two levels deep: tracker -> observation -> organic_results/aio_references."""
        await record_observation(self.tracker_id, None, "hotnews.ro", "q", _serp())
        obs_id = await self._observation_id()

        async with AsyncSessionLocal() as db:
            t = await db.get(CitationTracker, self.tracker_id)
            await db.delete(t)
            await db.commit()

        async with AsyncSessionLocal() as db:
            self.assertIsNone((await db.execute(
                select(SerpRankObservation).where(SerpRankObservation.id == obs_id)
            )).scalar_one_or_none())
            self.assertEqual((await db.execute(
                select(SerpOrganicResult).where(SerpOrganicResult.observation_id == obs_id)
            )).scalars().all(), [])
            self.assertEqual((await db.execute(
                select(SerpAioReference).where(SerpAioReference.observation_id == obs_id)
            )).scalars().all(), [])

    async def test_a_serp_without_an_ai_overview_saves_zero_references_not_an_error(self):
        no_aio = _serp()
        no_aio.ai_overview = None
        await record_observation(self.tracker_id, None, "hotnews.ro", "q", no_aio)
        obs_id = await self._observation_id()
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(SerpAioReference).where(SerpAioReference.observation_id == obs_id)
            )).scalars().all()
        self.assertEqual(rows, [])


class TestCompetitorsEndpoint(unittest.TestCase):
    def setUp(self):
        import asyncio
        self.tracker_id = str(uuid.uuid4())

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(CitationTracker(
                    id=self.tracker_id, name="t", website="hotnews.ro",
                    url_patterns="[]", tracking_queries="[]", providers_config="{}",
                ))
                await db.commit()
            await record_observation(self.tracker_id, None, "hotnews.ro",
                                     "cea mai buna banca din romania", _serp())

        asyncio.run(seed())
        from api.main import app
        self.client = TestClient(app)

    def tearDown(self):
        import asyncio

        async def clean():
            async with AsyncSessionLocal() as db:
                t = (await db.execute(select(CitationTracker).where(CitationTracker.id == self.tracker_id))).scalar_one_or_none()
                if t:
                    await db.delete(t)
                    await db.commit()

        asyncio.run(clean())

    def test_top10_domains_exclude_the_tracked_site_itself(self):
        body = self.client.get(f"/api/citations/trackers/{self.tracker_id}/competitors").json()
        self.assertEqual(len(body["queries"]), 1)
        domains = {d["domain"] for d in body["queries"][0]["top10_domains"]}
        self.assertNotIn("hotnews.ro", domains)

    def test_top10_domains_only_include_rank_1_through_10(self):
        body = self.client.get(f"/api/citations/trackers/{self.tracker_id}/competitors").json()
        for d in body["queries"][0]["top10_domains"]:
            self.assertLessEqual(d["best_rank"], 10)

    def test_aio_cited_domains_are_present(self):
        body = self.client.get(f"/api/citations/trackers/{self.tracker_id}/competitors").json()
        self.assertGreater(len(body["queries"][0]["aio_cited_domains"]), 0)

    def test_unknown_tracker_is_404(self):
        resp = self.client.get(f"/api/citations/trackers/{uuid.uuid4()}/competitors")
        self.assertEqual(resp.status_code, 404)

    def test_tracker_with_no_observations_returns_an_empty_list(self):
        new_id = str(uuid.uuid4())
        import asyncio

        async def seed_empty():
            async with AsyncSessionLocal() as db:
                db.add(CitationTracker(
                    id=new_id, name="empty", website="example.com",
                    url_patterns="[]", tracking_queries="[]", providers_config="{}",
                ))
                await db.commit()

        async def clean_empty():
            async with AsyncSessionLocal() as db:
                t = await db.get(CitationTracker, new_id)
                if t:
                    await db.delete(t)
                    await db.commit()

        asyncio.run(seed_empty())
        try:
            body = self.client.get(f"/api/citations/trackers/{new_id}/competitors").json()
            self.assertEqual(body["queries"], [])
        finally:
            asyncio.run(clean_empty())


if __name__ == "__main__":
    unittest.main()
