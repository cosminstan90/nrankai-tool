"""
Pasul 12 of docs/superpowers/plans/2026-09-30-next-steps.md.

Network-facing parts of core.js_visibility, with httpx mocked -- no real
requests. fetch_as_bot must reproduce exactly what GPTBot/ClaudeBot/
PerplexityBot actually do: one request, no retry, no JS.
"""
import unittest
from unittest.mock import AsyncMock, patch

from core.js_visibility import GPTBOT_USER_AGENT, fetch_as_bot, measure_page


class TestFetchAsBot(unittest.IsolatedAsyncioTestCase):
    async def test_a_successful_fetch_returns_the_html_and_status(self):
        resp = AsyncMock()
        resp.status_code = 200
        resp.text = "<html>ok</html>"
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
            result = await fetch_as_bot("https://example.test/page")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.html, "<html>ok</html>")
        self.assertFalse(result.blocked_or_errored)

    async def test_uses_the_gptbot_user_agent_by_default(self):
        resp = AsyncMock()
        resp.status_code = 200
        resp.text = "ok"
        captured = {}

        async def _get(url, headers=None):
            captured["headers"] = headers
            return resp

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = _get
            await fetch_as_bot("https://example.test/page")
        self.assertEqual(captured["headers"]["User-Agent"], GPTBOT_USER_AGENT)

    async def test_a_403_is_reported_not_raised(self):
        resp = AsyncMock()
        resp.status_code = 403
        resp.text = "Forbidden"
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
            result = await fetch_as_bot("https://example.test/page")
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.html, "")   # body not trusted/used on a non-200
        self.assertTrue(result.blocked_or_errored)

    async def test_a_network_error_is_reported_not_raised(self):
        with patch("httpx.AsyncClient", side_effect=RuntimeError("DNS failure")):
            result = await fetch_as_bot("https://example.test/page")
        self.assertIsNone(result.status_code)
        self.assertIn("DNS failure", result.error)
        self.assertTrue(result.blocked_or_errored)


class TestMeasurePage(unittest.IsolatedAsyncioTestCase):
    async def test_combines_a_bot_fetch_with_the_rendered_html(self):
        # extract_content() only keeps body text over 100 chars (its own
        # noise floor) -- padded well past that so this exercises the real
        # extraction path, not its "too short to bother with" fallback.
        rendered = ("<html><body><h1>Title</h1><p>Some real paragraph of actual content here, "
                   "long enough to clear extract_content's own minimum length before it keeps "
                   "any body text at all.</p></body></html>")
        resp = AsyncMock()
        resp.status_code = 200
        resp.text = "<html><body></body></html>"   # bot sees an empty shell
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
            facts = await measure_page("https://example.test/page", rendered)
        self.assertTrue(facts["measured"])
        self.assertLess(facts["text_visibility_ratio"], 1.0)
        self.assertEqual(facts["bot_status_code"], 200)


if __name__ == "__main__":
    unittest.main()
