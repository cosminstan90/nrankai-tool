"""
Pasul 13 of docs/superpowers/plans/2026-09-30-next-steps.md.

Integration tests for the two opportunity endpoints. weak-ctr reads
gsc_page_history (Pasul 2, real accumulated history) -- seeded directly.
striking-distance needs a live GSC dimensions=[page,query] fetch -- mocked,
same reasoning as api/routes/gsc/optimizer.py's cannibalization detector.
"""
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from api.models._base import AsyncSessionLocal
from api.models.database import GscPageHistory, GscProperty


def _pos_ctr_rows():
    """Enough (position, ctr) spread to build a real curve -- mirrors
    tests/test_gsc_opportunities.py's synthetic curve, but as GSC rows."""
    return [(1, 0.30), (2, 0.20), (3, 0.15), (4, 0.10), (5, 0.08), (8, 0.04), (10, 0.03)]


class TestWeakCtrEndpoint(unittest.TestCase):
    def setUp(self):
        import asyncio
        self.property_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        recent_day = (now.date() - timedelta(days=2)).isoformat()

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(GscProperty(id=self.property_id, name="t", site_url="sc-domain:example.com"))
                await db.flush()
                for i, (pos, ctr) in enumerate(_pos_ctr_rows()):
                    db.add(GscPageHistory(
                        property_id=self.property_id, page=f"/curve-page-{i}",
                        clicks=int(1000 * ctr), impressions=1000, ctr=ctr, position=float(pos),
                        period_start=recent_day, period_end=recent_day, source="api",
                    ))
                # The actual opportunity: well below the curve at position ~4.
                db.add(GscPageHistory(
                    property_id=self.property_id, page="/weak-page",
                    clicks=20, impressions=1000, ctr=0.02, position=4.0,
                    period_start=recent_day, period_end=recent_day, source="api",
                ))
                # Outside the 28-day window -- must not count.
                old_day = (now.date() - timedelta(days=200)).isoformat()
                db.add(GscPageHistory(
                    property_id=self.property_id, page="/ancient-page",
                    clicks=5, impressions=2000, ctr=0.0025, position=4.0,
                    period_start=old_day, period_end=old_day, source="api",
                ))
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
                    await db.delete(prop)
                    await db.commit()

        asyncio.run(clean())

    def test_the_weak_page_is_reported_as_an_opportunity(self):
        body = self.client.get(f"/api/gsc/properties/{self.property_id}/opportunities/weak-ctr").json()
        pages = {o["page"] for o in body["opportunities"]}
        self.assertIn("/weak-page", pages)

    def test_a_page_outside_the_window_is_not_included(self):
        body = self.client.get(f"/api/gsc/properties/{self.property_id}/opportunities/weak-ctr").json()
        pages = {o["page"] for o in body["opportunities"]}
        self.assertNotIn("/ancient-page", pages)

    def test_unknown_property_is_404(self):
        resp = self.client.get(f"/api/gsc/properties/{uuid.uuid4()}/opportunities/weak-ctr")
        self.assertEqual(resp.status_code, 404)

    def test_brand_terms_excludes_matching_pages(self):
        # /weak-page has no query attached at the page level in this seed,
        # so brand filtering here only confirms the param is accepted and
        # doesn't 500 -- per-query brand exclusion is exercised for real in
        # the striking-distance test below, where query text exists.
        resp = self.client.get(f"/api/gsc/properties/{self.property_id}/opportunities/weak-ctr",
                               params={"brand_terms": "acme,example"})
        self.assertEqual(resp.status_code, 200)


class TestStrikingDistanceEndpoint(unittest.TestCase):
    def setUp(self):
        import asyncio
        self.property_id = str(uuid.uuid4())

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(GscProperty(id=self.property_id, name="t", site_url="sc-domain:example.com"))
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
                    await db.delete(prop)
                    await db.commit()

        asyncio.run(clean())

    def _mock_gsc_rows(self, extra_pairs):
        rows = [{"keys": [f"/curve-page-{i}", f"generic query {i}"], "clicks": int(1000 * ctr),
                "impressions": 1000, "position": float(pos), "ctr": ctr}
               for i, (pos, ctr) in enumerate(_pos_ctr_rows())]
        rows.extend(extra_pairs)
        return rows

    def _patched(self, rows):
        mock_creds = MagicMock()
        mock_execute = MagicMock()
        mock_execute.execute.return_value = {"rows": rows}
        mock_svc = MagicMock()
        mock_svc.searchanalytics.return_value.query.return_value = mock_execute
        return (
            patch("api.routes.gsc.opportunities._get_gsc_credentials", return_value=mock_creds),
            patch("googleapiclient.discovery.build", return_value=mock_svc),
        )

    def test_a_near_top_pair_is_reported(self):
        rows = self._mock_gsc_rows([
            {"keys": ["/near-top", "credit ipotecar"], "clicks": 40, "impressions": 2000,
             "position": 7.0, "ctr": 0.02},
        ])
        p1, p2 = self._patched(rows)
        with p1, p2:
            body = self.client.get(f"/api/gsc/properties/{self.property_id}/opportunities/striking-distance").json()
        pairs = {(o["page"], o["query"]) for o in body["opportunities"]}
        self.assertIn(("/near-top", "credit ipotecar"), pairs)

    def test_no_credentials_is_a_400_not_a_500(self):
        with patch("api.routes.gsc.opportunities._get_gsc_credentials", return_value=None):
            resp = self.client.get(f"/api/gsc/properties/{self.property_id}/opportunities/striking-distance")
        self.assertEqual(resp.status_code, 400)

    def test_brand_query_is_excluded(self):
        rows = self._mock_gsc_rows([
            {"keys": ["/brand-page", "acme login"], "clicks": 40, "impressions": 2000,
             "position": 7.0, "ctr": 0.02},
        ])
        p1, p2 = self._patched(rows)
        with p1, p2:
            body = self.client.get(
                f"/api/gsc/properties/{self.property_id}/opportunities/striking-distance",
                params={"brand_terms": "acme"},
            ).json()
        pairs = {(o["page"], o["query"]) for o in body["opportunities"]}
        self.assertNotIn(("/brand-page", "acme login"), pairs)


if __name__ == "__main__":
    unittest.main()
