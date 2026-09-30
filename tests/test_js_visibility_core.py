"""
Pasul 12 of docs/superpowers/plans/2026-09-30-next-steps.md.

Pure comparison logic (core.js_visibility.compare_visibility) and sidecar
read/write, tested against synthetic SSR/SPA-shaped HTML -- no network,
no browser.
"""
import tempfile
import unittest
from pathlib import Path

from core.js_visibility import (
    compare_visibility, format_js_visibility_facts_block,
    load_js_visibility_facts, raw_text_sidecar_path, write_js_visibility_facts,
)

SSR_PAGE = """
<html><head><title>Credit ipotecar</title>
<script type="application/ld+json">{"@type": "FinancialProduct", "name": "Credit ipotecar"}</script>
</head>
<body>
  <h1>Credit ipotecar cu dobandă fixă</h1>
  <p>Creditul ipotecar cu dobândă fixă îți oferă predictibilitate pe toată durata împrumutului tău.</p>
  <p>Rata este de 6.5% pe an, iar suma maximă este 500000 lei.</p>
  <h2>Întrebări frecvente</h2>
  <p>Ce acte îmi trebuie?</p>
  <p>Cât durează aprobarea?</p>
  <p>Pot rambursa anticipat?</p>
</body></html>
"""

# A SPA shell: the raw HTML has almost nothing, everything is injected by
# client-side JS that a bot-UA fetch never runs.
SPA_SHELL = """
<html><head><title>App</title></head>
<body><div id="root"></div><script src="/app.js"></script></body></html>
"""


class TestCompareVisibilityOnASsrPage(unittest.TestCase):
    def test_fully_rendered_page_matches_itself(self):
        result = compare_visibility(SSR_PAGE, SSR_PAGE)
        self.assertEqual(result["text_visibility_ratio"], 1.0)
        self.assertEqual(result["missing_elements"], [])
        self.assertTrue(result["measured"])


class TestCompareVisibilityOnASpaShell(unittest.TestCase):
    def test_visibility_ratio_is_near_zero(self):
        result = compare_visibility(SSR_PAGE, SPA_SHELL)
        self.assertLess(result["text_visibility_ratio"], 0.2)

    def test_h1_is_reported_missing(self):
        result = compare_visibility(SSR_PAGE, SPA_SHELL)
        elements = {m["element"] for m in result["missing_elements"]}
        self.assertIn("h1", elements)

    def test_json_ld_is_reported_missing(self):
        result = compare_visibility(SSR_PAGE, SPA_SHELL)
        elements = {m["element"] for m in result["missing_elements"]}
        self.assertIn("json_ld", elements)

    def test_prices_are_reported_missing(self):
        result = compare_visibility(SSR_PAGE, SPA_SHELL)
        elements = {m["element"] for m in result["missing_elements"]}
        self.assertIn("prices_or_figures", elements)

    def test_faq_is_reported_missing(self):
        result = compare_visibility(SSR_PAGE, SPA_SHELL)
        elements = {m["element"] for m in result["missing_elements"]}
        self.assertIn("faq", elements)

    def test_first_paragraph_is_reported_missing(self):
        result = compare_visibility(SSR_PAGE, SPA_SHELL)
        elements = {m["element"] for m in result["missing_elements"]}
        self.assertIn("first_paragraph", elements)


class TestCompareVisibilityWithABotStatusCode(unittest.TestCase):
    def test_a_403_is_flagged_as_blocked(self):
        result = compare_visibility(SSR_PAGE, "", bot_status_code=403)
        self.assertTrue(result["bot_blocked_or_errored"])
        self.assertEqual(result["bot_status_code"], 403)

    def test_a_200_is_not_flagged_as_blocked(self):
        result = compare_visibility(SSR_PAGE, SSR_PAGE, bot_status_code=200)
        self.assertFalse(result["bot_blocked_or_errored"])

    def test_a_network_error_is_flagged_as_blocked(self):
        result = compare_visibility(SSR_PAGE, "", bot_status_code=None, bot_error="connection reset")
        self.assertTrue(result["bot_blocked_or_errored"])
        self.assertEqual(result["bot_fetch_error"], "connection reset")

    def test_an_empty_rendered_page_has_no_ratio(self):
        """No denominator to compute a ratio against -- None, not 0 or 1."""
        result = compare_visibility("", "")
        self.assertIsNone(result["text_visibility_ratio"])


class TestSidecarRoundTrip(unittest.TestCase):
    def test_write_then_load_returns_the_same_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = str(Path(tmp) / "page.html")
            facts = compare_visibility(SSR_PAGE, SPA_SHELL)
            write_js_visibility_facts(html_path, facts)
            loaded = load_js_visibility_facts(html_path)
            self.assertEqual(loaded["text_visibility_ratio"], facts["text_visibility_ratio"])
            self.assertEqual(len(loaded["missing_elements"]), len(facts["missing_elements"]))

    def test_no_sidecar_yet_is_none_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            html_path = str(Path(tmp) / "never_measured.html")
            self.assertIsNone(load_js_visibility_facts(html_path))

    def test_sidecar_path_matches_the_html_stem(self):
        self.assertEqual(raw_text_sidecar_path("/x/page.html"), "/x/page.rawtext.json")


class TestFormatFactsBlock(unittest.TestCase):
    def test_unmeasured_says_so_instead_of_claiming_visible(self):
        text = format_js_visibility_facts_block(None)
        self.assertIn("no automated measurement", text)
        self.assertNotIn("100%", text)

    def test_measured_page_reports_the_ratio_and_missing_elements(self):
        facts = compare_visibility(SSR_PAGE, SPA_SHELL)
        text = format_js_visibility_facts_block(facts)
        self.assertIn("h1", text)
        self.assertIn("json_ld", text)
        self.assertRegex(text, r"\d+%")

    def test_a_blocked_bot_fetch_is_called_out_as_critical(self):
        facts = compare_visibility(SSR_PAGE, "", bot_status_code=403)
        text = format_js_visibility_facts_block(facts)
        self.assertIn("CRITICAL", text)
        self.assertIn("403", text)

    def test_a_fully_visible_page_says_nothing_missing(self):
        facts = compare_visibility(SSR_PAGE, SSR_PAGE)
        text = format_js_visibility_facts_block(facts)
        self.assertIn("no specific elements", text)


if __name__ == "__main__":
    unittest.main()
