"""
Pasul 16 of docs/superpowers/plans/2026-09-30-next-steps.md.

Integration test for GET /api/citations/trackers/{id}/citation-comparison:
seeds a real tracker + SerpRankObservation + SerpAioReference + CitationScan
graph, with core.citation_fetch.fetch_and_cache mocked (no real network).
"""
import json
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.models._base import AsyncSessionLocal
from api.models.database import (
    CitationScan, CitationTracker, SerpAioReference, SerpRankObservation,
)

QUERY = "credit ipotecar"
OWN_URL = "https://ing.ro/credit-ipotecar"
CITED_URL_1 = "https://competitor-a.example/ghid-credit"
CITED_URL_2 = "https://competitor-b.example/credit-ipotecar-info"

LONG_PADDING = " ".join(["lorem ipsum"] * 30)


def _own_html():
    return f"<html><body><p>Pagina noastra. {LONG_PADDING}</p></body></html>"


def _cited_html_rich():
    return (
        '<html><head><meta property="article:modified_time" content="2026-01-01T00:00:00Z">'
        '<meta name="author" content="Ana Pop"></head><body>'
        f'<table><tr><td>Rata</td><td>6.5%</td></tr></table>'
        f'<p>Suma maxima 500000 lei. {LONG_PADDING}</p></body></html>'
    )


