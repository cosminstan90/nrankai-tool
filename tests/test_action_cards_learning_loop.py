"""
Pasul 18 of docs/superpowers/plans/2026-09-30-next-steps.md.

Integration tests for the three new action_cards endpoints: saving a
pasii-12-17 recommendation with no audit_id, marking it applied, and the
learning report. Uses the real ActionCard/GscPageHistory/GscProperty models
against the app's real DB session (rows are cleaned up in tearDown), the
same convention tests/test_fanout_coverage_endpoint.py already uses.
"""
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import ActionCard, GscPageHistory, GscProperty

PAGE_URL = "https://action-learning-test.example/page"


class TestSaveRecommendation(unittest.TestCase):
    def setUp(self):
        from api.main import app
        self.client = TestClient(app)
        self.created_ids = []

    def tearDown(self):
        import asyncio

        async def clean():
            async with AsyncSessionLocal() as db:
                for cid in self.created_ids:
                    card = await db.get(ActionCard, cid)
                    if card:
                        await db.delete(card)
                await db.commit()

        asyncio.run(clean())

    def _save(self, **overrides):
        body = {
            "source": "gsc_opportunity",
            "page_url": PAGE_URL,
            "page_title": "Test page",
            "priority": "high",
            "description": "Add FAQ schema to close the striking-distance gap",
            "metric_baseline": {"metric": "gsc_clicks", "value": 12.0, "higher_is_better": True},
        }
        body.update(overrides)
        resp = self.client.post("/api/action-cards/from-recommendation", json=body)
        if resp.status_code == 200:
            self.created_ids.append(resp.json()["id"])
        return resp

    def test_unknown_source_is_rejected(self):
        resp = self._save(source="not-a-real-source")
        self.assertEqual(resp.status_code, 400)

    def test_a_recommendation_with_no_audit_saves_fine(self):
        resp = self._save()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNone(body["audit_id"])
        self.assertEqual(body["source"], "gsc_opportunity")
        self.assertIsNone(body["applied_at"])
        # gsc_clicks is re-measurable, so the value comes from gsc_page_history
        # (none for this URL), not from the client's 12.0.
        self.assertEqual(body["metric_baseline"]["metric"], "gsc_clicks")
        self.assertIsNone(body["metric_baseline"]["value"])

    def test_saving_the_same_recommendation_twice_updates_instead_of_duplicating(self):
        """Same dedup_key -- a re-run whose description numbers changed is still the same recommendation."""
        first = self._save(description="Push q from 7.2 (~40 clicks)", dedup_key="striking:q")
        second = self._save(description="Push q from 6.8 (~45 clicks)", dedup_key="striking:q")
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(second.json()["actions"][0]["text"], "Push q from 6.8 (~45 clicks)")

    def test_different_recommendations_for_the_same_page_are_separate_cards(self):
        """
        Regression: dedup was (source, page_url) only, so every Fan-Out gap
        (all sharing the session's target_url) or every citation finding
        (all sharing own_url) overwrote the previous one -- 3 saves, 1 card.
        """
        ids = {self._save(source="fanout_gap", description=f"Answer q{i}", dedup_key=f"fanout:q{i}").json()["id"]
               for i in range(3)}
        self.assertEqual(len(ids), 3)

    def test_without_a_dedup_key_the_description_is_the_key(self):
        first = self._save(description="Same text")
        second = self._save(description="Same text")
        third = self._save(description="Other text")
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertNotEqual(first.json()["id"], third.json()["id"])

    def test_a_gsc_baseline_is_computed_server_side_not_trusted_from_the_client(self):
        """
        No gsc_page_history for PAGE_URL: the client's value (here one query's
        position) must not be stored -- the report would later compare it
        against a page-level average, which measures something else.
        """
        resp = self._save(metric_baseline={"metric": "gsc_position", "value": 7.2, "higher_is_better": True})
        baseline = resp.json()["metric_baseline"]
        self.assertIsNone(baseline["value"])
        self.assertFalse(baseline["higher_is_better"])   # lower position is better, whatever the client said

    def test_a_non_gsc_baseline_keeps_the_client_value(self):
        resp = self._save(source="fanout_gap",
                          metric_baseline={"metric": "fanout_similarity", "value": 0.41, "higher_is_better": True})
        self.assertEqual(resp.json()["metric_baseline"]["value"], 0.41)

    def test_a_different_source_for_the_same_page_creates_a_separate_card(self):
        first = self._save(source="gsc_opportunity")
        second = self._save(source="decay")
        self.assertNotEqual(first.json()["id"], second.json()["id"])

    def test_listing_by_source_with_no_audit_id_finds_the_card(self):
        saved = self._save(source="fanout_gap")
        body = self.client.get("/api/action-cards", params={"source": "fanout_gap"}).json()
        ids = {c["id"] for c in body["cards"]}
        self.assertIn(saved.json()["id"], ids)
        for card in body["cards"]:
            self.assertEqual(card["source"], "fanout_gap")


