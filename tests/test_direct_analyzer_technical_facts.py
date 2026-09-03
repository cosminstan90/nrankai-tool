"""
Etapa 6 of the consolidation (docs/CONSOLIDATION_PLAN.md): verifies the
DirectAnalyzer wiring that injects deterministic technical-SEO facts into
the page content sent to the LLM -- only for the TECHNICAL_SEO audit type,
using the domain facts fetched once in run() plus per-page structured-data
facts parsed from the original (pre-conversion) HTML in html_dir.

Exercises _process_single_page() directly (the method that actually builds
and injects the facts block) rather than the full run() orchestration --
run() also sets up real OS signal handlers and a tqdm progress bar, neither
of which this test needs and both of which are unrelated to what's being
verified here. No real network or LLM calls are made.
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock

from core.direct_analyzer import DirectAnalyzer


class FakeClient:
    def __init__(self):
        self.calls = []

    async def complete(self, system_message, user_content, max_tokens, output_schema=None, **kwargs):
        self.calls.append(user_content)
        return ('{"ok": true}', 0, 0)


class TestDirectAnalyzerTechnicalFacts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.input_dir = os.path.join(self.tmpdir.name, "input_llm")
        self.output_dir = os.path.join(self.tmpdir.name, "output")
        self.html_dir = os.path.join(self.tmpdir.name, "input_html")
        os.makedirs(self.input_dir)
        os.makedirs(self.output_dir)
        os.makedirs(self.html_dir)

        with open(os.path.join(self.input_dir, "page1.txt"), "w", encoding="utf-8") as f:
            f.write("Some converted page text for page1.")
        with open(os.path.join(self.html_dir, "page1.html"), "w", encoding="utf-8") as f:
            f.write('<html><script type="application/ld+json">{"@type": "Article"}</script></html>')

        # Page with no matching original HTML on disk at all
        with open(os.path.join(self.input_dir, "page2.txt"), "w", encoding="utf-8") as f:
            f.write("Some converted page text for page2.")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_analyzer(self, question_type: str, html_dir):
        analyzer = DirectAnalyzer(
            input_dir=self.input_dir,
            output_dir=self.output_dir,
            question_type=question_type,
            provider="ANTHROPIC",
            model_name="claude-test",
            website="example.com",
            html_dir=html_dir,
        )
        analyzer.client = FakeClient()
        analyzer._rate_limiter.acquire = AsyncMock(return_value=None)
        return analyzer

    async def _process_both_pages(self, analyzer):
        semaphore = asyncio.Semaphore(5)
        await analyzer._process_single_page("page1.txt", semaphore)
        await analyzer._process_single_page("page2.txt", semaphore)
        return analyzer.client.calls

    async def test_facts_injected_only_for_technical_seo(self):
        analyzer = self._make_analyzer("technical_seo", self.html_dir)
        analyzer._domain_facts = {
            "robots_txt_accessible": True, "ai_crawlers_blocked": ["GPTBot"],
            "ai_crawlers_allowed": ["ClaudeBot"], "llms_txt_present": False,
        }
        prompts = await self._process_both_pages(analyzer)
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("AUTOMATED VERIFICATION FACTS" in p for p in prompts))

    async def test_facts_not_injected_for_other_audit_types(self):
        analyzer = self._make_analyzer("seo_audit", self.html_dir)
        prompts = await self._process_both_pages(analyzer)
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("AUTOMATED VERIFICATION FACTS" not in p for p in prompts))

    async def test_structured_data_detected_when_html_present(self):
        analyzer = self._make_analyzer("technical_seo", self.html_dir)
        analyzer._domain_facts = None
        prompts = await self._process_both_pages(analyzer)
        page1_prompt = next(p for p in prompts if "Some converted page text for page1" in p)
        self.assertIn("DETECTED on this page: Article", page1_prompt)

    async def test_no_structured_data_when_html_missing(self):
        analyzer = self._make_analyzer("technical_seo", self.html_dir)
        analyzer._domain_facts = None
        prompts = await self._process_both_pages(analyzer)
        page2_prompt = next(p for p in prompts if "Some converted page text for page2" in p)
        self.assertIn("NONE detected on this page", page2_prompt)

    async def test_domain_facts_reflected_in_every_page(self):
        analyzer = self._make_analyzer("technical_seo", self.html_dir)
        analyzer._domain_facts = {
            "robots_txt_accessible": True, "ai_crawlers_blocked": ["GPTBot"],
            "ai_crawlers_allowed": [], "llms_txt_present": False,
        }
        prompts = await self._process_both_pages(analyzer)
        for p in prompts:
            self.assertIn("BLOCKS these AI crawlers: GPTBot", p)

    async def test_missing_html_dir_still_works_with_domain_facts_only(self):
        analyzer = self._make_analyzer("technical_seo", None)
        analyzer._domain_facts = {
            "robots_txt_accessible": True, "ai_crawlers_blocked": [],
            "ai_crawlers_allowed": ["GPTBot"], "llms_txt_present": True,
        }
        prompts = await self._process_both_pages(analyzer)
        self.assertEqual(len(prompts), 2)
        for p in prompts:
            self.assertIn("AUTOMATED VERIFICATION FACTS", p)
            self.assertIn("NONE detected on this page", p)


if __name__ == "__main__":
    unittest.main()
