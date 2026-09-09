"""
Etapa 5 of docs/IMPROVEMENTS_PLAN.md -- rendering crawl facts for the prompt.

The rendering tests are pure. The lookup tests seed their own throwaway
website so they never collide with real rows, and clean up after themselves.
"""
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CrawlLink, CrawlPage, SiteCrawl
from core.crawl_facts import format_crawl_facts_block, load_page_facts


class TestFormatCrawlFactsBlock(unittest.TestCase):
    def test_no_crawl_data_says_so_instead_of_implying_zero(self):
        """
        The most important case. Absent data must never render as "0 internal
        inlinks": prompts/internal_linking.yaml caps a page at 20/100 when it
        has no body-content links, so a fabricated zero would invent a failing
        score for a page nobody ever measured.
        """
        block = format_crawl_facts_block(None)
        self.assertIn("no crawl data", block.lower())
        self.assertNotIn("ZERO", block)

    def test_reports_content_and_nav_inlinks_separately(self):
        block = format_crawl_facts_block({
            "url": "https://example.com/pricing",
            "content_inlinks": 3, "nav_inlinks": 40, "crawl_depth": 1,
            "outlinks_total": 6, "is_orphan": False,
            "inbound_anchors": ["See our pricing", "pricing plans"],
            "broken_outlinks": [],
        })

        self.assertIn("3", block)
        self.assertIn("40", block)
        self.assertIn("navigation", block.lower())
        self.assertIn("See our pricing", block)

    def test_home_page_with_no_content_inlinks_is_not_called_an_orphan(self):
        """
        Caught by the first real end-to-end run. The database already exempts
        depth-0 pages from orphan status, but this block called the home page
        an orphan anyway, purely because its content_inlinks was 0 -- which
        would have produced exactly the false finding that exemption exists to
        prevent. Nothing links to a home page from body content by design.
        """
        block = format_crawl_facts_block({
            "url": "https://nrankai.com/",
            "content_inlinks": 0, "nav_inlinks": 0, "crawl_depth": 0,
            "outlinks_total": 4, "is_orphan": False,
            "inbound_anchors": [], "broken_outlinks": [],
        })
        self.assertNotIn("orphan", block.lower())
        self.assertIn("home page", block.lower())

    def test_orphan_page_is_stated_plainly(self):
        block = format_crawl_facts_block({
            "url": "https://example.com/orphan",
            "content_inlinks": 0, "nav_inlinks": 12, "crawl_depth": 3,
            "outlinks_total": 2, "is_orphan": True,
            "inbound_anchors": [], "broken_outlinks": [],
        })
        self.assertIn("ZERO", block)
        self.assertIn("orphan", block.lower())

    def test_broken_outlinks_are_listed_with_their_status(self):
        block = format_crawl_facts_block({
            "url": "https://example.com/a",
            "content_inlinks": 2, "nav_inlinks": 10, "crawl_depth": 1,
            "outlinks_total": 5, "is_orphan": False, "inbound_anchors": [],
            "broken_outlinks": [{"dest_url": "https://example.com/gone", "status_code": 404}],
        })
        self.assertIn("https://example.com/gone", block)
        self.assertIn("404", block)

    def test_absence_of_broken_links_is_stated_positively(self):
        """Silence would let the model assume broken links were simply not checked."""
        block = format_crawl_facts_block({
            "url": "https://example.com/a",
            "content_inlinks": 2, "nav_inlinks": 10, "crawl_depth": 1,
            "outlinks_total": 5, "is_orphan": False, "inbound_anchors": [],
            "broken_outlinks": [],
        })
        self.assertIn("none found", block.lower())

    def test_block_is_delimited_so_the_model_can_tell_facts_from_page_text(self):
        block = format_crawl_facts_block(None)
        self.assertTrue(block.startswith("==="))
        self.assertIn("END CRAWL FACTS", block)


