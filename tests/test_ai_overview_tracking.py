"""
Etapa 4 of docs/IMPROVEMENTS_PLAN.md: measure Google AI Overview presence
via DataForSEO, folded into the existing citation/mention tracking system
(api/routes/visibility.py) as a fourth "provider" (google_aio) alongside
chatgpt/claude/perplexity/gemini -- not a second, parallel feature. The plan
explicitly warned against repeating that mistake: citation_tracker.py and
geo_monitor.py used to be the same feature written twice, unified in Etapa 3.

core/ai_overview_client.py's parsing was verified live against DataForSEO's
real API before writing this file (2026-09-04, keyword "how does
photosynthesis work", ~$0.003 total across two calls) -- an exploratory
script first to see the real response shape, then the actual shipped
fetch_ai_overview() function directly, confirming asynchronous=False,
markdown populated, 8 references parsed correctly with real domains. That
happy-path case is NOT re-verified here with a mock (it already has a real
verification), and DataForSEO credentials are genuinely configured in this
environment, so this file mocks httpx.AsyncClient specifically for the paths
that are impractical or wasteful to hit live: no AI Overview for a query
(the overwhelming majority of real queries), a task-level API error, and
DataForSEO not configured at all.
"""
import unittest
from unittest.mock import AsyncMock, patch

import core.ai_overview_client as aio_mod
from api.routes.visibility import _query_provider, CreateTrackerRequest


class _FakeResponse:
    def __init__(self, json_body):
        self._json_body = json_body

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


def _mock_httpx(json_body):
    return patch.object(aio_mod.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(_FakeResponse(json_body)))


_NO_AIO_RESPONSE = {
    "tasks": [{
        "status_code": 20000, "status_message": "Ok.",
        "result": [{"items": [{"type": "organic", "rank_absolute": 1}]}],
    }]
}

_TASK_ERROR_RESPONSE = {
    "tasks": [{"status_code": 40501, "status_message": "Invalid Field.", "result": None}]
}

_AIO_PRESENT_RESPONSE = {
    "tasks": [{
        "status_code": 20000, "status_message": "Ok.",
        "result": [{"items": [{
            "type": "ai_overview",
            "asynchronous_ai_overview": False,
            "markdown": "Some overview text with a [link](https://cited-domain.example/page).",
            "items": [],
            "references": [
                {"type": "ai_overview_reference", "domain": "cited-domain.example",
                 "url": "https://cited-domain.example/page", "title": "Example Page", "source": "Example"},
                {"type": "ai_overview_reference", "domain": "another-domain.example",
                 "url": "https://another-domain.example/x", "title": "Other", "source": "Other Source"},
            ],
        }]}],
    }]
}


