"""
Etapa 7 of the consolidation (docs/CONSOLIDATION_PLAN.md): found while
verifying the new SEO/GEO/AEO scorecard (tests/test_seo_geo_aeo_scorecard.py)
against real production data -- api/routes/pages/audit_views.py's
site_health() view raised jinja2.exceptions.UndefinedError on every real
call (site_health.html references total_cost_usd and each audit summary's
cost_usd, neither of which the route ever computed or passed). Unrelated to
the scorecard itself; a pre-existing bug that just happened to block
verifying it. This pins both site_health() and page_view() actually
rendering (status 200), using unittest.mock.MagicMock() for `request` since
these are plain HTML page routes with no dependency on real request state.
"""
import unittest
from unittest.mock import MagicMock

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.audit import Audit, AuditResult
from api.models.infra import CostRecord
from api.routes.pages.audit_views import page_view, site_health

TEST_WEBSITE = "https://test-render-site.example.com"
TEST_PAGE_URL = "test-render-site.example.com_page1"


class TestAuditViewsRender(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with AsyncSessionLocal() as db:
            audit = Audit(
                id="test-render-audit-id", website=TEST_WEBSITE, audit_type="SEO_AUDIT",
                status="completed", provider="anthropic", model="claude-test",
            )
            db.add(audit)
            await db.flush()
            db.add(AuditResult(
                audit_id=audit.id, page_url=TEST_PAGE_URL, filename="page1.txt",
                score=72, classification="good",
            ))
            db.add(CostRecord(
                audit_id=audit.id, source="audit", website=TEST_WEBSITE,
                provider="anthropic", model="claude-test",
                input_tokens=100, output_tokens=50, estimated_cost_usd=0.01,
            ))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            audits = (await db.execute(
                select(Audit).where(Audit.website == TEST_WEBSITE)
            )).scalars().all()
            for a in audits:
                await db.delete(a)  # cascades to AuditResult
            costs = (await db.execute(
                select(CostRecord).where(CostRecord.website == TEST_WEBSITE)
            )).scalars().all()
            for c in costs:
                await db.delete(c)
            await db.commit()

    async def test_site_health_renders_with_cost_fields(self):
        async with AsyncSessionLocal() as db:
            result = await site_health(MagicMock(), TEST_WEBSITE, db)
        self.assertEqual(result.status_code, 200)
        body = result.body.decode("utf-8")
        self.assertIn("SEO / GEO / AEO Scorecard", body)

    async def test_page_view_renders(self):
        async with AsyncSessionLocal() as db:
            result = await page_view(MagicMock(), TEST_PAGE_URL, db)
        self.assertEqual(result.status_code, 200)
        body = result.body.decode("utf-8")
        self.assertIn("SEO / GEO / AEO Scorecard", body)


if __name__ == "__main__":
    unittest.main()
