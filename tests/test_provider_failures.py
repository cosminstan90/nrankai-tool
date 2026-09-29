"""
A provider that never answered must not be charted as 0%.

Found in the real ing.ro tracker: across all five scans, Claude and Perplexity
answered 0 of 5 queries each (every response empty -- Perplexity's key is
rejected with a 401), yet the trend chart drew both as a flat 0% line, which
reads as "they never cite ing.ro". ChatGPT, by contrast, answered all 5 and
genuinely never cited it; that 0 is real and must stay 0.

On the google_aio side the opposite distinction matters: a SERP with no AI
Overview is a measurement ("not shown"), not a failure.
"""
import json
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CitationScan, CitationTracker
from api.routes.visibility import _provider_point
from core.serp_client import SerpResult


class TestProviderPoint(unittest.TestCase):
    def test_legacy_scan_where_a_provider_never_answered_is_a_gap(self):
        """The real Claude/Perplexity rows: queries=0 and no failure counter yet."""
        b = {"claude": {"queries": 0, "citations": 0, "citation_rate": 0}}
        self.assertIsNone(_provider_point(b, "claude", "citation_rate"))

    def test_answered_but_never_cited_is_a_real_zero(self):
        """The real ChatGPT rows: 5 answers, 0 citations."""
        b = {"chatgpt": {"queries": 5, "citations": 0, "citation_rate": 0}}
        self.assertEqual(_provider_point(b, "chatgpt", "citation_rate"), 0)

    def test_all_attempts_failed_is_a_gap(self):
        b = {"perplexity": {"queries": 0, "failed": 5, "citation_rate": 0}}
        self.assertIsNone(_provider_point(b, "perplexity", "citation_rate"))

    def test_no_ai_overview_anywhere_is_a_measured_zero(self):
        b = {"google_aio": {"queries": 0, "failed": 0, "citation_rate": 0}}
        self.assertEqual(_provider_point(b, "google_aio", "citation_rate"), 0)

    def test_provider_absent_or_errored_is_a_gap(self):
        self.assertIsNone(_provider_point({}, "google_aio", "citation_rate"))
        self.assertIsNone(_provider_point({"google_aio": {"queries": 3, "error": "x"}}, "google_aio", "citation_rate"))


class TestScanRecordsFailures(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tracker_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(CitationTracker(
                id=self.tracker_id, name="failure test", website="ing.ro",
                url_patterns=json.dumps(["ing.ro"]), tracking_queries=json.dumps(["q1", "q2"]),
                providers_config=json.dumps({"perplexity": True, "google_aio": True}),
            ))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            t = (await db.execute(select(CitationTracker).where(CitationTracker.id == self.tracker_id))).scalar_one_or_none()
            if t:
                await db.delete(t)
                await db.commit()

    async def test_failed_llm_and_no_overview_serp_are_recorded_differently(self):
        from api.routes.visibility import _run_visibility_scan

        serp_without_aio = SerpResult(keyword="q", location_code=2642, language_code="ro", depth=20)
        with patch("api.routes.visibility._query_provider", AsyncMock(return_value=("", 0, 0, "sonar"))), \
             patch("core.serp_client.dfs_configured", return_value=True), \
             patch("core.serp_client.fetch_serp", AsyncMock(return_value=serp_without_aio)), \
             patch("api.routes.visibility.asyncio.sleep", AsyncMock()):
            await _run_visibility_scan(self.tracker_id)

        async with AsyncSessionLocal() as db:
            scan = (await db.execute(select(CitationScan).where(CitationScan.tracker_id == self.tracker_id))).scalar_one()

        breakdown = json.loads(scan.provider_breakdown)
        # Perplexity answered nothing: two failures, charted as a gap.
        self.assertEqual(breakdown["perplexity"]["failed"], 2)
        self.assertIsNone(_provider_point(breakdown, "perplexity", "citation_rate"))
        # google_aio got real SERPs without an overview: no failures, a real 0.
        self.assertEqual(breakdown["google_aio"]["failed"], 0)
        self.assertEqual(_provider_point(breakdown, "google_aio", "citation_rate"), 0)

        errors = {r["providers"]["google_aio"]["error"] for r in json.loads(scan.results_json)}
        self.assertEqual(errors, {"No AI Overview shown"})


if __name__ == "__main__":
    unittest.main()
