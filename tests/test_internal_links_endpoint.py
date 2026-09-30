"""
Pasul 14 of docs/superpowers/plans/2026-09-30-next-steps.md.

Integration test for GET /api/internal-links/suggestions: seeds a real
SiteCrawl + CrawlPage + CrawlLink + PageSnapshot graph, with
core.embeddings.embed_text mocked (fixed vectors, no real OpenAI call).
"""
import json
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.models._base import AsyncSessionLocal
from api.models.database import CrawlLink, CrawlPage, PageSnapshot, SiteCrawl, SnapshotRun

CLOSE_A = [1.0, 0.0, 0.0]
CLOSE_B = [0.95, 0.05, 0.0]
UNRELATED = [0.0, 1.0, 0.0]

WEBSITE = "https://internal-links-test.example"


class TestInternalLinkSuggestionsEndpoint(unittest.TestCase):
    def setUp(self):
        import asyncio
        self.crawl_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(SiteCrawl(id=self.crawl_id, website=WEBSITE, status="completed"))
                self.run_id = str(uuid.uuid4())
                db.add(SnapshotRun(id=self.run_id, website=WEBSITE, status="completed"))
                await db.flush()

                # /target: orphaned, indexable -- a real target.
                db.add(CrawlPage(id=str(uuid.uuid4()), crawl_id=self.crawl_id,
                                 url=f"{WEBSITE}/target", status_code=200,
                                 indexability="Indexable", content_inlinks=0, is_orphan=True))
                # /close-source: indexable, topically close, no existing link to target.
                db.add(CrawlPage(id=str(uuid.uuid4()), crawl_id=self.crawl_id,
                                 url=f"{WEBSITE}/close-source", status_code=200,
                                 indexability="Indexable", content_inlinks=5, is_orphan=False))
                # /unrelated: indexable but topically unrelated.
                db.add(CrawlPage(id=str(uuid.uuid4()), crawl_id=self.crawl_id,
                                 url=f"{WEBSITE}/unrelated", status_code=200,
                                 indexability="Indexable", content_inlinks=5, is_orphan=False))
                # /broken: 404, must never become a candidate source.
                db.add(CrawlPage(id=str(uuid.uuid4()), crawl_id=self.crawl_id,
                                 url=f"{WEBSITE}/broken", status_code=404,
                                 indexability="Non-Indexable", content_inlinks=0, is_orphan=True))

                # PageSnapshot text for the real (non-broken) pages -- broken/
                # unrelated deliberately get distinguishable content. Padded
                # past MIN_WORDS_TO_EMBED (20): a title+h1 alone is too short
                # for the endpoint's own "not enough text to embed" filter,
                # exactly as a real thin page would be.
                for path, title, h1, h2 in [
                    ("/target", "Credit ipotecar cu dobanda fixa",
                     ["Credit ipotecar cu dobanda fixa pentru prima casa"],
                     ["Rata ramane aceeasi pe toata durata imprumutului contractat"]),
                    ("/close-source", "Ghid credit ipotecar pentru prima casa",
                     ["Ghid complet despre creditul ipotecar cu dobanda fixa"],
                     ["Documente necesare si etapele aprobarii creditului ipotecar"]),
                    ("/unrelated", "Rețete de prăjituri de casă",
                     ["Retete simple de prajituri traditionale de casa"],
                     ["Ingrediente si mod de preparare pentru cozonac de casa"]),
                ]:
                    db.add(PageSnapshot(
                        id=str(uuid.uuid4()), run_id=self.run_id, website=WEBSITE,
                        url=f"{WEBSITE}{path}", title=title, h1=h1, h2=h2, h3=[],
                        word_count=500, captured_at=now,
                    ))
                await db.commit()

        asyncio.run(seed())
        from api.main import app
        self.client = TestClient(app)

    def tearDown(self):
        import asyncio

        async def clean():
            async with AsyncSessionLocal() as db:
                crawl = await db.get(SiteCrawl, self.crawl_id)
                if crawl:
                    await db.delete(crawl)
                run = await db.get(SnapshotRun, self.run_id)
                if run:
                    await db.delete(run)   # cascades to its page_snapshots rows
                await db.commit()

        asyncio.run(clean())

    def _fake_embed(self, vectors_by_url):
        async def _embed(text, model=None, source=None):
            for url, (title_marker, vector) in vectors_by_url.items():
                if title_marker in text:
                    return vector
            return [0.0, 0.0, 0.0]
        return AsyncMock(side_effect=_embed)

    def test_a_topically_close_source_is_suggested_for_the_orphaned_target(self):
        vectors_by_url = {
            f"{WEBSITE}/target": ("Credit ipotecar", CLOSE_A),
            f"{WEBSITE}/close-source": ("Ghid credit", CLOSE_B),
            f"{WEBSITE}/unrelated": ("Rețete", UNRELATED),
        }
        with patch("api.routes.internal_links.embed_text", self._fake_embed(vectors_by_url)):
            body = self.client.get("/api/internal-links/suggestions",
                                   params={"crawl_id": self.crawl_id}).json()

        # /target only: /broken is also orphaned, but non-indexable and 404,
        # so find_link_targets excludes it before it ever becomes a target.
        self.assertEqual(body["targets_considered"], 1)
        self.assertEqual(len(body["suggestions"]), 1)
        suggestion = body["suggestions"][0]
        self.assertEqual(suggestion["target_url"], f"{WEBSITE}/target")
        source_urls = {s["url"] for s in suggestion["candidate_sources"]}
        self.assertIn(f"{WEBSITE}/close-source", source_urls)
        self.assertNotIn(f"{WEBSITE}/unrelated", source_urls)
        self.assertNotIn(f"{WEBSITE}/broken", source_urls)

    def test_a_source_that_already_links_to_the_target_is_excluded(self):
        import asyncio

        async def add_link():
            async with AsyncSessionLocal() as db:
                db.add(CrawlLink(id=str(uuid.uuid4()), crawl_id=self.crawl_id,
                                 source_url=f"{WEBSITE}/close-source", dest_url=f"{WEBSITE}/target",
                                 reason="content"))
                await db.commit()

        asyncio.run(add_link())

        vectors_by_url = {
            f"{WEBSITE}/target": ("Credit ipotecar", CLOSE_A),
            f"{WEBSITE}/close-source": ("Ghid credit", CLOSE_B),
            f"{WEBSITE}/unrelated": ("Rețete", UNRELATED),
        }
        with patch("api.routes.internal_links.embed_text", self._fake_embed(vectors_by_url)):
            body = self.client.get("/api/internal-links/suggestions",
                                   params={"crawl_id": self.crawl_id}).json()

        # No qualifying source left (the only topically-close one already links there) -> no suggestion at all.
        self.assertEqual(body["suggestions"], [])

    def test_unknown_crawl_is_404(self):
        resp = self.client.get("/api/internal-links/suggestions", params={"crawl_id": str(uuid.uuid4())})
        self.assertEqual(resp.status_code, 404)

    def test_no_gsc_property_given_means_no_anchor(self):
        vectors_by_url = {
            f"{WEBSITE}/target": ("Credit ipotecar", CLOSE_A),
            f"{WEBSITE}/close-source": ("Ghid credit", CLOSE_B),
            f"{WEBSITE}/unrelated": ("Rețete", UNRELATED),
        }
        with patch("api.routes.internal_links.embed_text", self._fake_embed(vectors_by_url)):
            body = self.client.get("/api/internal-links/suggestions",
                                   params={"crawl_id": self.crawl_id}).json()
        self.assertFalse(body["anchors_from_gsc"])
        self.assertIsNone(body["suggestions"][0]["anchor"])


if __name__ == "__main__":
    unittest.main()
