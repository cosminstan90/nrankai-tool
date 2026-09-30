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
        self.assertEqual(body["metric_baseline"], {"metric": "gsc_clicks", "value": 12.0, "higher_is_better": True})

    def test_saving_twice_for_the_same_page_updates_instead_of_duplicating(self):
        first = self._save(description="First version")
        second = self._save(description="Updated version")
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(second.json()["actions"][0]["text"], "Updated version")

    def test_a_different_source_for_the_same_page_creates_a_separate_card(self):
        first = self._save(source="gsc_opportunity")
        second = self._save(source="decay")
        self.assertNotEqual(first.json()["id"], second.json()["id"])


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


if __name__ == "__main__":
    unittest.main()
