"""
Etapa 3 of docs/IMPROVEMENTS_PLAN.md: GSC URL Inspection. Everything else in
the gsc/ package calls searchanalytics -- how a URL performs in search. This
is the one endpoint answering a different question: can Google find, crawl,
and index this URL at all. Nothing in the tool could answer that before this.

No PAGESPEED_API_KEY-equivalent is needed here (this uses the existing GSC
OAuth token, not a separate API key), but no Google account is connected in
this environment either, so nothing here makes a real call to Google:
_get_gsc_credentials is patched to return a fake credentials object, and
httpx.AsyncClient is mocked the same way as tests/test_performance_cwv.py.

Three things this exercises end to end that the plan called out explicitly:
  1. an indexed page and a non-indexed page return genuinely different
     stored states (not just "different JSON keys happened to be present")
  2. quota is tracked correctly: a forced re-check of the SAME URL on the
     SAME day still counts as a second quota unit, even though it upserts
     the same url_inspections row rather than adding a new one -- this is
     the exact case a naive "count rows in url_inspections" approach would
     undercount, which is why UrlInspectionQuotaLog exists as a separate
     append-only table
  3. exceeding the configured daily limit returns a handled 429, not a
     crash, and does NOT spend an extra Google API call once refused
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import GscProperty, UrlInspection, UrlInspectionQuotaLog
from api.routes.gsc.url_inspection import router as inspection_router

_app = FastAPI()
_app.include_router(inspection_router)
_client = TestClient(_app)


class _FakeCreds:
    token = "fake-access-token"


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.text = text

    def json(self):
        return self._json_body


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **kw):
        return self._response


def _mock_httpx(response):
    import api.routes.gsc.url_inspection as mod
    return patch.object(mod.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(response))


_INDEXED_RESPONSE = {
    "inspectionResult": {
        "indexStatusResult": {
            "verdict": "PASS",
            "coverageState": "Submitted and indexed",
            "robotsTxtState": "ALLOWED",
            "indexingState": "INDEXING_ALLOWED",
            "pageFetchState": "SUCCESSFUL",
            "googleCanonical": "https://example.com/a",
            "userCanonical": "https://example.com/a",
            "sitemap": ["https://example.com/sitemap.xml"],
            "lastCrawlTime": "2026-08-01T10:00:00Z",
        },
        "mobileUsabilityResult": {"verdict": "PASS"},
        "richResultsResult": {"verdict": "NEUTRAL"},
    }
}

_NOT_INDEXED_RESPONSE = {
    "inspectionResult": {
        "indexStatusResult": {
            "verdict": "FAIL",
            "coverageState": "Crawled - currently not indexed",
            "robotsTxtState": "ALLOWED",
            "indexingState": "INDEXING_ALLOWED",
            "pageFetchState": "SUCCESSFUL",
            "googleCanonical": "https://example.com/b",
            "userCanonical": "https://example.com/b",
            "sitemap": [],
        },
        "mobileUsabilityResult": {"verdict": "NEUTRAL"},
        "richResultsResult": {"verdict": "NEUTRAL"},
    }
}


class TestUrlInspection(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with AsyncSessionLocal() as db:
            prop = GscProperty(name="test-inspection-property", site_url="sc-domain:inspect-test.example.com")
            db.add(prop)
            await db.commit()
            await db.refresh(prop)
            self.property_id = prop.id

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            prop = await db.get(GscProperty, self.property_id)
            if prop:
                await db.delete(prop)  # cascades to url_inspections + quota log
                await db.commit()

    def _post(self, url, force=False, max_age_days=3):
        with patch("api.routes.gsc.url_inspection._get_gsc_credentials", AsyncMock(return_value=_FakeCreds())):
            return _client.post(
                f"/api/gsc/properties/{self.property_id}/inspect",
                json={"url": url, "force": force, "max_age_days": max_age_days},
            )

    def test_indexed_and_not_indexed_pages_return_different_states(self):
        with _mock_httpx(_FakeResponse(200, _INDEXED_RESPONSE)):
            r1 = self._post("https://example.com/a")
        self.assertEqual(r1.status_code, 200, r1.text)
        b1 = r1.json()
        self.assertEqual(b1["verdict"], "PASS")
        self.assertEqual(b1["coverage_state"], "Submitted and indexed")
        self.assertEqual(b1["google_canonical"], "https://example.com/a")

        with _mock_httpx(_FakeResponse(200, _NOT_INDEXED_RESPONSE)):
            r2 = self._post("https://example.com/b")
        self.assertEqual(r2.status_code, 200, r2.text)
        b2 = r2.json()
        self.assertEqual(b2["verdict"], "FAIL")
        self.assertEqual(b2["coverage_state"], "Crawled - currently not indexed")

        self.assertNotEqual(b1["verdict"], b2["verdict"])
        self.assertNotEqual(b1["coverage_state"], b2["coverage_state"])

    def test_cached_result_within_max_age_skips_the_api_call(self):
        with _mock_httpx(_FakeResponse(200, _INDEXED_RESPONSE)):
            self._post("https://example.com/a")

        # No httpx mock installed at all this time -- if the code tried a
        # real network call it would fail loudly (no event loop / real DNS
        # to a fake domain), proving the cache genuinely short-circuits it.
        r2 = self._post("https://example.com/a", force=False)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertTrue(r2.json()["from_cache"])

    def test_forced_recheck_same_day_counts_as_second_quota_unit(self):
        with _mock_httpx(_FakeResponse(200, _INDEXED_RESPONSE)):
            self._post("https://example.com/a")
            self._post("https://example.com/a", force=True)

        async def _check():
            async with AsyncSessionLocal() as db:
                results = (await db.execute(
                    select(UrlInspection).where(UrlInspection.property_id == self.property_id)
                )).scalars().all()
                quota_rows = (await db.execute(
                    select(UrlInspectionQuotaLog).where(UrlInspectionQuotaLog.property_id == self.property_id)
                )).scalars().all()
                # one result row (upserted for today), but TWO quota units spent
                self.assertEqual(len(results), 1)
                self.assertEqual(len(quota_rows), 2)

        import asyncio
        asyncio.get_event_loop().run_until_complete(_check())

    def test_quota_endpoint_reflects_usage(self):
        with _mock_httpx(_FakeResponse(200, _INDEXED_RESPONSE)):
            self._post("https://example.com/a")

        r = _client.get(f"/api/gsc/properties/{self.property_id}/inspect/quota")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["used"], 1)
        self.assertEqual(body["remaining"], body["limit"] - 1)

    def test_exceeding_daily_limit_returns_429_without_calling_google(self):
        today = datetime.now(timezone.utc).date().isoformat()

        async def _fill_quota():
            async with AsyncSessionLocal() as db:
                import api.routes.gsc.url_inspection as mod
                from sqlalchemy import insert
                # bulk insert, not 1900 individual db.add() calls -- this test
                # only cares that the quota-check path refuses correctly, not
                # about exercising the ORM's row-by-row insert path
                await db.execute(insert(UrlInspectionQuotaLog), [
                    {"property_id": self.property_id, "checked_date": today}
                    for _ in range(mod._DAILY_QUOTA_LIMIT)
                ])
                await db.commit()

        import asyncio
        asyncio.get_event_loop().run_until_complete(_fill_quota())

        # Deliberately no httpx mock: if the code called out anyway despite
        # being over quota, this would fail with a real network error
        # instead of the expected handled 429.
        r = self._post("https://example.com/should-not-be-called")
        self.assertEqual(r.status_code, 429, r.text)


if __name__ == "__main__":
    unittest.main()