class TestFetchAiOverview(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_when_not_configured(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("DATAFORSEO_LOGIN", None)
            os.environ.pop("DATAFORSEO_PASSWORD", None)
            result = await aio_mod.fetch_ai_overview("anything")
        self.assertIsNone(result)

    async def test_returns_none_when_no_ai_overview_present(self):
        with patch.dict("os.environ", {"DATAFORSEO_LOGIN": "x", "DATAFORSEO_PASSWORD": "y"}), \
             _mock_httpx(_NO_AIO_RESPONSE):
            result = await aio_mod.fetch_ai_overview("some rare query")
        self.assertIsNone(result)

    async def test_returns_none_on_task_error(self):
        with patch.dict("os.environ", {"DATAFORSEO_LOGIN": "x", "DATAFORSEO_PASSWORD": "y"}), \
             _mock_httpx(_TASK_ERROR_RESPONSE):
            result = await aio_mod.fetch_ai_overview("bad request")
        self.assertIsNone(result)

    async def test_parses_present_ai_overview_with_top_level_references(self):
        with patch.dict("os.environ", {"DATAFORSEO_LOGIN": "x", "DATAFORSEO_PASSWORD": "y"}), \
             _mock_httpx(_AIO_PRESENT_RESPONSE):
            result = await aio_mod.fetch_ai_overview("photosynthesis")
        self.assertIsNotNone(result)
        self.assertFalse(result["asynchronous"])
        self.assertIn("cited-domain.example", result["markdown"])
        self.assertEqual(len(result["references"]), 2)
        self.assertEqual(result["references"][0]["domain"], "cited-domain.example")
        self.assertEqual(result["references"][1]["domain"], "another-domain.example")


class TestQueryProviderGoogleAio(unittest.IsolatedAsyncioTestCase):
    """
    Exercises _query_provider("google_aio", ...) -- the actual integration
    point into the existing scan engine -- not just the standalone client.
    """

    async def test_not_configured_returns_empty_like_other_providers(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("DATAFORSEO_LOGIN", None)
            os.environ.pop("DATAFORSEO_PASSWORD", None)
            text, in_tok, out_tok, model = await _query_provider("google_aio", "some query")
        self.assertEqual(text, "")
        self.assertEqual(in_tok, 0)
        self.assertEqual(out_tok, 0)

    async def test_no_overview_returns_empty_response(self):
        with patch("core.ai_overview_client.fetch_ai_overview", AsyncMock(return_value=None)), \
             patch.dict("os.environ", {"DATAFORSEO_LOGIN": "x", "DATAFORSEO_PASSWORD": "y"}):
            text, in_tok, out_tok, model = await _query_provider("google_aio", "no overview here")
        self.assertEqual(text, "")
        self.assertEqual(in_tok, 0)
        self.assertEqual(out_tok, 0)

    async def test_present_overview_builds_text_with_cited_sources(self):
        fake_aio = {
            "asynchronous": False,
            "markdown": "Photosynthesis converts light into energy.",
            "references": [
                {"domain": "cited-domain.example", "url": "https://cited-domain.example/page",
                 "title": "Example", "source": "Example"},
            ],
        }
        with patch("core.ai_overview_client.fetch_ai_overview", AsyncMock(return_value=fake_aio)), \
             patch.dict("os.environ", {"DATAFORSEO_LOGIN": "x", "DATAFORSEO_PASSWORD": "y"}):
            text, in_tok, out_tok, model = await _query_provider("google_aio", "photosynthesis")

        self.assertIn("Photosynthesis converts light into energy.", text)
        self.assertIn("Cited sources:", text)
        self.assertIn("https://cited-domain.example/page", text)
        self.assertEqual(in_tok, 0)
        self.assertEqual(out_tok, 0)
        self.assertEqual(model, "dataforseo-serp")

    async def test_returned_text_is_detected_as_a_citation_by_the_existing_pipeline(self):
        """
        The whole point of folding this into _query_provider rather than
        building a parallel system: extract_cited_urls (used unchanged for
        every provider) must correctly find the cited domain in what
        google_aio returns, with zero new parsing code.
        """
        from api.routes.visibility import extract_cited_urls

        fake_aio = {
            "asynchronous": False,
            "markdown": "Some overview text.",
            "references": [
                {"domain": "ing.ro", "url": "https://www.ing.ro/some-page",
                 "title": "ING", "source": "ING"},
            ],
        }
        with patch("core.ai_overview_client.fetch_ai_overview", AsyncMock(return_value=fake_aio)), \
             patch.dict("os.environ", {"DATAFORSEO_LOGIN": "x", "DATAFORSEO_PASSWORD": "y"}):
            text, _, _, _ = await _query_provider("google_aio", "some banking query")

        cited = extract_cited_urls(text, "ing.ro")
        self.assertEqual(len(cited), 1)
        self.assertIn("ing.ro", cited[0])


class TestProvidersConfigValidation(unittest.TestCase):
    def test_google_aio_only_config_is_accepted(self):
        """
        Before this change, validate_providers_config's whitelist was
        {"chatgpt", "claude", "perplexity"} -- a request enabling ONLY
        google_aio would have been rejected with "At least one provider
        must be enabled" despite a valid provider being enabled.
        """
        req = CreateTrackerRequest(
            name="Test Tracker",
            website="example.com",
            url_patterns=["example.com"],
            tracking_queries=["q1", "q2", "q3", "q4", "q5"],
            providers_config={"chatgpt": False, "claude": False, "perplexity": False, "google_aio": True},
        )
        self.assertTrue(req.providers_config["google_aio"])


if __name__ == "__main__":
    unittest.main()