class TestLoadPageFacts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.website = f"https://t{uuid.uuid4().hex[:8]}.example"
        self.old_id = str(uuid.uuid4())
        self.new_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async with AsyncSessionLocal() as db:
            db.add(SiteCrawl(id=self.old_id, website=self.website, status="completed",
                             completed_at=now - timedelta(days=10)))
            db.add(SiteCrawl(id=self.new_id, website=self.website, status="completed",
                             completed_at=now))
            db.add(CrawlPage(id=str(uuid.uuid4()), crawl_id=self.old_id,
                             url=f"{self.website}/p", content_inlinks=99, nav_inlinks=1,
                             crawl_depth=1, outlinks_total=1))
            db.add(CrawlPage(id=str(uuid.uuid4()), crawl_id=self.new_id,
                             url=f"{self.website}/p", content_inlinks=4, nav_inlinks=20,
                             crawl_depth=2, outlinks_total=7))
            db.add(CrawlLink(id=str(uuid.uuid4()), crawl_id=self.new_id,
                             source_url=f"{self.website}/home", dest_url=f"{self.website}/p",
                             anchor="our plans", reason="content", link_position="Content"))
            db.add(CrawlLink(id=str(uuid.uuid4()), crawl_id=self.new_id,
                             source_url=f"{self.website}/p", dest_url=f"{self.website}/dead",
                             anchor="old doc", reason="error", dest_status_code=404))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            for crawl_id in (self.old_id, self.new_id):
                crawl = (await db.execute(
                    select(SiteCrawl).where(SiteCrawl.id == crawl_id)
                )).scalar_one_or_none()
                if crawl:
                    await db.delete(crawl)
            await db.commit()

    async def test_reads_the_newest_completed_crawl_not_an_older_one(self):
        """A stale crawl must never shadow a fresh one."""
        facts = await load_page_facts(self.website, f"{self.website}/p")
        self.assertEqual(facts["content_inlinks"], 4)   # not 99
        self.assertEqual(facts["crawl_depth"], 2)

    async def test_collects_inbound_anchor_text(self):
        facts = await load_page_facts(self.website, f"{self.website}/p")
        self.assertIn("our plans", facts["inbound_anchors"])

    async def test_collects_broken_outbound_links_from_this_page(self):
        facts = await load_page_facts(self.website, f"{self.website}/p")
        self.assertEqual(len(facts["broken_outlinks"]), 1)
        self.assertEqual(facts["broken_outlinks"][0]["dest_url"], f"{self.website}/dead")
        self.assertEqual(facts["broken_outlinks"][0]["status_code"], 404)

    async def test_unknown_page_returns_none(self):
        facts = await load_page_facts(self.website, f"{self.website}/never-crawled")
        self.assertIsNone(facts)

    async def test_uncrawled_site_returns_none(self):
        facts = await load_page_facts("https://never-crawled.example", "https://never-crawled.example/x")
        self.assertIsNone(facts)

    async def test_a_running_crawl_is_not_used(self):
        """
        A crawl still in flight has partial counts. Reading it would report a
        page as having fewer inbound links than it really has -- which, at
        zero, trips the prompt's cap. Only completed crawls count.
        """
        running_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as db:
            db.add(SiteCrawl(id=running_id, website=self.website, status="running",
                             completed_at=datetime.now(timezone.utc) + timedelta(days=1)))
            db.add(CrawlPage(id=str(uuid.uuid4()), crawl_id=running_id,
                             url=f"{self.website}/p", content_inlinks=0, nav_inlinks=0,
                             crawl_depth=9, outlinks_total=0))
            await db.commit()
        try:
            facts = await load_page_facts(self.website, f"{self.website}/p")
            self.assertEqual(facts["content_inlinks"], 4)  # the completed crawl, not the running one
        finally:
            async with AsyncSessionLocal() as db:
                crawl = (await db.execute(
                    select(SiteCrawl).where(SiteCrawl.id == running_id)
                )).scalar_one_or_none()
                if crawl:
                    await db.delete(crawl)
                    await db.commit()


if __name__ == "__main__":
    unittest.main()
