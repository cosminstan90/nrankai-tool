"""
Etapa 7 of the consolidation (docs/CONSOLIDATION_PLAN.md), second idea: an
explicit SEO/GEO/AEO scorecard instead of one blended composite score.

Verified before building: the audit composite (_compute_composite/
_COMPOSITE_WEIGHTS in api/routes/pages/_shared.py) is used in exactly one
file (api/routes/pages/audit_views.py, two call sites: the per-page and
per-site views) -- not "everywhere" as the plan's wording implied. AEO is
deliberately NOT derived from any of the 18 LLM audit types -- it comes
entirely from real Fan-Out/citation-tracking data (CitationTracker/
CitationScan), reusing the exact mention_rate*0.4 + citation_rate*0.6 blend
already used by api/routes/ai_visibility.py's global summary, scoped here
to one site's tracker(s) instead of every tracker in the account.
"""
import unittest
from datetime import datetime, timezone

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.analytics import GscProperty, GscPageRow
from api.models.content import CitationTracker, CitationScan
from api.routes.pages._shared import (
    _bare_domain, _gsc_position_to_score, _normalize_audit_type, compute_scorecard,
)

WEIGHTS = {
    "SEO_AUDIT": 0.20, "TECHNICAL_SEO": 0.12, "GEO_AUDIT": 0.15,
    "CONTENT_QUALITY": 0.12, "UX_CONTENT": 0.10,
}


class TestBareDomain(unittest.TestCase):
    def test_strips_protocol_and_www(self):
        self.assertEqual(_bare_domain("https://www.example.com/some/path"), "example.com")

    def test_strips_sc_domain_prefix(self):
        self.assertEqual(_bare_domain("sc-domain:example.com"), "example.com")

    def test_bare_domain_passthrough(self):
        self.assertEqual(_bare_domain("example.com"), "example.com")

    def test_empty_input(self):
        self.assertEqual(_bare_domain(""), "")


class TestNormalizeAuditType(unittest.TestCase):
    def test_strips_single_prefix(self):
        # SINGLE_GEO_AUDIT / SINGLE_SEO_AUDIT are real, common audit_type
        # values from POST /api/audits/single (the Instant Audit tool) --
        # 48+ real rows for SINGLE_GEO_AUDIT alone in production. Without
        # this normalisation they'd silently land in neither bucket.
        self.assertEqual(_normalize_audit_type("SINGLE_GEO_AUDIT"), "GEO_AUDIT")
        self.assertEqual(_normalize_audit_type("single_seo_audit"), "SEO_AUDIT")

    def test_god_mode_left_unclassified(self):
        # No single-type equivalent -- shouldn't be rewritten into some
        # other bucket's name.
        self.assertEqual(_normalize_audit_type("SINGLE_PAGE_GOD_MODE"), "SINGLE_PAGE_GOD_MODE")

    def test_regular_type_passthrough(self):
        self.assertEqual(_normalize_audit_type("geo_audit"), "GEO_AUDIT")


class TestGscPositionToScore(unittest.TestCase):
    def test_position_one_is_high(self):
        self.assertEqual(_gsc_position_to_score(1.0), 100)

    def test_position_twenty_plus_is_clamped_low(self):
        self.assertEqual(_gsc_position_to_score(30.0), 0)

    def test_mid_position(self):
        self.assertEqual(_gsc_position_to_score(10.0), 60)


