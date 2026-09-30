"""
Pasul 17 of docs/superpowers/plans/2026-09-30-next-steps.md.

Integration test for GET /api/fanout/sessions/{id}/coverage-gaps: seeds a
real FanoutSession + FanoutQuery, writes real HTML into a throwaway site
directory under the project root (the same convention
api/routes/internal_links.py's tests already use), with
core.embeddings.embed_text mocked (fixed vectors, no real OpenAI call).
"""
import math
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.models._base import AsyncSessionLocal
from api.models.database import FanoutQuery, FanoutSession

WEBSITE = "fanout-coverage-test.example"
COVERED_QUERY = "cum aleg creditul potrivit"
UNCOVERED_QUERY = "retete de prajituri traditionale"


def _vector_at_similarity(target_similarity: float):
    angle = math.acos(target_similarity)
    return [math.cos(angle), math.sin(angle)]


REFERENCE = [1.0, 0.0]


class TestFanoutCoverageGapsEndpoint(unittest.TestCase):
    def setUp(self):
        import asyncio
        self.session_id = str(uuid.uuid4())
        self.html_dir = Path(WEBSITE) / "input_html"
        self.html_dir.mkdir(parents=True, exist_ok=True)
        (self.html_dir / "page.html").write_text(
            "<html><body><h2>Credit ipotecar</h2><p>"
            + " ".join(["cuvant"] * 200) + "</p></body></html>",
            encoding="utf-8",
        )

        async def seed():
            async with AsyncSessionLocal() as db:
                db.add(FanoutSession(
                    id=self.session_id, prompt="test prompt", provider="anthropic",
                    model="claude-haiku-4-5-20251001", target_url=f"https://{WEBSITE}",
                ))
                await db.flush()
                db.add(FanoutQuery(session_id=self.session_id, query_text=COVERED_QUERY, query_position=1))
                db.add(FanoutQuery(session_id=self.session_id, query_text=UNCOVERED_QUERY, query_position=2))
                await db.commit()

        asyncio.run(seed())
        from api.main import app
        self.client = TestClient(app)

    def tearDown(self):
        import asyncio
        shutil.rmtree(WEBSITE, ignore_errors=True)

        async def clean():
            async with AsyncSessionLocal() as db:
                session = await db.get(FanoutSession, self.session_id)
                if session:
                    await db.delete(session)   # cascades to its queries
                await db.commit()

        asyncio.run(clean())

    def _fake_embed(self):
        async def _embed(text, model=None, source=None):
            if text == COVERED_QUERY:
                return REFERENCE
            if text == UNCOVERED_QUERY:
                return [0.0, 1.0]   # orthogonal -- 0.0 similarity with anything at REFERENCE
            # The page's own passage -- give it REFERENCE too, so the covered
            # query (also REFERENCE) matches perfectly and the uncovered
            # query (orthogonal) matches nothing.
            return REFERENCE
        return AsyncMock(side_effect=_embed)

    def test_a_covered_query_is_reported_covered(self):
        with patch("api.routes.fanout_coverage.embed_text", self._fake_embed()):
            body = self.client.get(f"/api/fanout/sessions/{self.session_id}/coverage-gaps").json()
        by_query = {r["query"]: r for r in body["report"]["queries"]}
        self.assertEqual(by_query[COVERED_QUERY]["status"], "covered")

    def test_an_uncovered_query_is_reported_uncovered_and_grouped(self):
        with patch("api.routes.fanout_coverage.embed_text", self._fake_embed()):
            body = self.client.get(f"/api/fanout/sessions/{self.session_id}/coverage-gaps").json()
        by_query = {r["query"]: r for r in body["report"]["queries"]}
        self.assertEqual(by_query[UNCOVERED_QUERY]["status"], "uncovered")
        all_gap_queries = [r["query"] for group in body["report"]["gaps_by_cluster"].values() for r in group]
        self.assertIn(UNCOVERED_QUERY, all_gap_queries)

    def test_unknown_session_is_404(self):
        resp = self.client.get(f"/api/fanout/sessions/{uuid.uuid4()}/coverage-gaps")
        self.assertEqual(resp.status_code, 404)

    def test_a_session_with_no_target_url_reports_why(self):
        import asyncio

        async def seed_no_target():
            sid = str(uuid.uuid4())
            async with AsyncSessionLocal() as db:
                db.add(FanoutSession(id=sid, prompt="p", provider="anthropic",
                                     model="claude-haiku-4-5-20251001", target_url=None))
                await db.commit()
            return sid

        sid = asyncio.run(seed_no_target())
        try:
            body = self.client.get(f"/api/fanout/sessions/{sid}/coverage-gaps").json()
            self.assertIsNone(body["report"])
            self.assertIn("target_url", body["note"])
        finally:
            async def clean():
                async with AsyncSessionLocal() as db:
                    s = await db.get(FanoutSession, sid)
                    if s:
                        await db.delete(s)
                    await db.commit()
            asyncio.run(clean())


if __name__ == "__main__":
    unittest.main()
