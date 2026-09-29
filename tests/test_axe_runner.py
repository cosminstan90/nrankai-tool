"""
Etapa 7 of docs/IMPROVEMENTS_PLAN.md -- axe-core accessibility facts.

tests/fixtures/axe/ing_persoane_fizice.json is an unmodified axe-core 4.13.0
result for https://ing.ro/persoane-fizice, captured 2026-09-29 in a real
browser: five rules violated, of which only color-contrast maps to a WCAG
success criterion.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from core.axe_runner import (
    AXE_PATH,
    format_axe_facts_block,
    load_axe_results,
    run_axe,
    summarize,
    wcag_criteria,
    write_axe_results,
)

FIXTURE = Path(__file__).parent / "fixtures" / "axe" / "ing_persoane_fizice.json"


def _summary():
    return summarize(json.loads(FIXTURE.read_text(encoding="utf-8")), url="https://ing.ro/persoane-fizice")


class TestVendoredEngine(unittest.TestCase):
    def test_axe_is_vendored_with_version_and_licence(self):
        self.assertTrue(AXE_PATH.exists())
        self.assertTrue((AXE_PATH.parent / "LICENSE").exists())
        self.assertIn("4.13.0", (AXE_PATH.parent / "VERSION").read_text(encoding="utf-8"))


class TestWcagCriteria(unittest.TestCase):
    def test_maps_criterion_tags(self):
        self.assertEqual(wcag_criteria(["wcag143"]), ["1.4.3"])
        self.assertEqual(wcag_criteria(["wcag1410", "wcag2411"]), ["1.4.10", "2.4.11"])

    def test_level_and_non_wcag_tags_are_not_criteria(self):
        self.assertEqual(wcag_criteria(["wcag2aa", "wcag21a", "best-practice", "cat.color"]), [])


class TestSummarizeRealResult(unittest.TestCase):
    def setUp(self):
        self.s = _summary()
        self.by_id = {v["id"]: v for v in self.s["violations"]}

    def test_only_color_contrast_is_a_wcag_failure(self):
        """Five rules violated; reporting five WCAG failures would overstate it five-fold."""
        self.assertEqual(self.s["wcag_failures"], 1)
        self.assertEqual(self.s["best_practice_issues"], 4)
        self.assertTrue(self.by_id["color-contrast"]["is_wcag_failure"])
        self.assertEqual(self.by_id["color-contrast"]["wcag"], ["1.4.3"])

    def test_impact_and_conformance_stay_separate(self):
        """label-title-only is 'serious' and still only best practice."""
        v = self.by_id["label-title-only"]
        self.assertEqual(v["impact"], "serious")
        self.assertFalse(v["is_wcag_failure"])

    def test_wcag_failures_sort_first(self):
        self.assertEqual(self.s["violations"][0]["id"], "color-contrast")

    def test_contrast_examples_carry_the_measured_colours(self):
        """White on ING orange at 3:1 against a required 4.5:1 -- what text cannot show."""
        ex = self.by_id["color-contrast"]["examples"][0]
        self.assertEqual(ex["contrast"]["background"], "#ff6200")
        self.assertEqual(ex["contrast"]["foreground"], "#ffffff")
        self.assertEqual(ex["contrast"]["ratio"], 3)
        self.assertEqual(ex["contrast"]["required"], "4.5:1")

    def test_examples_are_capped_but_node_counts_are_not(self):
        region = self.by_id["region"]
        self.assertEqual(region["nodes"], 32)
        self.assertLessEqual(len(region["examples"]), 3)


class TestFactsBlock(unittest.TestCase):
    def test_unmeasured_page_is_never_called_clean(self):
        block = format_axe_facts_block(None)
        self.assertIn("no automated measurement", block)
        self.assertNotIn("no automated violations", block)

    def test_block_separates_wcag_failures_from_best_practice(self):
        block = format_axe_facts_block(_summary())
        self.assertIn("WCAG success-criterion failures: 1", block)
        self.assertIn("NOT WCAG failures", block)
        self.assertIn("best practice, not a WCAG failure", block)
        self.assertIn("WCAG 1.4.3", block)

    def test_block_includes_measured_contrast(self):
        block = format_axe_facts_block(_summary())
        self.assertIn("#ff6200", block)
        self.assertIn("4.5:1", block)

    def test_a_clean_page_still_notes_the_limits_of_automation(self):
        clean = {"engine": "axe-core 4.13.0", "wcag_failures": 0, "best_practice_issues": 0,
                 "incomplete": 0, "violations": []}
        block = format_axe_facts_block(clean)
        self.assertIn("covers only part", block)


class TestSidecar(unittest.TestCase):
    def test_round_trip_next_to_the_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            html = str(Path(tmp) / "ing.ro_persoane-fizice.html")
            write_axe_results(html, _summary())
            self.assertTrue((Path(tmp) / "ing.ro_persoane-fizice.axe.json").exists())
            self.assertEqual(load_axe_results(html)["wcag_failures"], 1)

    def test_missing_sidecar_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_axe_results(str(Path(tmp) / "x.html")))


class TestRunAxe(unittest.TestCase):
    def test_driver_failure_returns_none_not_a_clean_result(self):
        driver = MagicMock()
        driver.execute_async_script.side_effect = Exception("page crashed")
        self.assertIsNone(run_axe(driver))

    def test_axe_error_payload_returns_none(self):
        driver = MagicMock()
        driver.execute_async_script.return_value = {"error": "axe is not defined"}
        self.assertIsNone(run_axe(driver))


if __name__ == "__main__":
    unittest.main()
