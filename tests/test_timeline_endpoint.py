"""
Pasul 10 of docs/superpowers/plans/2026-09-30-next-steps.md.

Integration tests for GET /api/timeline: seeds one real row per source
(page_snapshots, gsc_page_history, serp_rank_observations, citation_scans)
and confirms they land on the same URL's timeline, and that a source with
nothing for this URL is reported as unavailable rather than causing an error.
"""
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api.models._base import AsyncSessionLocal
from api.models.database import (
    CitationScan, CitationTracker, GscProperty, GscPageHistory,
    PageSnapshot, SerpRankObservation, SnapshotRun,
)

URL = "https://ing.ro/oferta-noua"
NORMALIZED = "ing.ro/oferta-noua"


class TestTimelineEndpoint(unittest.TestCase):
    def setUp(self):
        import asyncio
        self.run1_id = str(uuid.uuid4())
        self.run2_id = str(uuid.uuid4())
        self.property_id = str(uuid.uuid4())
        self.tracker_id = str(uuid.uuid4())
        self.scan_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(SnapshotRun(id=self.run1_id, website="ing.ro", status="completed",
                                   started_at=now - timedelta(days=10), completed_at=now - timedelta(days=10)))
                db.add(SnapshotRun(id=self.run2_id, website="ing.ro", status="completed",
                                   started_at=now, completed_at=now))
                await db.flush()

                db.add(PageSnapshot(
                    id=str(uuid.uuid4()), run_id=self.run1_id, website="ing.ro", url=URL,
                    title="Old title", word_count=500, content_hash="hash-a",
                    captured_at=now - timedelta(days=10),
                ))
                db.add(PageSnapshot(
                    id=str(uuid.uuid4()), run_id=self.run2_id, website="ing.ro", url=URL,
                    title="New title", word_count=520, content_hash="hash-b",
                    captured_at=now,
                ))

                db.add(GscProperty(id=self.property_id, name="ing", site_url="sc-domain:ing.ro"))
                await db.flush()
                db.add(GscPageHistory(
                    property_id=self.property_id, page=URL, clicks=12, impressions=100,
                    ctr=0.12, position=4.5,
                    period_start=(now - timedelta(days=1)).date().isoformat(),
                    period_end=(now - timedelta(days=1)).date().isoformat(), source="api",
                ))

                db.add(CitationTracker(
                    id=self.tracker_id, name="t", website="ing.ro",
                    url_patterns="[]", tracking_queries="[]", providers_config="{}",
                ))
                await db.flush()
                db.add(SerpRankObservation(
                    id=str(uuid.uuid4()), tracker_id=self.tracker_id, query="oferta ing",
                    website="ing.ro", location_code=2642, language_code="ro", depth=20,
                    results_count=10, rank_group=3, rank_absolute=3, ranking_url=URL,
                    observed_at=now,
                ))
                db.add(CitationScan(
                    id=self.scan_id, tracker_id=self.tracker_id, status="completed",
                    completed_at=now, top_cited_urls=json.dumps([{"url": URL, "count": 4}]),
                ))
                await db.commit()

        asyncio.run(seed())
        from api.main import app
        self.client = TestClient(app)

    def tearDown(self):
        import asyncio

        async def clean():
            async with AsyncSessionLocal() as db:
                for model, id_ in ((SnapshotRun, self.run1_id), (SnapshotRun, self.run2_id),
                                   (GscProperty, self.property_id), (CitationTracker, self.tracker_id)):
                    row = await db.get(model, id_)
                    if row:
                        await db.delete(row)
                await db.commit()

        asyncio.run(clean())

    def test_all_four_sources_are_present_for_the_seeded_url(self):
        body = self.client.get("/api/timeline", params={"url": URL}).json()
        self.assertEqual(body["url"], NORMALIZED)
        self.assertTrue(body["gsc"]["available"])
        self.assertTrue(body["serp"]["available"])
        self.assertTrue(body["ai_citations"]["available"])
        self.assertEqual(len(body["changes"]), 1)   # one content change between the two snapshots

    def test_url_variants_all_resolve_to_the_same_timeline(self):
        variant = "http://www.ing.ro/oferta-noua/?utm_source=test"
        body = self.client.get("/api/timeline", params={"url": variant}).json()
        self.assertEqual(body["url"], NORMALIZED)
        self.assertTrue(body["gsc"]["available"])

    def test_change_event_reports_the_diff(self):
        body = self.client.get("/api/timeline", params={"url": URL}).json()
        kinds = {c["kind"] for c in body["changes"][0]["changes"]}
        self.assertIn("title_changed", kinds)

    def test_serp_observation_is_attached_with_its_rank(self):
        body = self.client.get("/api/timeline", params={"url": URL}).json()
        self.assertEqual(body["serp"]["observations"][0]["rank_group"], 3)

    def test_ai_citation_count_is_attached(self):
        body = self.client.get("/api/timeline", params={"url": URL}).json()
        self.assertEqual(body["ai_citations"]["citations"][0]["count"], 4)

    def test_a_url_with_no_data_anywhere_reports_every_source_unavailable(self):
        body = self.client.get("/api/timeline", params={"url": "https://never-seen.example/x"}).json()
        self.assertFalse(body["gsc"]["available"])
        self.assertFalse(body["serp"]["available"])
        self.assertFalse(body["ai_citations"]["available"])
        self.assertEqual(body["changes"], [])

    def test_missing_url_param_is_a_400_not_a_500(self):
        resp = self.client.get("/api/timeline", params={"url": ""})
        self.assertIn(resp.status_code, (400, 422))


if __name__ == "__main__":
    unittest.main()
