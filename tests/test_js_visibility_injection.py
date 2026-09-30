"""
Pasul 12 of docs/superpowers/plans/2026-09-30-next-steps.md -- JS-visibility
facts must actually reach the model.

Mirrors tests/test_axe_injection.py's approach exactly: intercepts the
analyzer at the point where the finished page text, facts included, is
handed to the chunker -- the last step before the LLM -- rather than testing
the formatter in isolation, which would pass even if the analyzer never
called it.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.direct_analyzer import DirectAnalyzer
from core.js_visibility import compare_visibility, write_js_visibility_facts

SSR_PAGE = """
<html><body>
  <h1>Persoane fizice</h1>
  <p>Deschide un cont curent în câteva minute, direct din aplicație, fără drumuri la bancă.</p>
</body></html>
"""
SPA_SHELL = "<html><body><div id=\"root\"></div></body></html>"


class _Stop(Exception):
    pass


def _text_sent_to_model(question_type: str, with_measurement: bool) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        input_dir, html_dir, out_dir = tmp / "llm", tmp / "html", tmp / "out"
        for d in (input_dir, html_dir, out_dir):
            d.mkdir()
        (input_dir / "ing.ro_persoane-fizice.txt").write_text("Persoane fizice. Deschide un cont.", encoding="utf-8")
        html_path = html_dir / "ing.ro_persoane-fizice.html"
        html_path.write_text(SSR_PAGE, encoding="utf-8")
        if with_measurement:
            facts = compare_visibility(SSR_PAGE, SPA_SHELL, bot_status_code=200)
            write_js_visibility_facts(str(html_path), facts)

        analyzer = DirectAnalyzer(
            input_dir=str(input_dir), output_dir=str(out_dir), question_type=question_type,
            provider="ANTHROPIC", model_name="claude-sonnet-4-6",
            website="https://ing.ro", html_dir=str(html_dir),
        )
        captured = {}

        def capture(page_text, max_chars=None):
            captured["text"] = page_text
            raise _Stop()

        analyzer.chunker = MagicMock()
        analyzer.chunker.chunk_content.side_effect = capture
        try:
            asyncio.run(analyzer._process_single_page("ing.ro_persoane-fizice.txt", asyncio.Semaphore(1)))
        except _Stop:
            pass
        return captured.get("text", "")


class TestJsVisibilityFactsReachTheModel(unittest.TestCase):
    def test_measured_page_carries_the_facts_for_geo_audit(self):
        text = _text_sent_to_model("GEO_AUDIT", with_measurement=True)
        self.assertTrue(text.startswith("=== AI-CRAWLER VISIBILITY FACTS"))
        self.assertIn("h1", text)
        self.assertIn("Persoane fizice. Deschide un cont.", text)   # page text still follows

    def test_measured_page_carries_the_facts_for_ai_overview_optimization(self):
        text = _text_sent_to_model("AI_OVERVIEW_OPTIMIZATION", with_measurement=True)
        self.assertTrue(text.startswith("=== AI-CRAWLER VISIBILITY FACTS"))

    def test_unmeasured_page_says_so_instead_of_claiming_visible(self):
        text = _text_sent_to_model("GEO_AUDIT", with_measurement=False)
        self.assertIn("no automated measurement", text)
        self.assertNotIn("100%", text)

    def test_other_audit_types_are_untouched(self):
        """Adding facts to every audit would change ~20 audit types' input."""
        text = _text_sent_to_model("CONTENT_QUALITY", with_measurement=True)
        self.assertIn("Persoane fizice. Deschide un cont.", text)   # capture really happened
        self.assertNotIn("AI-CRAWLER VISIBILITY", text)


if __name__ == "__main__":
    unittest.main()