class TestComputeScorecard(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            props = (await db.execute(
                select(GscProperty).where(GscProperty.name == "test-scorecard-property")
            )).scalars().all()
            for p in props:
                await db.delete(p)
            trackers = (await db.execute(
                select(CitationTracker).where(CitationTracker.name == "test-scorecard-tracker")
            )).scalars().all()
            for t in trackers:
                await db.delete(t)
            await db.commit()

    async def test_seo_geo_split_with_no_external_data(self):
        scored_map = {"SEO_AUDIT": 80, "TECHNICAL_SEO": 60, "GEO_AUDIT": 90, "CONTENT_QUALITY": 70}
        async with AsyncSessionLocal() as db:
            result = await compute_scorecard(db, "untracked-site.example.com", scored_map, WEIGHTS)

        # SEO bucket = SEO_AUDIT(80, w.20) + TECHNICAL_SEO(60, w.12) -> weighted avg
        expected_seo = round((80 * 0.20 + 60 * 0.12) / (0.20 + 0.12))
        self.assertEqual(result["seo"]["llm_score"], expected_seo)
        self.assertIsNone(result["seo"]["gsc"])
        self.assertEqual(result["seo"]["score"], expected_seo)  # no GSC -> falls back to LLM-only

        expected_geo = round((90 * 0.15 + 70 * 0.12) / (0.15 + 0.12))
        self.assertEqual(result["geo"]["score"], expected_geo)

        self.assertIsNone(result["aeo"]["score"])  # not tracked -> None, not guessed

    async def test_gsc_signal_blends_into_seo_score(self):
        async with AsyncSessionLocal() as db:
            prop = GscProperty(name="test-scorecard-property", site_url="sc-domain:gsctest.example.com")
            db.add(prop)
            await db.flush()
            db.add(GscPageRow(property_id=prop.id, page="https://gsctest.example.com/x",
                               clicks=10, impressions=100, ctr=0.1, position=5.0))
            await db.commit()

        scored_map = {"SEO_AUDIT": 60}
        async with AsyncSessionLocal() as db:
            result = await compute_scorecard(db, "gsctest.example.com", scored_map, WEIGHTS)

        self.assertIsNotNone(result["seo"]["gsc"])
        self.assertEqual(result["seo"]["gsc"]["avg_position"], 5.0)
        gsc_score = _gsc_position_to_score(5.0)
        expected = round(60 * 0.6 + gsc_score * 0.4)
        self.assertEqual(result["seo"]["score"], expected)

    async def test_gsc_page_filter_scopes_to_one_page(self):
        async with AsyncSessionLocal() as db:
            prop = GscProperty(name="test-scorecard-property", site_url="sc-domain:multipage.example.com")
            db.add(prop)
            await db.flush()
            db.add_all([
                GscPageRow(property_id=prop.id, page="https://multipage.example.com/a",
                           clicks=1, impressions=10, ctr=0.1, position=2.0),
                GscPageRow(property_id=prop.id, page="https://multipage.example.com/b",
                           clicks=1, impressions=10, ctr=0.1, position=20.0),
            ])
            await db.commit()

        async with AsyncSessionLocal() as db:
            result = await compute_scorecard(
                db, "multipage.example.com", {"SEO_AUDIT": 50}, WEIGHTS,
                page_url="https://multipage.example.com/a",
            )
        self.assertEqual(result["seo"]["gsc"]["avg_position"], 2.0)

    async def test_single_prefixed_audit_type_lands_in_correct_bucket(self):
        # Reproduces the real bug found manually testing against bestetic.ro:
        # its only completed audit was SINGLE_GEO_AUDIT, which without
        # normalisation matched neither _SEO_AUDIT_TYPES nor _GEO_AUDIT_TYPES,
        # showing "not tracked" for GEO despite a real score existing.
        scored_map = {"SINGLE_GEO_AUDIT": 35}
        async with AsyncSessionLocal() as db:
            result = await compute_scorecard(db, "untracked-site.example.com", scored_map, WEIGHTS)
        self.assertEqual(result["geo"]["score"], 35)
        self.assertIsNone(result["seo"]["score"])

    async def test_aeo_signal_from_citation_tracker(self):
        async with AsyncSessionLocal() as db:
            tracker = CitationTracker(
                id="test-scorecard-tracker-id", name="test-scorecard-tracker",
                website="aeotest.example.com", url_patterns="[]", tracking_queries="[]",
                providers_config="{}",
            )
            db.add(tracker)
            await db.flush()
            db.add(CitationScan(
                id="test-scorecard-scan-id", tracker_id=tracker.id, status="completed",
                visibility_score=40.0, citation_rate=60.0,
                completed_at=datetime.now(timezone.utc),
            ))
            await db.commit()

        async with AsyncSessionLocal() as db:
            result = await compute_scorecard(db, "aeotest.example.com", {}, WEIGHTS)

        self.assertIsNotNone(result["aeo"]["score"])
        self.assertEqual(result["aeo"]["score"], round(40.0 * 0.4 + 60.0 * 0.6, 1))


if __name__ == "__main__":
    unittest.main()
