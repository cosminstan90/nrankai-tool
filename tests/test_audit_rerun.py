"""
task_06f52aa7 (flagged during Etapa 5.2's file split of compare.py, see
docs/CONSOLIDATION_PLAN.md): api/routes/audit_rerun.py's rerun_single_page()
was broken in two independent ways, both confirmed by reading the real
current APIs rather than trusting the existing code:

1. It constructed core.direct_analyzer.DirectAnalyzer(question_type=...,
   provider=..., model_name=..., max_chars=...) -- missing the two REQUIRED
   positional args (input_dir, output_dir) -- then called
   analyzer.analyze_single_page(page_text, txt_name), a method that does
   not exist on DirectAnalyzer at all. The real per-page entry point is the
   private _process_single_page(filename, semaphore), which reads its own
   file from self.input_dir, needs self.client/self.chunker/self._rate_limiter
   already initialized (normally done inside run()), and returns a
   PageResult, not raw text -- a completely different interface. This
   endpoint could not have completed a single successful call.
2. Independently, its input/output directory paths were built from the raw
   `audit.website` string (e.g. "https://example.com") instead of
   sanitize_website_for_path(audit.website) (e.g. "example.com") -- the
   real convention every other file-writing code path in this codebase
   uses (api/workers/audit_worker.py's own _safe_dir() is exactly this same
   call). The source .txt file could never have been found for any real
   audit, regardless of bug #1.

Fixed by reusing the same simple AsyncLLMClient.complete() pattern already
used successfully by POST /api/audits/single (api/routes/audits.py) instead
of trying to shoehorn the file-orchestration DirectAnalyzer class into a
single ad-hoc page re-analysis it wasn't designed for.

Verified against real production data (ing.ro audit 53a6d6f0-df69-4f68-
84c3-c2f46d487186, result id 1) with the LLM call mocked, then restored the
touched AuditResult row, its output JSON file, and Audit.average_score
back to their original values by hand. This test suite instead uses fresh,
disposable fixtures so nothing production-adjacent needs manual restoring.
"""
import json
import os
import shutil
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.audit import Audit, AuditResult
from api.routes.audit_rerun import rerun_single_page
from api.utils.url_validator import sanitize_website_for_path

TEST_WEBSITE = "https://rerun-test-site.example.com"
FAKE_LLM_RESPONSE = json.dumps({"seo_audit": {"overall_score": 82}, "issues": [], "recommendations": []})


class TestRerunSinglePage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.safe_website = sanitize_website_for_path(TEST_WEBSITE)
        self.input_dir = os.path.join(self.safe_website, "input_llm")
        self.output_dir = os.path.join(self.safe_website, "output_seo_audit")
        os.makedirs(self.input_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)
        with open(os.path.join(self.input_dir, "page1.txt"), "w", encoding="utf-8") as f:
            f.write("Some page content to re-analyze.")
        with open(os.path.join(self.output_dir, "005_page1.json"), "w", encoding="utf-8") as f:
            json.dump({"seo_audit": {"overall_score": 5}}, f)

    def tearDown(self):
        if os.path.isdir(self.safe_website):
            shutil.rmtree(self.safe_website)

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            audits = (await db.execute(
                select(Audit).where(Audit.website == TEST_WEBSITE)
            )).scalars().all()
            for a in audits:
                await db.delete(a)  # cascades to AuditResult
            await db.commit()

    async def _make_audit_and_result(self, db):
        audit = Audit(
            id="test-rerun-audit-id", website=TEST_WEBSITE, audit_type="SEO_AUDIT",
            status="completed", provider="anthropic", model="claude-test",
        )
        db.add(audit)
        await db.flush()
        result = AuditResult(
            audit_id=audit.id, page_url="rerun-test-site.example.com_page1",
            filename="005_page1.json", score=5, classification="poor",
            result_json=json.dumps({"seo_audit": {"overall_score": 5}}),
        )
        db.add(result)
        await db.commit()
        return audit.id, result.id

    async def test_rerun_uses_asyncllmclient_not_directanalyzer(self):
        async with AsyncSessionLocal() as db:
            audit_id, result_id = await self._make_audit_and_result(db)

        with patch("core.direct_analyzer.AsyncLLMClient.complete",
                   new=AsyncMock(return_value=(FAKE_LLM_RESPONSE, 100, 50))), \
             patch("core.direct_analyzer.AsyncLLMClient.close", new=AsyncMock(return_value=None)), \
             patch("api.routes.costs.track_cost", new=AsyncMock(return_value=None)):
            async with AsyncSessionLocal() as db:
                response = await rerun_single_page(audit_id, result_id, db)

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["old_score"], 5)
        self.assertEqual(response["new_score"], 82)
        self.assertEqual(response["new_classification"], "good")

    async def test_finds_input_file_via_sanitized_path(self):
        # Pins the second, independent bug: the endpoint must resolve the
        # source .txt file via sanitize_website_for_path(audit.website),
        # not the raw "https://..." string.
        async with AsyncSessionLocal() as db:
            audit_id, result_id = await self._make_audit_and_result(db)

        with patch("core.direct_analyzer.AsyncLLMClient.complete",
                   new=AsyncMock(return_value=(FAKE_LLM_RESPONSE, 100, 50))) as mock_complete, \
             patch("core.direct_analyzer.AsyncLLMClient.close", new=AsyncMock(return_value=None)), \
             patch("api.routes.costs.track_cost", new=AsyncMock(return_value=None)):
            async with AsyncSessionLocal() as db:
                await rerun_single_page(audit_id, result_id, db)

        self.assertIn("Some page content to re-analyze.", mock_complete.call_args.kwargs["user_content"])

    async def test_db_and_disk_state_updated(self):
        async with AsyncSessionLocal() as db:
            audit_id, result_id = await self._make_audit_and_result(db)

        with patch("core.direct_analyzer.AsyncLLMClient.complete",
                   new=AsyncMock(return_value=(FAKE_LLM_RESPONSE, 100, 50))), \
             patch("core.direct_analyzer.AsyncLLMClient.close", new=AsyncMock(return_value=None)), \
             patch("api.routes.costs.track_cost", new=AsyncMock(return_value=None)):
            async with AsyncSessionLocal() as db:
                await rerun_single_page(audit_id, result_id, db)

        async with AsyncSessionLocal() as db:
            result = await db.get(AuditResult, result_id)
            self.assertEqual(result.score, 82)
            self.assertEqual(result.filename, "082_page1.json")
            audit = await db.get(Audit, audit_id)
            self.assertEqual(audit.average_score, 82.0)

        self.assertFalse(os.path.exists(os.path.join(self.output_dir, "005_page1.json")))
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "082_page1.json")))


if __name__ == "__main__":
    unittest.main()
