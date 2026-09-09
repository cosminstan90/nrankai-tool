"""
The crawl subprocess must not block the event loop.

run_site_crawl is handed to FastAPI as a BackgroundTask and FastAPI runs async
background tasks on the event loop. core.sf_crawler.run_crawl calls
subprocess.run(), which is synchronous and blocking, so calling it directly
from the coroutine would freeze the entire API for the length of the crawl --
verified at over 10 minutes on a small site. Every other request, including
/api/health, would hang.
"""
import asyncio
import time
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import SiteCrawl
import api.workers.crawl_worker as worker


class TestCrawlDoesNotBlockTheEventLoop(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(SiteCrawl).where(SiteCrawl.website == self.website)
            )).scalars().all()
            for row in rows:
                await db.delete(row)
            await db.commit()

    async def test_other_coroutines_keep_running_during_the_crawl(self):
        self.website = f"https://t{uuid.uuid4().hex[:8]}.example"
        BLOCK_FOR = 0.6

        def fake_blocking_crawl(website, output_dir, timeout=1800):
            time.sleep(BLOCK_FOR)   # stands in for subprocess.run
            raise RuntimeError("stop here -- persistence is covered elsewhere")

        ticks = 0

        async def heartbeat():
            """Stands in for the rest of the API still serving requests."""
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            with patch.object(worker, "run_crawl", fake_blocking_crawl):
                with self.assertRaises(RuntimeError):
                    await worker.run_site_crawl(self.website)
        finally:
            beat.cancel()

        # With a blocking call on the loop, ticks would be 0 or 1. Off-loop, the
        # heartbeat keeps firing every 50ms for the whole 600ms.
        self.assertGreater(
            ticks, 3,
            f"event loop was blocked during the crawl (only {ticks} ticks in {BLOCK_FOR}s)",
        )

    async def test_failure_is_still_recorded_when_run_off_the_loop(self):
        """Moving the call off the loop must not lose the failure handling."""
        self.website = f"https://t{uuid.uuid4().hex[:8]}.example"

        def boom(website, output_dir, timeout=1800):
            raise RuntimeError("crawl exploded")

        with patch.object(worker, "run_crawl", boom):
            with self.assertRaises(RuntimeError):
                await worker.run_site_crawl(self.website)

        async with AsyncSessionLocal() as db:
            crawl = (await db.execute(
                select(SiteCrawl).where(SiteCrawl.website == self.website)
            )).scalar_one()

        self.assertEqual(crawl.status, "failed")
        self.assertIn("crawl exploded", crawl.error)
        self.assertIsNotNone(crawl.completed_at)


if __name__ == "__main__":
    unittest.main()
