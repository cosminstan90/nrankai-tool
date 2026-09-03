"""
Pins the fix from Etapa 5.5 of the consolidation (docs/CONSOLIDATION_PLAN.md):
crawl_audit()'s _fetch_and_save() only wrapped the HTTP fetch
(extract_page_meta) in the concurrency semaphore -- the subsequent DB
select/add/setattr ran unsynchronized across every in-flight task. A single
SQLAlchemy AsyncSession is not safe for concurrent use by more than one
task at a time (this is documented SQLAlchemy behavior, not a project-specific
assumption), so once more than one page finished fetching around the same
time, they raced on the shared `db` session -- this is the entry point of
every ContentIQ crawl (POST /api/contentiq/audits/{id}/start).

The fix adds a dedicated asyncio.Lock() around the DB section only, keeping
the HTTP fetch concurrency (the semaphore) untouched -- so fetches still run
in parallel but all AsyncSession access is serialized.

This test simulates high fetch concurrency with an injected delay (so many
tasks finish extract_page_meta at nearly the same instant, which is exactly
the condition that triggered the race) and asserts the crawl completes
without a SQLAlchemy concurrency error and produces exactly one CiqPage row
per URL.
"""
import asyncio
import unittest
from unittest.mock import patch

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.contentiq import CiqAudit, CiqPage
from api.workers.contentiq import crawler as crawler_module


async def _fake_extract_page_meta(url: str, client) -> dict:
    # Simulate real network latency so many concurrent tasks land on the
    # DB section at nearly the same instant -- the exact condition that
    # triggered the pre-fix race.
    await asyncio.sleep(0.01)
    return {
        "url": url,
        "status_code": 200,
        "title": f"Title for {url}",
        "h1": None,
        "meta_description": None,
        "canonical": None,
        "word_count": 42,
        "last_modified": None,
    }


class TestCrawlAuditDbConcurrency(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            audits = (await db.execute(
                select(CiqAudit).where(CiqAudit.domain == "test-crawl-concurrency.example.com")
            )).scalars().all()
            for a in audits:
                await db.delete(a)  # cascades to CiqPage
            await db.commit()

    async def test_high_concurrency_crawl_does_not_race_on_shared_session(self):
        num_urls = 40
        fake_urls = [f"https://test-crawl-concurrency.example.com/page-{i}" for i in range(num_urls)]

        async with AsyncSessionLocal() as db:
            audit = CiqAudit(
                label="concurrency test",
                domain="test-crawl-concurrency.example.com",
                sitemap_url="https://test-crawl-concurrency.example.com/sitemap.xml",
                status="pending",
            )
            db.add(audit)
            await db.flush()
            audit_id = audit.id
            await db.commit()

        with patch.object(crawler_module, "crawl_sitemap", return_value=fake_urls), \
             patch.object(crawler_module, "extract_page_meta", _fake_extract_page_meta):
            async with AsyncSessionLocal() as db:
                # concurrency=10 with a 0.01s delay means ~10 tasks finish
                # their fetch and reach the DB section within the same
                # event-loop tick, repeatedly, across the 40 URLs.
                await crawler_module.crawl_audit(
                    audit_id, "https://test-crawl-concurrency.example.com/sitemap.xml",
                    db, max_urls=100, concurrency=10,
                )

        async with AsyncSessionLocal() as db:
            refreshed = (await db.execute(
                select(CiqAudit).where(CiqAudit.id == audit_id)
            )).scalar_one()
            self.assertEqual(refreshed.status, "scoring")
            self.assertEqual(refreshed.total_urls, num_urls)

            pages = (await db.execute(
                select(CiqPage).where(CiqPage.audit_id == audit_id)
            )).scalars().all()
            self.assertEqual(len(pages), num_urls)
            self.assertEqual({p.url for p in pages}, set(fake_urls))
            self.assertTrue(all(p.word_count == 42 for p in pages))


if __name__ == "__main__":
    unittest.main()
