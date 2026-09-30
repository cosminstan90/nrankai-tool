"""
Pasul 16 of docs/superpowers/plans/2026-09-30-next-steps.md.

core.citation_fetch, with robots.txt reading and the real HTTP fetch both
mocked -- no real network.
"""
import os
import shutil
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from core.citation_fetch import cache_path, fetch_and_cache

TEST_URL = "https://cited-example.test/article"


class TestFetchAndCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.path = cache_path(TEST_URL)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    async def test_a_first_fetch_downloads_and_caches(self):
        resp = MagicMock()
        resp.text = "<html>cited content</html>"
        resp.raise_for_status = MagicMock()
        with patch("core.citation_fetch._robots_allows", return_value=True), \
             patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
            html = await fetch_and_cache(TEST_URL)

        self.assertEqual(html, "<html>cited content</html>")
        self.assertTrue(os.path.exists(self.path))

    async def test_a_cached_url_is_read_from_disk_not_refetched(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("<html>already cached</html>")

        with patch("httpx.AsyncClient") as mock_client:
            html = await fetch_and_cache(TEST_URL)

        self.assertEqual(html, "<html>already cached</html>")
        mock_client.assert_not_called()

    async def test_robots_disallow_returns_none_without_fetching(self):
        with patch("core.citation_fetch._robots_allows", return_value=False), \
             patch("httpx.AsyncClient") as mock_client:
            html = await fetch_and_cache(TEST_URL)

        self.assertIsNone(html)
        mock_client.assert_not_called()
        self.assertFalse(os.path.exists(self.path))

    async def test_a_fetch_failure_returns_none_not_raised(self):
        import httpx

        with patch("core.citation_fetch._robots_allows", return_value=True), \
             patch("httpx.AsyncClient", side_effect=httpx.HTTPError("boom")):
            html = await fetch_and_cache(TEST_URL)

        self.assertIsNone(html)

    async def test_unreadable_robots_txt_defaults_to_allowed(self):
        from urllib.robotparser import RobotFileParser

        resp = MagicMock()
        resp.text = "<html>ok</html>"
        resp.raise_for_status = MagicMock()
        with patch.object(RobotFileParser, "read", side_effect=Exception("network down")), \
             patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=resp)
            html = await fetch_and_cache(TEST_URL)

        self.assertEqual(html, "<html>ok</html>")


if __name__ == "__main__":
    unittest.main()
