"""
Etapa 7 -- axe-core facts must actually reach the model.

Tests the formatter in isolation would pass even if the analyzer never called
it. These intercept the analyzer at the point where the finished page text,
facts included, is handed to the chunker -- the last step before the LLM.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.axe_runner import summarize, write_axe_results
from core.direct_analyzer import DirectAnalyzer

FIXTURE = Path(__file__).parent / "fixtures" / "axe" / "ing_persoane_fizice.json"


class _Stop(Exception):
    pass


def _text_sent_to_model(question_type: str, with_axe: bool) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        input_dir, html_dir, out_dir = tmp / "llm", tmp / "html", tmp / "out"
        for d in (input_dir, html_dir, out_dir):
            d.mkdir()
        (input_dir / "ing.ro_persoane-fizice.txt").write_text("Persoane fizice. Deschide un cont.", encoding="utf-8")
        (html_dir / "ing.ro_persoane-fizice.html").write_text("<div>Persoane fizice</div>", encoding="utf-8")
        if with_axe:
            raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
            write_axe_results(str(html_dir / "ing.ro_persoane-fizice.html"), summarize(raw))

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


class TestAxeFactsReachTheModel(unittest.TestCase):
    def test_measured_page_carries_the_facts_including_contrast(self):
        text = _text_sent_to_model("ACCESSIBILITY_AUDIT", with_axe=True)
        self.assertTrue(text.startswith("=== AXE-CORE ACCESSIBILITY FACTS"))
        self.assertIn("WCAG success-criterion failures: 1", text)
        self.assertIn("#ff6200", text)
        self.assertIn("Persoane fizice. Deschide un cont.", text)   # page text still follows

    def test_unmeasured_page_says_so_instead_of_passing(self):
        text = _text_sent_to_model("ACCESSIBILITY_AUDIT", with_axe=False)
        self.assertIn("no automated measurement", text)
        self.assertNotIn("no automated violations", text)

    def test_other_audit_types_are_untouched(self):
        """Adding facts to every audit would change ~20 audit types' input."""
        text = _text_sent_to_model("CONTENT_QUALITY", with_axe=True)
        self.assertIn("Persoane fizice. Deschide un cont.", text)   # capture really happened
        self.assertNotIn("AXE-CORE", text)


if __name__ == "__main__":
    unittest.main()