class TestCitationComparisonEndpoint(unittest.TestCase):
    def setUp(self):
        import asyncio
        self.tracker_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(CitationTracker(
                    id=self.tracker_id, name="t", website="ing.ro",
                    url_patterns=json.dumps(["ing.ro"]), tracking_queries=json.dumps([QUERY]),
                    providers_config=json.dumps({"google_aio": True}),
                ))
                await db.flush()

                obs_id = str(uuid.uuid4())
                db.add(SerpRankObservation(
                    id=obs_id, tracker_id=self.tracker_id, query=QUERY, website="ing.ro",
                    location_code=2642, language_code="ro", depth=20, results_count=10,
                    rank_group=3, rank_absolute=3, ranking_url=OWN_URL, observed_at=now,
                ))
                await db.flush()
                db.add(SerpAioReference(
                    observation_id=obs_id, position=1, domain="competitor-a.example",
                    url=CITED_URL_1, title="Ghid credit",
                ))

                scan_results = [{
                    "query": QUERY, "query_index": 1,
                    "providers": {"anthropic": {"cited": True, "cited_urls": [CITED_URL_2]}},
                }]
                db.add(CitationScan(
                    id=str(uuid.uuid4()), tracker_id=self.tracker_id, status="completed",
                    completed_at=now, results_json=json.dumps(scan_results),
                ))
                await db.commit()

        asyncio.run(seed())
        from api.main import app
        self.client = TestClient(app)

    def tearDown(self):
        import asyncio

        async def clean():
            async with AsyncSessionLocal() as db:
                tracker = await db.get(CitationTracker, self.tracker_id)
                if tracker:
                    await db.delete(tracker)   # cascades to observations -> aio references, and scans
                await db.commit()

        asyncio.run(clean())

    def _fake_fetch(self, html_by_url):
        async def _fetch(url):
            return html_by_url.get(url)
        return AsyncMock(side_effect=_fetch)

    def test_both_cited_urls_are_gathered_from_aio_and_scan_results(self):
        html_by_url = {OWN_URL: _own_html(), CITED_URL_1: _cited_html_rich(), CITED_URL_2: _cited_html_rich()}
        with patch("api.routes.citation_comparison.fetch_and_cache", self._fake_fetch(html_by_url)):
            body = self.client.get("/api/citations/trackers/" + self.tracker_id + "/citation-comparison",
                                   params={"query": QUERY}).json()
        self.assertEqual(body["cited_urls_considered"], 2)
        self.assertEqual(body["own_url"], OWN_URL)

    def test_findings_surface_features_the_own_page_lacks(self):
        html_by_url = {OWN_URL: _own_html(), CITED_URL_1: _cited_html_rich(), CITED_URL_2: _cited_html_rich()}
        with patch("api.routes.citation_comparison.fetch_and_cache", self._fake_fetch(html_by_url)):
            body = self.client.get("/api/citations/trackers/" + self.tracker_id + "/citation-comparison",
                                   params={"query": QUERY}).json()
        features = {f["feature"] for f in body["report"]["findings"]}
        self.assertIn("has_tables", features)

    def test_a_citing_page_that_fails_to_fetch_is_excluded_not_scored_as_lacking(self):
        html_by_url = {OWN_URL: _own_html(), CITED_URL_1: _cited_html_rich(), CITED_URL_2: None}
        with patch("api.routes.citation_comparison.fetch_and_cache", self._fake_fetch(html_by_url)):
            body = self.client.get("/api/citations/trackers/" + self.tracker_id + "/citation-comparison",
                                   params={"query": QUERY}).json()
        self.assertEqual(body["cited_urls_considered"], 1)

    def test_no_recommendation_by_default(self):
        html_by_url = {OWN_URL: _own_html(), CITED_URL_1: _cited_html_rich(), CITED_URL_2: _cited_html_rich()}
        with patch("api.routes.citation_comparison.fetch_and_cache", self._fake_fetch(html_by_url)):
            body = self.client.get("/api/citations/trackers/" + self.tracker_id + "/citation-comparison",
                                   params={"query": QUERY}).json()
        self.assertIsNone(body["recommendation"])

    def test_recommendation_calls_the_llm_with_only_the_feature_table(self):
        html_by_url = {OWN_URL: _own_html(), CITED_URL_1: _cited_html_rich(), CITED_URL_2: _cited_html_rich()}
        captured = {}

        async def _fake_llm(provider, model, system_prompt, user_content, **kw):
            captured["user_content"] = user_content
            return ("Add a table.", 20, 10)

        with patch("api.routes.citation_comparison.fetch_and_cache", self._fake_fetch(html_by_url)), \
             patch("api.utils.llm_json_client.call_llm_for_summary", _fake_llm), \
             patch("api.routes.costs.track_cost", AsyncMock()):
            body = self.client.get("/api/citations/trackers/" + self.tracker_id + "/citation-comparison",
                                   params={"query": QUERY, "with_recommendation": "true"}).json()

        self.assertEqual(body["recommendation"], "Add a table.")
        self.assertNotIn(_cited_html_rich(), captured["user_content"])   # never given raw page content
        self.assertIn("comparison table", captured["user_content"])   # the human-readable label, not the raw feature key

    def test_an_llm_failure_keeps_the_measured_report_and_says_why(self):
        """CLAUDE.md rule 4: the provider call is wrapped -- no 500, report kept, no cost tracked."""
        html_by_url = {OWN_URL: _own_html(), CITED_URL_1: _cited_html_rich(), CITED_URL_2: _cited_html_rich()}

        async def _failing_llm(provider, model, system_prompt, user_content, **kw):
            raise RuntimeError("provider down")

        track = AsyncMock()
        with patch("api.routes.citation_comparison.fetch_and_cache", self._fake_fetch(html_by_url)), \
             patch("api.utils.llm_json_client.call_llm_for_summary", _failing_llm), \
             patch("api.routes.costs.track_cost", track):
            resp = self.client.get("/api/citations/trackers/" + self.tracker_id + "/citation-comparison",
                                   params={"query": QUERY, "with_recommendation": "true"})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNone(body["recommendation"])
        self.assertEqual(body["recommendation_error"], "AI service unavailable")
        self.assertTrue(body["report"]["findings"])
        track.assert_not_called()

    def test_unknown_tracker_is_404(self):
        resp = self.client.get(f"/api/citations/trackers/{uuid.uuid4()}/citation-comparison",
                               params={"query": QUERY})
        self.assertEqual(resp.status_code, 404)

    def test_a_query_never_scanned_reports_no_observed_ranking(self):
        body = self.client.get("/api/citations/trackers/" + self.tracker_id + "/citation-comparison",
                               params={"query": "never tracked query"}).json()
        self.assertIsNone(body["own_url"])
        self.assertIsNone(body["report"])


if __name__ == "__main__":
    unittest.main()