class TestMarkApplied(unittest.TestCase):
    def setUp(self):
        from api.main import app
        self.client = TestClient(app)
        resp = self.client.post("/api/action-cards/from-recommendation", json={
            "source": "internal_link", "page_url": PAGE_URL,
            "description": "Add an internal link from /related-page",
        })
        self.card_id = resp.json()["id"]

    def tearDown(self):
        import asyncio

        async def clean():
            async with AsyncSessionLocal() as db:
                card = await db.get(ActionCard, self.card_id)
                if card:
                    await db.delete(card)
                await db.commit()

        asyncio.run(clean())

    def test_marking_applied_sets_applied_at(self):
        resp = self.client.patch(f"/api/action-cards/{self.card_id}/apply")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.json()["applied_at"])

    def test_marking_applied_twice_is_idempotent(self):
        first = self.client.patch(f"/api/action-cards/{self.card_id}/apply").json()
        second = self.client.patch(f"/api/action-cards/{self.card_id}/apply").json()
        self.assertEqual(first["applied_at"], second["applied_at"])

    def test_unknown_card_is_404(self):
        resp = self.client.patch(f"/api/action-cards/{uuid.uuid4()}/apply")
        self.assertEqual(resp.status_code, 404)


class TestLearningReport(unittest.TestCase):
    """
    Seeds enough applied gsc_opportunity cards (with real GscPageHistory rows
    giving each one a measurable "current" clicks value) to clear
    MIN_SAMPLE_FOR_CONCLUSIONS, plus a lone citation_gap card that should stay
    under the threshold and report insufficient_data.
    """
    def setUp(self):
        from api.main import app
        self.client = TestClient(app)
        self.property_id = str(uuid.uuid4())
        self.card_ids = []
        self.page_urls = []

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(GscProperty(id=self.property_id, name="learning-loop-test",
                                    site_url="https://action-learning-report-test.example"))
                await db.flush()

                applied_at = datetime.now(timezone.utc) - timedelta(days=40)
                for i in range(5):
                    url = f"https://action-learning-report-test.example/page-{i}"
                    self.page_urls.append(url)
                    card = ActionCard(
                        id=str(uuid.uuid4()), audit_id=None, page_url=url,
                        source="gsc_opportunity", status="completed", applied_at=applied_at,
                        metric_baseline='{"metric": "gsc_clicks", "value": 5.0, "higher_is_better": true}',
                        actions_json="[]", total_actions=0, completed_actions=0,
                    )
                    db.add(card)
                    self.card_ids.append(card.id)
                    period_start = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
                    db.add(GscPageHistory(
                        property_id=self.property_id, page=url, clicks=20, impressions=200,
                        ctr=0.1, position=5.0, period_start=period_start, period_end=period_start,
                        source="api",
                    ))

                lone_url = "https://action-learning-report-test.example/citation-page"
                self.page_urls.append(lone_url)
                lone = ActionCard(
                    id=str(uuid.uuid4()), audit_id=None, page_url=lone_url,
                    source="citation_gap", status="completed", applied_at=applied_at,
                    metric_baseline='{"metric": "citation_rate", "value": 0.2, "higher_is_better": true}',
                    actions_json="[]", total_actions=0, completed_actions=0,
                )
                db.add(lone)
                self.card_ids.append(lone.id)

                await db.commit()

        import asyncio
        asyncio.run(seed())

    def tearDown(self):
        import asyncio

        async def clean():
            async with AsyncSessionLocal() as db:
                for cid in self.card_ids:
                    card = await db.get(ActionCard, cid)
                    if card:
                        await db.delete(card)
                rows = (await db.execute(
                    select(GscPageHistory).where(GscPageHistory.property_id == self.property_id)
                )).scalars().all()
                for r in rows:
                    await db.delete(r)
                prop = await db.get(GscProperty, self.property_id)
                if prop:
                    await db.delete(prop)
                await db.commit()

        asyncio.run(clean())

    def test_a_source_with_enough_eligible_applied_actions_reports_ok(self):
        body = self.client.get("/api/action-cards/learning-report").json()
        self.assertEqual(body["sources"]["gsc_opportunity"]["status"], "ok")
        self.assertEqual(body["sources"]["gsc_opportunity"]["improved"], 5)

    def test_a_source_below_the_sample_threshold_reports_insufficient_data(self):
        body = self.client.get("/api/action-cards/learning-report").json()
        self.assertEqual(body["sources"]["citation_gap"]["status"], "insufficient_data")


