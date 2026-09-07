"""
Etapa 5 of docs/IMPROVEMENTS_PLAN.md -- persisting a crawl.

Runs against the committed fixtures, so no Screaming Frog and no network. Each
test uses its own crawl id so rows never collide between tests or with real
data in the dev database.
"""
import unittest
import uuid
from pathlib import Path

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CrawlLink, CrawlPage, SiteCrawl
from api.workers.crawl_worker import persist_crawl
from core.sf_crawler import CrawlArtifacts

FIXTURES = Path(__file__).parent / "fixtures" / "sf"


def _artifacts() -> CrawlArtifacts:
    """Point the worker at the fixtures. Two paths deliberately do not exist --
    SF is run with --skip-empty, so absent exports are a normal case."""
    return CrawlArtifacts(
        output_dir=FIXTURES,
        inlinks_csv=FIXTURES / "all_inlinks.csv",
        internal_html_csv=FIXTURES / "internal_html.csv",
        errors_4xx_csv=FIXTURES / "response_codes_internal_client_error_(4xx).csv",
        orphan_urls_csv=FIXTURES / "does_not_exist.csv",
        redirect_chains_csv=FIXTURES / "does_not_exist.csv",
    )


class TestPersistCrawl(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.crawl_id = str(uuid.uuid4())
        self.website = "https://example.com"
        await persist_crawl(self.crawl_id, self.website, _artifacts())

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            crawl = (await db.execute(
                select(SiteCrawl).where(SiteCrawl.id == self.crawl_id)
            )).scalar_one_or_none()
            if crawl:
                await db.delete(crawl)  # cascades to pages and links
                await db.commit()

    async def _pages(self):
        async with AsyncSessionLocal() as db:
            return (await db.execute(
                select(CrawlPage).where(CrawlPage.crawl_id == self.crawl_id)
            )).scalars().all()

    async def test_persists_pages_and_only_filtered_edges(self):
        async with AsyncSessionLocal() as db:
            crawl = (await db.execute(
                select(SiteCrawl).where(SiteCrawl.id == self.crawl_id)
            )).scalar_one()
            links = (await db.execute(
                select(CrawlLink).where(CrawlLink.crawl_id == self.crawl_id)
            )).scalars().all()

        self.assertEqual(crawl.status, "completed")
        self.assertEqual(len(await self._pages()), 4)
        # 1 content + 2 broken, out of 5 hyperlink rows in the fixture
        self.assertEqual(len(links), 3)
        self.assertEqual(crawl.content_edges, 1)
        self.assertGreater(crawl.nav_edges_discarded, 0)

    async def test_content_inlink_counts_land_on_the_page_rows(self):
        """
        The number prompts/internal_linking.yaml scores on: how many BODY
        links point at this page, counted apart from navigation.
        """
        pages = {p.url: p for p in await self._pages()}

        self.assertEqual(pages["https://example.com/pricing"].content_inlinks, 1)
        self.assertEqual(pages["https://example.com/pricing"].nav_inlinks, 0)
        # /about is linked from the nav (home) and from an aside (pricing)
        self.assertEqual(pages["https://example.com/about"].content_inlinks, 0)
        self.assertEqual(pages["https://example.com/about"].nav_inlinks, 2)

    async def test_page_with_no_content_inlinks_is_flagged_orphan(self):
        pages = {p.url: p for p in await self._pages()}
        self.assertTrue(pages["https://example.com/orphan-ish"].is_orphan)
        self.assertFalse(pages["https://example.com/pricing"].is_orphan)

    async def test_home_page_is_never_called_an_orphan(self):
        """
        Nothing links to the home page from body content on most sites, by
        design. Flagging it would put a false finding at the top of every
        report, so depth 0 is exempt.
        """
        pages = {p.url: p for p in await self._pages()}
        home = pages["https://example.com/"]
        self.assertEqual(home.content_inlinks, 0)
        self.assertFalse(home.is_orphan)

    async def test_broken_edge_keeps_the_page_that_links_to_it(self):
        """
        SF's 4xx tab export gives only an inlink count; the source pages come
        from the stored error edges. Without them the finding is not actionable.
        """
        async with AsyncSessionLocal() as db:
            broken = (await db.execute(
                select(CrawlLink).where(
                    CrawlLink.crawl_id == self.crawl_id,
                    CrawlLink.reason == "error",
                )
            )).scalars().all()

        by_dest = {b.dest_url: b for b in broken}
        self.assertEqual(by_dest["https://example.com/gone"].source_url, "https://example.com/pricing")
        self.assertEqual(by_dest["https://example.com/missing"].source_url, "https://example.com/about")


if __name__ == "__main__":
    unittest.main()
