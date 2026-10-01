"""
Three endpoints that 500'd on real data (found by probing every
parameterised GET route against the real database):

- GET /api/audits/{id}/export: the temp file was named from audit.website,
  so "https://..." put ':' and '/' in the path -- 71 of 84 real audits.
- GET /audits/{id}/report: report.html compared average_score >= 85 with
  average_score None -- every single-page audit (64 of 84).
- GET /api/serpiq/snapshots/{id}: assigning to the serp_items relationship
  lazy-loaded the old collection in an async session (MissingGreenlet) --
  every snapshot.
"""
import asyncio
import io
import unittest
import uuid
import zipfile

from fastapi.testclient import TestClient

from api.models._base import AsyncSessionLocal
from api.models.database import Audit, AuditResult
from app.modules.serpiq.models import SiqSerpItem, SiqSnapshot


def run(coro):
    return asyncio.run(coro)


class TestAuditExportAndReport(unittest.TestCase):
    def setUp(self):
        from api.main import app
        self.client = TestClient(app)
        self.audit_id = str(uuid.uuid4())

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(Audit(id=self.audit_id, website="https://export-test.example/some/page",
                             audit_type="SINGLE_GEO_AUDIT", provider="anthropic", model="m",
                             status="completed", average_score=None, pages_analyzed=1))
                await db.flush()
                db.add(AuditResult(audit_id=self.audit_id, page_url="https://export-test.example/some/page",
                                   filename="page.json", score=None, result_json='{"note": "ok"}'))
                await db.commit()
        run(seed())

    def tearDown(self):
        async def clean():
            async with AsyncSessionLocal() as db:
                row = await db.get(Audit, self.audit_id)
                if row:
                    await db.delete(row)
                await db.commit()
        run(clean())

    def test_export_works_for_a_website_saved_with_a_scheme(self):
        resp = self.client.get(f"/api/audits/{self.audit_id}/export")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(resp.content)))   # a real .xlsx
        disposition = resp.headers["content-disposition"]
        self.assertIn("export-test.example_some_page_SINGLE_GEO_AUDIT_", disposition)
        self.assertNotIn("https:", disposition)
        self.assertNotIn("/", disposition.split("filename=")[1])

    def test_report_renders_for_an_audit_with_no_average_score(self):
        resp = self.client.get(f"/audits/{self.audit_id}/report")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("—", resp.text)   # shown as "no score", not crashed or scored red


class TestSerpiqSnapshotDetail(unittest.TestCase):
    def setUp(self):
        from api.main import app
        self.client = TestClient(app)

        async def seed():
            async with AsyncSessionLocal() as db:
                snap = SiqSnapshot(input_type="keyword", input_value="test kw", keyword="test kw")
                db.add(snap)
                await db.flush()
                # Inserted out of order -- the response must come back by position.
                for pos in (3, 1, 2):
                    db.add(SiqSerpItem(snapshot_id=snap.id, position=pos, url=f"https://r{pos}.example"))
                await db.commit()
                return snap.id
        self.snapshot_id = run(seed())

    def tearDown(self):
        async def clean():
            async with AsyncSessionLocal() as db:
                row = await db.get(SiqSnapshot, self.snapshot_id)
                if row:
                    await db.delete(row)
                await db.commit()
        run(clean())

    def test_snapshot_detail_returns_its_items_in_position_order(self):
        resp = self.client.get(f"/api/serpiq/snapshots/{self.snapshot_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([i["position"] for i in resp.json()["serp_items"]], [1, 2, 3])

    def test_unknown_snapshot_is_404(self):
        self.assertEqual(self.client.get("/api/serpiq/snapshots/999999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