class TestGscCtrRemeasurement(unittest.TestCase):
    """
    gsc_ctr (clicks / impressions over the window, not an average of daily
    ratios) is the metric api/templates/recommendations.html attaches to a
    saved weak-CTR opportunity -- confirms it's actually auto-remeasured,
    not silently stuck at not_yet_remeasured forever.
    """
    def setUp(self):
        from api.main import app
        self.client = TestClient(app)
        self.property_id = str(uuid.uuid4())
        self.card_ids = []
        url = "https://gsc-ctr-remeasure-test.example/page"
        applied_at = datetime.now(timezone.utc) - timedelta(days=40)

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(GscProperty(id=self.property_id, name="ctr-remeasure-test",
                                    site_url="https://gsc-ctr-remeasure-test.example"))
                await db.flush()
                for i in range(5):
                    card = ActionCard(
                        id=str(uuid.uuid4()), audit_id=None, page_url=url,
                        source="gsc_opportunity", status="completed", applied_at=applied_at,
                        metric_baseline='{"metric": "gsc_ctr", "value": 0.01, "higher_is_better": true}',
                        actions_json="[]", total_actions=0, completed_actions=0,
                    )
                    db.add(card)
                    self.card_ids.append(card.id)
                period_start = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
                # clicks/impressions = 10/100 = 0.10 -- well above the 0.01 baseline
                db.add(GscPageHistory(
                    property_id=self.property_id, page=url, clicks=10, impressions=100,
                    ctr=0.1, position=5.0, period_start=period_start, period_end=period_start,
                    source="api",
                ))
                await db.commit()

        import asyncio
        asyncio.run(seed())

    def tearDown(self):
        import asyncio

        async def clean():
            async with AsyncSessionLocal() as db:
                for cid in self.card_ids:
                    card = await db.get(ActionCard, cid)
                    if card:
                        await db.delete(card)
                rows = (await db.execute(
                    select(GscPageHistory).where(GscPageHistory.property_id == self.property_id)
                )).scalars().all()
                for r in rows:
                    await db.delete(r)
                prop = await db.get(GscProperty, self.property_id)
                if prop:
                    await db.delete(prop)
                await db.commit()

        asyncio.run(clean())

    def test_saving_computes_the_baseline_from_the_same_history_the_report_uses(self):
        resp = self.client.post("/api/action-cards/from-recommendation", json={
            "source": "gsc_opportunity", "page_url": "https://gsc-ctr-remeasure-test.example/page",
            "description": "Improve CTR", "dedup_key": "weak_ctr",
            "metric_baseline": {"metric": "gsc_ctr", "value": 0.99},
        })
        self.card_ids.append(resp.json()["id"])
        self.assertAlmostEqual(resp.json()["metric_baseline"]["value"], 0.10)   # 10 clicks / 100 impressions

    def test_gsc_ctr_is_remeasured_from_stored_history(self):
        body = self.client.get("/api/action-cards/learning-report").json()
        self.assertEqual(body["sources"]["gsc_opportunity"]["status"], "ok")
        self.assertEqual(body["sources"]["gsc_opportunity"]["improved"], 5)
        self.assertEqual(body["sources"]["gsc_opportunity"]["not_yet_remeasured"], 0)


if __name__ == "__main__":
    unittest.main()
