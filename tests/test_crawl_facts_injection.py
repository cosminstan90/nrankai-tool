"""
Etapa 5 of docs/IMPROVEMENTS_PLAN.md -- wiring crawl facts into the analyzer.

Covers the filename -> URL mapping specifically. Crawl facts are keyed by URL
but the analyzer works from filenames, so if this mapping is wrong every
lookup silently misses and every page renders "no crawl data" -- the feature
would appear to work while delivering nothing.

The mapping is built from the crawl's own URLs rather than from the scrape
state file, because on real data that file covered only 157 of 545 scraped
pages. See load_crawl_url_map's docstring.
"""
import unittest
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CrawlPage, SiteCrawl
from core.crawl_facts import load_crawl_url_map
from core.web_scraper import safe_filename_stem


class TestSafeFilenameStem(unittest.TestCase):
    """
    The rule the whole mapping rests on. It was extracted out of
    core/web_scraper.py's scrape loop, so these pin that the extraction did
    not change behaviour.
    """

    def test_strips_scheme_and_sanitises_separators(self):
        self.assertEqual(safe_filename_stem("https://ing.ro/companii-mari"), "ing.ro_companii-mari")

    def test_matches_what_the_analyzer_looks_up(self):
        """
        The scraper writes '<stem>.html' and conversion produces '<stem>.txt',
        so both reduce to the same stem -- the assumption the lookup rests on.
        """
        import os
        stem = safe_filename_stem("https://ing.ro/companii-mari")
        self.assertEqual(os.path.splitext(f"{stem}.txt")[0], stem)
        self.assertEqual(os.path.splitext(f"{stem}.html")[0], stem)

    def test_empty_path_does_not_produce_an_empty_filename(self):
        self.assertTrue(safe_filename_stem("https://"))

    def test_long_urls_are_truncated_to_150_chars(self):
        long_url = "https://example.com/" + ("a" * 400)
        self.assertEqual(len(safe_filename_stem(long_url)), 150)


class TestLoadCrawlUrlMap(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.website = f"https://t{uuid.uuid4().hex[:8]}.example"
        self.crawl_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(SiteCrawl(id=self.crawl_id, website=self.website, status="completed",
                             completed_at=datetime.now(timezone.utc)))
            for path in ("/", "/pricing", "/about-us"):
                db.add(CrawlPage(id=str(uuid.uuid4()), crawl_id=self.crawl_id,
                                 url=f"{self.website}{path}", crawl_depth=1))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            crawl = (await db.execute(
                select(SiteCrawl).where(SiteCrawl.id == self.crawl_id)
            )).scalar_one_or_none()
            if crawl:
                await db.delete(crawl)
                await db.commit()

    async def test_every_crawled_page_gets_a_mapping(self):
        """
        Coverage is the whole point of building this from the crawl instead of
        the scrape state file.
        """
        url_map = await load_crawl_url_map(self.website)
        self.assertEqual(len(url_map), 3)
        self.assertEqual(set(url_map.values()),
                         {f"{self.website}/", f"{self.website}/pricing", f"{self.website}/about-us"})

    async def test_keys_are_the_stems_the_analyzer_will_look_up(self):
        url_map = await load_crawl_url_map(self.website)
        expected_stem = safe_filename_stem(f"{self.website}/pricing")
        self.assertEqual(url_map[expected_stem], f"{self.website}/pricing")

    async def test_uncrawled_site_returns_empty(self):
        self.assertEqual(await load_crawl_url_map("https://never-crawled.example"), {})


if __name__ == "__main__":
    unittest.main()
