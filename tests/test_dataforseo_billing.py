"""
Running out of DataForSEO credit must be visible, not silent.

Before this, an exhausted balance and a genuinely empty result came back the
same way: "no AI Overview", no ranking, SerpIQ verdicts computed on an empty
SERP. The account had $0.89 left when this was written. The billing codes are
DataForSEO's own, from /v3/appendix/errors.
"""
import asyncio
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CitationScan, CitationTracker, SerpRankObservation
from core import serp_client
from core.serp_client import DataForSEOBillingError, raise_for_billing

INSUFFICIENT = {"status_code": 20000, "tasks": [
    {"status_code": 40210, "status_message": "Insufficient Funds. Your account's balance is too low to complete this request."}]}


class TestRaiseForBilling(unittest.TestCase):
    def test_task_level_insufficient_funds(self):
        with self.assertRaises(DataForSEOBillingError) as ctx:
            raise_for_billing(INSUFFICIENT)
        self.assertEqual(ctx.exception.code, 40210)

    def test_top_level_payment_required(self):
        with self.assertRaises(DataForSEOBillingError):
            raise_for_billing({"status_code": 40200, "status_message": "Payment Required.", "tasks": []})

    def test_cost_limit(self):
        with self.assertRaises(DataForSEOBillingError):
            raise_for_billing({"tasks": [{"status_code": 40203, "status_message": "The cost limit has been exceeded."}]})

    def test_success_and_transient_balance_check_errors_do_not_raise(self):
        """50001 is a transient server-side failure, not a lack of funds."""
        raise_for_billing({"status_code": 20000, "tasks": [{"status_code": 20000}]})
        raise_for_billing({"status_code": 20000, "tasks": [{"status_code": 50001}]})
        raise_for_billing({"tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]})


class _Resp:
    def __init__(self, body): self._b = body
    def json(self): return self._b


class _Client:
    def __init__(self, body): self._b = body
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **k): return _Resp(self._b)


class TestFetchRawRaises(unittest.IsolatedAsyncioTestCase):
    async def test_billing_refusal_reaches_the_caller_instead_of_none(self):
        with patch.dict("os.environ", {"DATAFORSEO_LOGIN": "x", "DATAFORSEO_PASSWORD": "y"}), \
             patch.object(serp_client.httpx, "AsyncClient", lambda **kw: _Client(INSUFFICIENT)):
            with self.assertRaises(DataForSEOBillingError):
                await serp_client.fetch_raw("q", 2642, "ro")


class TestScanSurfacesBillingError(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tracker_id = str(uuid.uuid4())
        self.queries = ["q1", "q2", "q3"]
        async with AsyncSessionLocal() as db:
            db.add(CitationTracker(
                id=self.tracker_id, name="billing test", website="ing.ro",
                url_patterns=json.dumps(["ing.ro"]), tracking_queries=json.dumps(self.queries),
                providers_config=json.dumps({"google_aio": True}),
            ))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            t = (await db.execute(select(CitationTracker).where(CitationTracker.id == self.tracker_id))).scalar_one_or_none()
            if t:
                await db.delete(t)
                await db.commit()

    async def test_first_refusal_stops_further_calls_and_is_recorded(self):
        from api.routes.visibility import _run_visibility_scan

        fetch = AsyncMock(side_effect=DataForSEOBillingError(40210, "Insufficient Funds."))
        with patch("core.serp_client.dfs_configured", return_value=True), \
             patch("core.serp_client.fetch_serp", fetch), \
             patch("api.routes.visibility.asyncio.sleep", AsyncMock()):
            await _run_visibility_scan(self.tracker_id)

        # One refused call, not one per query.
        self.assertEqual(fetch.await_count, 1)

        async with AsyncSessionLocal() as db:
            scan = (await db.execute(select(CitationScan).where(CitationScan.tracker_id == self.tracker_id))).scalar_one()
            obs = (await db.execute(select(SerpRankObservation).where(SerpRankObservation.tracker_id == self.tracker_id))).scalars().all()

        self.assertEqual(scan.status, "completed")
        self.assertEqual(obs, [])
        breakdown = json.loads(scan.provider_breakdown)
        self.assertIn("40210", breakdown["google_aio"]["error"])
        per_query = [r["providers"]["google_aio"]["error"] for r in json.loads(scan.results_json)]
        self.assertTrue(all("40210" in e for e in per_query), per_query)


class TestChartsDoNotDrawMissingDataAsZero(unittest.TestCase):
    def setUp(self):
        self.tracker_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(CitationTracker(
                    id=self.tracker_id, name="chart test", website="ing.ro",
                    url_patterns=json.dumps(["ing.ro"]), tracking_queries="[]",
                    providers_config=json.dumps({"chatgpt": True, "google_aio": True}),
                ))
                await db.flush()
                breakdowns = [
                    {"chatgpt": {"citation_rate": 20, "mention_rate": 40, "queries": 5}},              # before google_aio existed
                    {"chatgpt": {"citation_rate": 30, "mention_rate": 50, "queries": 5},
                     "google_aio": {"citation_rate": 0, "mention_rate": 0, "queries": 0,
                                    "error": "DataForSEO 40210: Insufficient Funds."}},              # out of credit
                    {"chatgpt": {"citation_rate": 25, "mention_rate": 45, "queries": 5},
                     "google_aio": {"citation_rate": 0, "mention_rate": 0, "queries": 5}},           # measured, nothing cited
                ]
                for i, b in enumerate(breakdowns):
                    db.add(CitationScan(
                        id=str(uuid.uuid4()), tracker_id=self.tracker_id, status="completed",
                        provider_breakdown=json.dumps(b), citation_rate=10, visibility_score=10,
                        created_at=now - timedelta(days=3 - i), completed_at=now - timedelta(days=3 - i),
                    ))
                await db.commit()

        asyncio.run(seed())
        from api.main import app
        self.client = TestClient(app)

    def tearDown(self):
        async def clean():
            async with AsyncSessionLocal() as db:
                t = (await db.execute(select(CitationTracker).where(CitationTracker.id == self.tracker_id))).scalar_one_or_none()
                if t:
                    await db.delete(t)
                    await db.commit()
        asyncio.run(clean())

    def _series(self, body, label_contains):
        return next(d["data"] for d in body["chart_data"]["datasets"] if label_contains in d["label"].lower())

    def test_citation_trend_gaps_unmeasured_and_failed_scans(self):
        body = self.client.get(f"/api/citations/trackers/{self.tracker_id}/trend").json()
        self.assertEqual(self._series(body, "google_aio"), [None, None, 0])
        self.assertEqual(self._series(body, "chatgpt"), [20, 30, 25])

    def test_geo_trend_keeps_every_provider_aligned_with_the_dates(self):
        """A provider enabled later used to get a shorter list and drift onto the wrong dates."""
        # This endpoint returns {labels, datasets} at the top level -- its own
        # long-standing contract, unlike /api/citations/.../trend.
        body = self.client.get(f"/api/geo-monitor/projects/{self.tracker_id}/trend").json()
        labels = body["labels"]
        for d in body["datasets"]:
            self.assertEqual(len(d["data"]), len(labels), d["label"])
        aio = next(d["data"] for d in body["datasets"] if "google_aio" in d["label"].lower())
        self.assertEqual(aio, [None, None, 0])

    def test_rankings_endpoint_reports_the_last_scan_error(self):
        # The most recent scan here succeeded, so no error is reported ...
        body = self.client.get(f"/api/citations/trackers/{self.tracker_id}/rankings").json()
        self.assertIsNone(body["data_source_error"])


class TestRankingsReportsCurrentError(unittest.TestCase):
    def test_error_from_the_latest_scan_is_surfaced(self):
        tracker_id = str(uuid.uuid4())

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(CitationTracker(id=tracker_id, name="t", website="ing.ro", url_patterns="[]",
                                       tracking_queries="[]", providers_config=json.dumps({"google_aio": True})))
                await db.flush()
                db.add(CitationScan(id=str(uuid.uuid4()), tracker_id=tracker_id, status="completed",
                                    completed_at=datetime.now(timezone.utc),
                                    provider_breakdown=json.dumps({"google_aio": {"error": "DataForSEO 40210: Insufficient Funds."}})))
                await db.commit()

        async def clean():
            async with AsyncSessionLocal() as db:
                t = (await db.execute(select(CitationTracker).where(CitationTracker.id == tracker_id))).scalar_one_or_none()
                if t:
                    await db.delete(t)
                    await db.commit()

        asyncio.run(seed())
        try:
            from api.main import app
            body = TestClient(app).get(f"/api/citations/trackers/{tracker_id}/rankings").json()
            self.assertIn("40210", body["data_source_error"])
        finally:
            asyncio.run(clean())


class TestSerpIqReportsBillingInsteadOfAFakeVerdict(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_returns_the_billing_error(self):
        """Before, an empty SERP was scored as if Google had returned nothing."""
        from app.modules.serpiq.services.orchestrator import SerpIQOrchestrator

        with patch.dict("os.environ", {"DATAFORSEO_LOGIN": "x", "DATAFORSEO_PASSWORD": "y"}), \
             patch("core.serp_client.fetch_raw", AsyncMock(side_effect=DataForSEOBillingError(40210, "Insufficient Funds."))):
            result = await SerpIQOrchestrator().run_snapshot(
                input_type="keyword", input_value="credit ipotecar",
                location_code=2642, language_code="ro", generate_brief=False, user_id=None,
            )
        self.assertIn("error", result)
        self.assertIn("40210", result["error"])


if __name__ == "__main__":
    unittest.main()
