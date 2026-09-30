"""
Pasul 12 of docs/superpowers/plans/2026-09-30-next-steps.md.

core.js_visibility.backfill_js_visibility, with the sitemap fetch and the
bot-UA HTTP fetch both mocked -- no real network, no real site touched.
"""
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.js_visibility import backfill_js_visibility, raw_text_sidecar_path
from core.web_scraper import SitemapEntry, safe_filename_stem


class TestBackfillJsVisibility(unittest.IsolatedAsyncioTestCase):
    def _write_html(self, html_dir: Path, url: str, body: str = "x" * 200) -> Path:
        path = html_dir / f"{safe_filename_stem(url)}.html"
        path.write_text(f"<html><body><p>{body}</p></body></html>", encoding="utf-8")
        return path

    async def test_only_pages_without_a_sidecar_are_measured(self):
        with self._tmp_html_dir() as html_dir:
            url_a, url_b = "https://example.test/a", "https://example.test/b"
            path_a = self._write_html(html_dir, url_a)
            path_b = self._write_html(html_dir, url_b)
            # b already measured -- must be skipped
            from core.js_visibility import write_js_visibility_facts
            write_js_visibility_facts(str(path_b), {"measured": True, "text_visibility_ratio": 1.0,
                                                     "missing_elements": [], "rendered_word_count": 1,
                                                     "raw_word_count": 1, "bot_status_code": 200,
                                                     "bot_blocked_or_errored": False, "bot_fetch_error": None})

            fetch_calls = []

            async def _fake_fetch_as_bot(url, user_agent=None):
                fetch_calls.append(url)
                from core.js_visibility import BotFetchResult
                return BotFetchResult(status_code=200, html="<html><body>" + "y" * 200 + "</body></html>")

            with patch("core.web_scraper.fetch_sitemap_urls",
                      return_value=[SitemapEntry(url=url_a), SitemapEntry(url=url_b)]), \
                 patch("core.js_visibility.fetch_as_bot", AsyncMock(side_effect=_fake_fetch_as_bot)):
                result = await backfill_js_visibility(str(html_dir), "https://example.test/sitemap.xml")

            self.assertEqual(fetch_calls, [url_a])   # b skipped, already has a sidecar
            self.assertEqual(result["candidates"], 1)
            self.assertEqual(result["measured"], 1)
            self.assertTrue(Path(raw_text_sidecar_path(str(path_a))).exists())

    async def test_a_page_with_no_stored_html_is_not_a_candidate(self):
        with self._tmp_html_dir() as html_dir:
            url = "https://example.test/never-scraped"
            # No HTML file written for this URL at all.
            with patch("core.web_scraper.fetch_sitemap_urls", return_value=[SitemapEntry(url=url)]), \
                 patch("core.js_visibility.fetch_as_bot", AsyncMock()):
                result = await backfill_js_visibility(str(html_dir), "https://example.test/sitemap.xml")
            self.assertEqual(result["candidates"], 0)
            self.assertEqual(result["measured"], 0)

    async def test_pages_beyond_the_cap_are_reported_not_measured(self):
        with self._tmp_html_dir() as html_dir:
            urls = [f"https://example.test/p{i}" for i in range(5)]
            for u in urls:
                self._write_html(html_dir, u)

            async def _fake_fetch_as_bot(url, user_agent=None):
                from core.js_visibility import BotFetchResult
                return BotFetchResult(status_code=200, html="<html><body>" + "y" * 200 + "</body></html>")

            with patch("core.web_scraper.fetch_sitemap_urls", return_value=[SitemapEntry(url=u) for u in urls]), \
                 patch("core.js_visibility.fetch_as_bot", AsyncMock(side_effect=_fake_fetch_as_bot)):
                result = await backfill_js_visibility(str(html_dir), "https://example.test/sitemap.xml", max_pages=2)

            self.assertEqual(result["candidates"], 5)
            self.assertEqual(result["measured"], 2)
            self.assertEqual(result["skipped_over_cap"], 3)

    async def test_a_failed_fetch_is_counted_not_raised(self):
        with self._tmp_html_dir() as html_dir:
            url = "https://example.test/a"
            self._write_html(html_dir, url)

            with patch("core.web_scraper.fetch_sitemap_urls", return_value=[SitemapEntry(url=url)]), \
                 patch("core.js_visibility.fetch_as_bot", AsyncMock(side_effect=RuntimeError("boom"))):
                result = await backfill_js_visibility(str(html_dir), "https://example.test/sitemap.xml")

            self.assertEqual(result["measured"], 0)
            self.assertEqual(result["failed"], 1)

    async def test_the_stored_sidecar_carries_the_page_url(self):
        """
        compare_visibility() itself has no url to attach -- the recommendations
        UI (Pasul 18) needs a real page_url to save a "fix this page" action
        against, so backfill_js_visibility stamps it on before writing.
        """
        with self._tmp_html_dir() as html_dir:
            url = "https://example.test/a"
            path = self._write_html(html_dir, url)

            async def _fake_fetch_as_bot(url, user_agent=None):
                from core.js_visibility import BotFetchResult
                return BotFetchResult(status_code=200, html="<html><body>" + "y" * 200 + "</body></html>")

            with patch("core.web_scraper.fetch_sitemap_urls", return_value=[SitemapEntry(url=url)]), \
                 patch("core.js_visibility.fetch_as_bot", AsyncMock(side_effect=_fake_fetch_as_bot)):
                await backfill_js_visibility(str(html_dir), "https://example.test/sitemap.xml")

            with open(raw_text_sidecar_path(str(path)), encoding="utf-8") as fh:
                facts = json.load(fh)
            self.assertEqual(facts["url"], url)

    class _tmp_html_dir:
        def __enter__(self):
            import tempfile
            self._tmp = tempfile.TemporaryDirectory()
            return Path(self._tmp.name)

        def __exit__(self, *exc):
            self._tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
