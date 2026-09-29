"""
Etapa 8 of docs/IMPROVEMENTS_PLAN.md -- Google rank tracking.

Uses the real Romanian SERP in tests/fixtures/serp/ro_banca.json (location
2642, captured 2026-09-29) so rank maths runs against a response DataForSEO
actually returned: an AI Overview on top, a people-also-ask block and image
packs in between, and domains that appear twice.
"""
import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CitationScan, CitationTracker, SerpRankObservation
from api.workers.rank_tracking import build_observation, record_observation
from core.serp_client import parse_serp_response

FIXTURE = Path(__file__).parent / "fixtures" / "serp" / "ro_banca.json"


def _serp(keyword="cea mai buna banca din romania"):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_serp_response(data, keyword, 2642, "ro", 20)


class TestBuildObservation(unittest.TestCase):
    def test_ranked_site_gets_both_positions(self):
        obs = build_observation("t", "s", "hotnews.ro", "q", _serp())
        self.assertEqual(obs["rank_group"], 3)
        self.assertEqual(obs["rank_absolute"], 4)     # the AI Overview sits above it
        self.assertTrue(obs["ranking_url"])

    def test_unranked_site_is_null_with_its_n_recorded(self):
        """
        ing.ro is really not in this SERP. NULL plus results_count reads as
        "not in the top 17" -- never position 0.
        """
        obs = build_observation("t", "s", "ing.ro", "q", _serp())
        self.assertIsNone(obs["rank_group"])
        self.assertIsNone(obs["rank_absolute"])
        self.assertEqual(obs["results_count"], 17)

    def test_records_the_market_actually_used(self):
        obs = build_observation("t", "s", "ing.ro", "q", _serp())
        self.assertEqual(obs["location_code"], 2642)
        self.assertEqual(obs["language_code"], "ro")

    def test_records_ai_overview_presence_and_features(self):
        obs = build_observation("t", "s", "ing.ro", "q", _serp())
        self.assertTrue(obs["aio_present"])
        self.assertEqual(obs["serp_features"][0], "ai_overview")

    def test_aio_citation_is_checked_for_the_tracked_site(self):
        serp = _serp()
        cited = serp.ai_overview["references"][0]["domain"]
        self.assertTrue(build_observation("t", "s", cited, "q", serp)["aio_cites_site"])


class TestRecordObservation(unittest.IsolatedAsyncioTestCase):
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

    async def test_persists_one_row(self):
        await record_observation(self.tracker_id, None, "hotnews.ro", "q", _serp())
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(SerpRankObservation).where(SerpRankObservation.tracker_id == self.tracker_id)
            )).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].rank_group, 3)

    async def test_a_failed_write_does_not_raise(self):
        """It rides on a visibility scan; it must never fail that scan."""
        with patch("api.workers.rank_tracking.AsyncSessionLocal", side_effect=RuntimeError("db down")):
            await record_observation(self.tracker_id, None, "hotnews.ro", "q", _serp())


class TestScanRecordsRankings(unittest.IsolatedAsyncioTestCase):
    """The integration that matters: one SERP call per query, two uses."""

    async def asyncSetUp(self):
        self.tracker_id = str(uuid.uuid4())
        self.queries = ["cea mai buna banca din romania", "credit ipotecar"]
        async with AsyncSessionLocal() as db:
            db.add(CitationTracker(
                id=self.tracker_id, name="rank test", website="hotnews.ro",
                url_patterns=json.dumps(["hotnews.ro"]),
                tracking_queries=json.dumps(self.queries),
                providers_config=json.dumps({"google_aio": True}),
                language="English",   # the real tracker's never-changed default
            ))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            t = (await db.execute(select(CitationTracker).where(CitationTracker.id == self.tracker_id))).scalar_one_or_none()
            if t:
                await db.delete(t)
                await db.commit()

    async def test_scan_makes_one_serp_call_per_query_and_records_a_rank_for_each(self):
        from api.routes.visibility import _run_visibility_scan

        serp = _serp()
        fetch = AsyncMock(return_value=serp)
        with patch("core.serp_client.dfs_configured", return_value=True), \
             patch("core.serp_client.fetch_serp", fetch), \
             patch("api.routes.visibility.asyncio.sleep", AsyncMock()):
            await _run_visibility_scan(self.tracker_id)

        # One call per query -- not one for the AI Overview and another for rankings.
        self.assertEqual(fetch.await_count, len(self.queries))

        # And the market came from the .ro TLD, not from language="English".
        for call in fetch.await_args_list:
            self.assertEqual(call.args[1], 2642)
            self.assertEqual(call.args[2], "ro")

        async with AsyncSessionLocal() as db:
            obs = (await db.execute(
                select(SerpRankObservation).where(SerpRankObservation.tracker_id == self.tracker_id)
            )).scalars().all()
            scan = (await db.execute(
                select(CitationScan).where(CitationScan.tracker_id == self.tracker_id)
            )).scalar_one()

        self.assertEqual(len(obs), len(self.queries))
        self.assertTrue(all(o.rank_group == 3 for o in obs))
        self.assertTrue(all(o.scan_id == scan.id for o in obs))
        self.assertEqual(scan.status, "completed")


class TestRankingsEndpoint(unittest.TestCase):
    def setUp(self):
        import asyncio
        from datetime import datetime, timedelta, timezone

        self.tracker_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(CitationTracker(
                    id=self.tracker_id, name="t", website="ing.ro",
                    url_patterns="[]", tracking_queries="[]",
                    providers_config=json.dumps({"google_aio": True}),
                ))
                await db.flush()

                def obs(query, rank, days_ago):
                    return SerpRankObservation(
                        id=str(uuid.uuid4()), tracker_id=self.tracker_id, query=query,
                        website="ing.ro", location_code=2642, language_code="ro",
                        depth=20, results_count=17, rank_group=rank,
                        rank_absolute=(rank + 1) if rank else None,
                        observed_at=now - timedelta(days=days_ago),
                    )
                db.add_all([
                    obs("improved", 9, 7), obs("improved", 4, 0),        # moved up 5
                    obs("slipped", 2, 7), obs("slipped", 6, 0),          # moved down 4
                    obs("appeared", None, 7), obs("appeared", 11, 0),    # entered the results
                    obs("absent", None, 0),
                ])
                await db.commit()

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

    def test_change_is_positive_when_the_site_moves_up(self):
        body = self.client.get(f"/api/citations/trackers/{self.tracker_id}/rankings").json()
        by_q = {q["query"]: q for q in body["queries"]}
        self.assertEqual(by_q["improved"]["change"], 5)
        self.assertEqual(by_q["slipped"]["change"], -4)

    def test_change_is_none_when_either_side_is_unranked(self):
        """Entering the results is real news, but a number would overstate it."""
        body = self.client.get(f"/api/citations/trackers/{self.tracker_id}/rankings").json()
        by_q = {q["query"]: q for q in body["queries"]}
        self.assertIsNone(by_q["appeared"]["change"])
        self.assertEqual(by_q["appeared"]["latest"]["rank_group"], 11)

    def test_unranked_queries_sort_last(self):
        body = self.client.get(f"/api/citations/trackers/{self.tracker_id}/rankings").json()
        self.assertEqual(body["queries"][-1]["query"], "absent")
        self.assertEqual(body["queries_observed"], 4)
        self.assertEqual(body["queries_ranked"], 3)

    def test_unknown_tracker_is_404(self):
        self.assertEqual(self.client.get("/api/citations/trackers/nope/rankings").status_code, 404)


if __name__ == "__main__":
    unittest.main()
