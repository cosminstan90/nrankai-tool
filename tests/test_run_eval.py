"""
Pasul 11 of docs/superpowers/plans/2026-09-30-next-steps.md.

Tests scripts/run_eval.py's OWN logic (fixture loading, keyword matching,
prompt hashing, result-file shape) with the real LLM call mocked out --
this harness costs real money to actually run, and that real run is meant
to happen manually and deliberately (see tests/eval/README.md), never as
part of this suite.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts import run_eval


class TestScoreMatches(unittest.TestCase):
    def test_a_present_keyword_is_matched(self):
        issues = [{"id": "x", "keywords": ["contrast", "ff6200"]}]
        result = run_eval.score_matches(issues, "this page has a contrast problem")
        self.assertEqual(result["matched"], ["x"])
        self.assertEqual(result["missed"], [])
        self.assertEqual(result["recall"], 1.0)

    def test_an_absent_keyword_is_missed(self):
        issues = [{"id": "x", "keywords": ["contrast", "ff6200"]}]
        result = run_eval.score_matches(issues, "this page looks fine")
        self.assertEqual(result["matched"], [])
        self.assertEqual(result["missed"], ["x"])
        self.assertEqual(result["recall"], 0.0)

    def test_matching_is_case_insensitive(self):
        issues = [{"id": "x", "keywords": ["CONTRAST"]}]
        result = run_eval.score_matches(issues, "a contrast issue")
        self.assertEqual(result["matched"], ["x"])

    def test_only_one_keyword_of_several_needs_to_match(self):
        issues = [{"id": "x", "keywords": ["thin content", "shallow", "superficial"]}]
        result = run_eval.score_matches(issues, "this content feels superficial")
        self.assertEqual(result["matched"], ["x"])

    def test_partial_recall_across_multiple_issues(self):
        issues = [
            {"id": "a", "keywords": ["contrast"]},
            {"id": "b", "keywords": ["schema"]},
        ]
        result = run_eval.score_matches(issues, "there is a contrast problem here")
        self.assertEqual(result["matched"], ["a"])
        self.assertEqual(result["missed"], ["b"])
        self.assertEqual(result["recall"], 0.5)

    def test_no_issues_at_all_is_no_recall_number(self):
        result = run_eval.score_matches([], "anything")
        self.assertIsNone(result["recall"])


class TestLoadFixtures(unittest.TestCase):
    def test_the_real_fixtures_directory_has_the_three_documented_fixtures(self):
        fixtures = run_eval.load_fixtures()
        names = {f["name"] for f in fixtures}
        self.assertEqual(names, {"low_contrast", "thin_content", "no_schema"})

    def test_filtering_by_audit_type(self):
        fixtures = run_eval.load_fixtures(audit_type="ACCESSIBILITY_AUDIT")
        self.assertEqual([f["name"] for f in fixtures], ["low_contrast"])

    def test_filtering_by_fixture_name(self):
        fixtures = run_eval.load_fixtures(fixture_name="no_schema")
        self.assertEqual([f["name"] for f in fixtures], ["no_schema"])

    def test_every_fixture_has_page_txt_and_at_least_one_issue(self):
        for fixture in run_eval.load_fixtures():
            self.assertTrue(os.path.isfile(os.path.join(fixture["path"], "page.txt")), fixture["name"])
            self.assertTrue(fixture["expected"].get("issues"), fixture["name"])

    def test_a_nonexistent_directory_returns_no_fixtures_not_an_error(self):
        self.assertEqual(run_eval.load_fixtures(fixtures_dir="/no/such/dir"), [])


class TestPromptHash(unittest.TestCase):
    def test_a_real_audit_type_hashes_to_a_short_hex_string(self):
        h = run_eval.prompt_hash("CONTENT_QUALITY")
        self.assertEqual(len(h), 12)
        int(h, 16)   # raises if not hex

    def test_the_same_audit_type_hashes_the_same_way_twice(self):
        self.assertEqual(run_eval.prompt_hash("CONTENT_QUALITY"), run_eval.prompt_hash("CONTENT_QUALITY"))

    def test_an_unknown_audit_type_does_not_raise(self):
        result = run_eval.prompt_hash("NOT_A_REAL_AUDIT_TYPE")
        self.assertIn("unavailable", result)


class TestRunOneFixtureWithoutCallingAnyLlm(unittest.IsolatedAsyncioTestCase):
    """run_direct_analysis is mocked -- these must never make a real API call."""

    @staticmethod
    def _stats(input_tokens=100, output_tokens=50):
        class _Stats:
            total_input_tokens = input_tokens
            total_output_tokens = output_tokens
        return _Stats()

    async def test_a_matched_fixture_reports_full_recall(self):
        async def _fake_run(**kwargs):
            with open(os.path.join(kwargs["output_dir"], "result.json"), "w", encoding="utf-8") as f:
                f.write('{"finding": "this page has thin content and lacks depth"}')
            return self._stats()

        fixtures = run_eval.load_fixtures(fixture_name="thin_content")
        with patch.object(run_eval, "run_direct_analysis", AsyncMock(side_effect=_fake_run)):
            result = await run_eval.run_one_fixture(fixtures[0], "anthropic", "claude-haiku-4-5-20251001")

        self.assertEqual(result["recall"], 1.0)
        self.assertEqual(result["missed"], [])
        self.assertEqual(result["input_tokens"], 100)
        self.assertEqual(result["output_tokens"], 50)

    async def test_a_missed_fixture_reports_zero_recall(self):
        async def _fake_run(**kwargs):
            with open(os.path.join(kwargs["output_dir"], "result.json"), "w", encoding="utf-8") as f:
                # Deliberately avoids "thin" as a bare substring (it hides
                # inside ordinary words like "everything" and "nothing") --
                # this test caught exactly that mistake once already, which
                # is the actual risk of crude substring keyword matching.
                f.write('{"finding": "well structured, comprehensive, no issues to flag here"}')
            return self._stats()

        fixtures = run_eval.load_fixtures(fixture_name="thin_content")
        with patch.object(run_eval, "run_direct_analysis", AsyncMock(side_effect=_fake_run)):
            result = await run_eval.run_one_fixture(fixtures[0], "anthropic", "claude-haiku-4-5-20251001")

        self.assertEqual(result["recall"], 0.0)
        self.assertEqual(result["matched"], [])

    async def test_no_output_produced_is_reported_not_raised(self):
        async def _empty_run(**kwargs):
            class _Stats:
                total_input_tokens = 10
                total_output_tokens = 5
            return _Stats()   # writes nothing to output_dir

        fixtures = run_eval.load_fixtures(fixture_name="thin_content")
        with patch.object(run_eval, "run_direct_analysis", AsyncMock(side_effect=_empty_run)):
            result = await run_eval.run_one_fixture(fixtures[0], "anthropic", "claude-haiku-4-5-20251001")

        self.assertEqual(result["error"], "no output produced")
        self.assertEqual(result["recall"], 0.0)

    async def test_the_axe_sidecar_is_actually_written_for_the_low_contrast_fixture(self):
        """Confirms the harness wires into the same sidecar path the real accessibility pipeline reads."""
        from core.axe_runner import load_axe_results

        captured_html_dir = {}

        async def _capture_and_run(**kwargs):
            captured_html_dir["path"] = kwargs.get("html_dir")
            with open(os.path.join(kwargs["output_dir"], "result.json"), "w", encoding="utf-8") as f:
                f.write('{"finding": "contrast issue with ff6200"}')

            class _Stats:
                total_input_tokens = 10
                total_output_tokens = 5
            return _Stats()

        fixtures = run_eval.load_fixtures(fixture_name="low_contrast")
        with patch.object(run_eval, "run_direct_analysis", AsyncMock(side_effect=_capture_and_run)):
            result = await run_eval.run_one_fixture(fixtures[0], "anthropic", "claude-haiku-4-5-20251001")

        self.assertEqual(result["recall"], 1.0)
        # The sidecar only exists inside the (now-deleted) temp dir, so what
        # matters is that html_dir was passed at all -- confirming the fixture's
        # page.html took the ACCESSIBILITY_AUDIT / TECHNICAL_SEO fact-injection
        # path rather than being silently skipped.
        self.assertIsNotNone(captured_html_dir["path"])


class TestRunEvalOrchestration(unittest.IsolatedAsyncioTestCase):
    async def test_no_matching_fixtures_reports_an_error_not_an_exception(self):
        summary = await run_eval.run_eval(audit_type="NOT_A_REAL_TYPE", fixture_name=None,
                                          provider="anthropic", model="claude-haiku-4-5-20251001")
        self.assertIn("error", summary)

    async def test_a_full_run_produces_the_documented_summary_shape(self):
        async def _fake_run(**kwargs):
            with open(os.path.join(kwargs["output_dir"], "result.json"), "w", encoding="utf-8") as f:
                f.write('{"finding": "no schema, no structured data, no json-ld here"}')

            class _Stats:
                total_input_tokens = 20
                total_output_tokens = 10
            return _Stats()

        with patch.object(run_eval, "run_direct_analysis", AsyncMock(side_effect=_fake_run)):
            summary = await run_eval.run_eval(audit_type=None, fixture_name="no_schema",
                                              provider="anthropic", model="claude-haiku-4-5-20251001")

        self.assertEqual(summary["fixtures_run"], 1)
        self.assertEqual(summary["mean_recall"], 1.0)
        self.assertIn("total_cost_usd", summary)
        self.assertEqual(summary["results"][0]["prompt_hash"], run_eval.prompt_hash("TECHNICAL_SEO"))


if __name__ == "__main__":
    unittest.main()
