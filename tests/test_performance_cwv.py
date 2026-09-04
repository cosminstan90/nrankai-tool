"""
Etapa 2 of docs/IMPROVEMENTS_PLAN.md: Core Web Vitals via CrUX (real-user
field data) and PageSpeed Insights (one simulated Lighthouse run). The tool
had zero performance data before this -- no PageSpeed, no CrUX, no
Lighthouse, no LCP/INP/CLS anywhere, despite both being free Google APIs.

No PAGESPEED_API_KEY is configured in this environment (confirmed: unset in
os.environ, and nothing here or in conftest.py sets it), so nothing in this
file makes a real network call to Google -- core/performance_client.py's
httpx calls are mocked at the httpx.AsyncClient level, and the route tests
below patch the fetch functions directly. The one test that deliberately does
NOT mock anything (test_check_returns_503_without_configured_key) exercises
the actual current state of this deployment: hitting /check today returns a
clear 503, not a crash.
"""
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import PerformanceSnapshot
from api.routes.performance import router as performance_router
from core import performance_client

_app = FastAPI()
_app.include_router(performance_router)
_client = TestClient(_app)


class _FakeResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body

    def json(self):
        return self._json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient(...) as an async context manager."""
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        return self._response

    async def get(self, *a, **kw):
        return self._response


def _mock_client(response: _FakeResponse):
    return patch.object(performance_client.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(response))


_CRUX_RECORD_RESPONSE = {
    "record": {
        "key": {"url": "https://example.com/", "formFactor": "PHONE"},
        "metrics": {
            "largest_contentful_paint": {"percentiles": {"p75": 1978}},
            "interaction_to_next_paint": {"percentiles": {"p75": 350}},
            "cumulative_layout_shift": {"percentiles": {"p75": 0.31}},
            "first_contentful_paint": {"percentiles": {"p75": 1500}},
            "experimental_time_to_first_byte": {"percentiles": {"p75": 600}},
        },
        "collectionPeriod": {
            "firstDate": {"year": 2026, "month": 7, "day": 1},
            "lastDate": {"year": 2026, "month": 7, "day": 28},
        },
    }
}

_CRUX_HISTORY_RESPONSE = {
    "record": {
        "metrics": {
            "largest_contentful_paint": {"percentilesTimeseries": {"p75s": [2000, 2100]}},
            "interaction_to_next_paint": {"percentilesTimeseries": {"p75s": [180, 190]}},
            "cumulative_layout_shift": {"percentilesTimeseries": {"p75s": [0.05, 0.06]}},
            "first_contentful_paint": {"percentilesTimeseries": {"p75s": [1400, 1420]}},
            "experimental_time_to_first_byte": {"percentilesTimeseries": {"p75s": [500, 510]}},
        },
        "collectionPeriods": [
            {"firstDate": {"year": 2026, "month": 5, "day": 1}, "lastDate": {"year": 2026, "month": 5, "day": 28}},
            {"firstDate": {"year": 2026, "month": 6, "day": 1}, "lastDate": {"year": 2026, "month": 6, "day": 28}},
        ],
    }
}

_PSI_RESPONSE = {
    "lighthouseResult": {
        "categories": {"performance": {"score": 0.87}},
        "audits": {
            "largest-contentful-paint": {"numericValue": 2100.0},
            "cumulative-layout-shift": {"numericValue": 0.04},
            "total-blocking-time": {"numericValue": 150.0},
            "first-contentful-paint": {"numericValue": 1300.0},
            "speed-index": {"numericValue": 2400.0},
        },
    }
}


class TestFetchCrux(unittest.IsolatedAsyncioTestCase):
    async def test_parses_response_and_computes_ratings(self):
        with patch.dict("os.environ", {"PAGESPEED_API_KEY": "fake-key"}), \
             _mock_client(_FakeResponse(200, _CRUX_RECORD_RESPONSE)):
            result = await performance_client.fetch_crux("https://example.com/")

        self.assertEqual(result["p75"]["lcp"], 1978)
        self.assertEqual(result["p75"]["inp"], 350)
        self.assertEqual(result["p75"]["cls"], 0.31)
        # thresholds: lcp good<=2500 -> good; inp poor>500? no, 350 is between 200-500 -> needs-improvement
        self.assertEqual(result["rating"]["lcp"], "good")
        self.assertEqual(result["rating"]["inp"], "needs-improvement")
        self.assertEqual(result["rating"]["cls"], "poor")       # 0.31 > 0.25
        self.assertEqual(result["period_start"], "2026-07-01")
        self.assertEqual(result["period_end"], "2026-07-28")

    async def test_returns_none_on_404(self):
        with patch.dict("os.environ", {"PAGESPEED_API_KEY": "fake-key"}), \
             _mock_client(_FakeResponse(404, {"error": {"code": 404}})):
            result = await performance_client.fetch_crux("https://no-data-example.com/")
        self.assertIsNone(result)

    async def test_returns_none_when_key_missing(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("PAGESPEED_API_KEY", None)
            result = await performance_client.fetch_crux("https://example.com/")
        self.assertIsNone(result)


class TestFetchCruxHistory(unittest.IsolatedAsyncioTestCase):
    async def test_maps_periods_to_points_by_index(self):
        with patch.dict("os.environ", {"PAGESPEED_API_KEY": "fake-key"}), \
             _mock_client(_FakeResponse(200, _CRUX_HISTORY_RESPONSE)):
            points = await performance_client.fetch_crux_history("https://example.com/")

        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["period_start"], "2026-05-01")
        self.assertEqual(points[0]["p75"]["lcp"], 2000)
        self.assertEqual(points[0]["rating"]["inp"], "good")     # 180 <= 200
        self.assertEqual(points[1]["period_start"], "2026-06-01")
        self.assertEqual(points[1]["p75"]["lcp"], 2100)


class TestFetchPsi(unittest.IsolatedAsyncioTestCase):
    async def test_parses_lighthouse_response(self):
        with patch.dict("os.environ", {"PAGESPEED_API_KEY": "fake-key"}), \
             _mock_client(_FakeResponse(200, _PSI_RESPONSE)):
            result = await performance_client.fetch_psi("https://example.com/")

        self.assertEqual(result["performance_score"], 87)   # 0.87 * 100, rounded
        self.assertEqual(result["lcp"], 2100.0)
        self.assertEqual(result["cls"], 0.04)
        self.assertEqual(result["tbt"], 150.0)


class TestCheckEndpoint(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(PerformanceSnapshot).where(PerformanceSnapshot.url == "https://cwv-test.example.com/")
            )).scalars().all()
            for r in rows:
                await db.delete(r)
            await db.commit()

    def test_returns_503_without_configured_key(self):
        # No mocking here on purpose: PAGESPEED_API_KEY is genuinely unset in
        # this environment right now, and this proves the endpoint degrades
        # to a clear error instead of crashing when Google returns nothing
        # because there's no key to call it with.
        r = _client.post("/api/performance/check", json={"url": "https://cwv-test.example.com/"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("PAGESPEED_API_KEY", r.json()["detail"])

    async def test_check_stores_snapshot_and_history_endpoint_returns_it(self):
        with patch.dict("os.environ", {"PAGESPEED_API_KEY": "fake-key"}), \
             patch("api.routes.performance.fetch_crux", AsyncMock(return_value={
                 "p75": {"lcp": 1978, "inp": 350, "cls": 0.31, "fcp": 1500, "ttfb": 600},
                 "rating": {"lcp": "good", "inp": "needs-improvement", "cls": "poor",
                            "fcp": "good", "ttfb": "needs-improvement"},
                 "period_start": "2026-07-01", "period_end": "2026-07-28",
             })), \
             patch("api.routes.performance.fetch_crux_history", AsyncMock(return_value=[])), \
             patch("api.routes.performance.fetch_psi", AsyncMock(return_value={
                 "performance_score": 87, "lcp": 2100.0, "cls": 0.04, "tbt": 150.0,
                 "fcp": 1300.0, "speed_index": 2400.0,
             })):
            r = _client.post("/api/performance/check", json={"url": "https://cwv-test.example.com/"})

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["field"]["p75"]["lcp"], 1978)
        self.assertEqual(body["field"]["rating"]["cls"], "poor")
        self.assertEqual(body["lab"]["performance_score"], 87)

        r2 = _client.get("/api/performance/history", params={
            "url": "https://cwv-test.example.com/", "source": "crux", "days": 365,
        })
        self.assertEqual(r2.status_code, 200, r2.text)
        points = r2.json()["points"]
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["field"]["lcp"]["p75"], 1978)

    async def test_resyncing_same_period_updates_not_duplicates(self):
        fake_crux = AsyncMock(return_value={
            "p75": {"lcp": 1978, "inp": 350, "cls": 0.31, "fcp": 1500, "ttfb": 600},
            "rating": {"lcp": "good", "inp": "needs-improvement", "cls": "poor",
                       "fcp": "good", "ttfb": "needs-improvement"},
            "period_start": "2026-07-01", "period_end": "2026-07-28",
        })
        with patch.dict("os.environ", {"PAGESPEED_API_KEY": "fake-key"}), \
             patch("api.routes.performance.fetch_crux", fake_crux), \
             patch("api.routes.performance.fetch_crux_history", AsyncMock(return_value=[])), \
             patch("api.routes.performance.fetch_psi", AsyncMock(return_value=None)):
            _client.post("/api/performance/check", json={"url": "https://cwv-test.example.com/"})

            fake_crux.return_value = dict(fake_crux.return_value, p75={
                "lcp": 2200, "inp": 350, "cls": 0.31, "fcp": 1500, "ttfb": 600,
            })
            _client.post("/api/performance/check", json={"url": "https://cwv-test.example.com/"})

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(PerformanceSnapshot).where(
                    PerformanceSnapshot.url == "https://cwv-test.example.com/",
                    PerformanceSnapshot.source == "crux",
                )
            )).scalars().all()
        self.assertEqual(len(rows), 1)           # updated in place, not duplicated
        self.assertEqual(rows[0].p75_lcp, 2200)  # holds the SECOND check's value


if __name__ == "__main__":
    unittest.main()
