"""
Pasul 18 of docs/superpowers/plans/2026-09-30-next-steps.md.

core.action_learning is pure logic on already-loaded action rows -- no
database, no network.
"""
import unittest
from datetime import datetime, timedelta, timezone

from core.action_learning import (
    MIN_DAYS_BEFORE_REPORT, MIN_SAMPLE_FOR_CONCLUSIONS,
    build_learning_report, classify_effect, is_eligible_for_report,
)

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


class TestClassifyEffect(unittest.TestCase):
    def test_higher_is_better_and_current_went_up_is_improved(self):
        self.assertEqual(classify_effect(10.0, 20.0, higher_is_better=True), "improved")

    def test_higher_is_better_and_current_went_down_is_worse(self):
        self.assertEqual(classify_effect(20.0, 10.0, higher_is_better=True), "worse")

    def test_lower_is_better_and_current_went_down_is_improved(self):
        self.assertEqual(classify_effect(10.0, 5.0, higher_is_better=False), "improved")

    def test_lower_is_better_and_current_went_up_is_worse(self):
        self.assertEqual(classify_effect(5.0, 10.0, higher_is_better=False), "worse")

    def test_tiny_change_within_noise_floor_is_unchanged(self):
        self.assertEqual(classify_effect(4.00, 4.02, higher_is_better=False), "unchanged")

    def test_missing_baseline_is_insufficient_data(self):
        self.assertEqual(classify_effect(None, 20.0, higher_is_better=True), "insufficient_data")

    def test_missing_current_is_insufficient_data(self):
        self.assertEqual(classify_effect(10.0, None, higher_is_better=True), "insufficient_data")

    def test_zero_baseline_uses_absolute_delta_not_a_divide_by_zero(self):
        self.assertEqual(classify_effect(0.0, 5.0, higher_is_better=True), "improved")


class TestIsEligibleForReport(unittest.TestCase):
    def test_applied_far_enough_in_the_past_is_eligible(self):
        applied = NOW - timedelta(days=40)
        self.assertTrue(is_eligible_for_report(applied, NOW))

    def test_applied_too_recently_is_not_eligible(self):
        applied = NOW - timedelta(days=10)
        self.assertFalse(is_eligible_for_report(applied, NOW))

    def test_exactly_min_days_is_eligible(self):
        applied = NOW - timedelta(days=MIN_DAYS_BEFORE_REPORT)
        self.assertTrue(is_eligible_for_report(applied, NOW))

    def test_never_applied_is_not_eligible(self):
        self.assertFalse(is_eligible_for_report(None, NOW))


def _action(source, days_ago, baseline, current, higher_is_better=True):
    return {
        "source": source, "page_url": "https://example.com/x",
        "applied_at": NOW - timedelta(days=days_ago),
        "baseline_value": baseline, "current_value": current,
        "higher_is_better": higher_is_better,
    }


class TestBuildLearningReport(unittest.TestCase):
    def test_a_source_below_min_sample_reports_insufficient_data(self):
        actions = [_action("gsc_opportunity", 40, 10, 20) for _ in range(MIN_SAMPLE_FOR_CONCLUSIONS - 1)]
        report = build_learning_report(actions, NOW)
        self.assertEqual(report["gsc_opportunity"]["status"], "insufficient_data")
        self.assertEqual(report["gsc_opportunity"]["eligible_count"], MIN_SAMPLE_FOR_CONCLUSIONS - 1)

    def test_a_source_at_min_sample_with_clear_improvement_reports_ok(self):
        actions = [_action("gsc_opportunity", 40, 10, 20) for _ in range(MIN_SAMPLE_FOR_CONCLUSIONS)]
        report = build_learning_report(actions, NOW)
        self.assertEqual(report["gsc_opportunity"]["status"], "ok")
        self.assertEqual(report["gsc_opportunity"]["improved"], MIN_SAMPLE_FOR_CONCLUSIONS)
        self.assertEqual(report["gsc_opportunity"]["worse"], 0)

    def test_actions_applied_too_recently_dont_count_toward_the_sample(self):
        eligible = [_action("decay", 40, 10, 20) for _ in range(MIN_SAMPLE_FOR_CONCLUSIONS)]
        too_recent = [_action("decay", 5, 10, 20)]
        report = build_learning_report(eligible + too_recent, NOW)
        self.assertEqual(report["decay"]["eligible_count"], MIN_SAMPLE_FOR_CONCLUSIONS)

    def test_a_not_yet_remeasured_action_does_not_block_the_rest_of_the_group(self):
        measured = [_action("internal_link", 40, 10, 20) for _ in range(MIN_SAMPLE_FOR_CONCLUSIONS)]
        not_remeasured = [_action("internal_link", 40, 10, None)]
        report = build_learning_report(measured + not_remeasured, NOW)
        self.assertEqual(report["internal_link"]["status"], "ok")
        self.assertEqual(report["internal_link"]["not_yet_remeasured"], 1)
        self.assertEqual(report["internal_link"]["improved"], MIN_SAMPLE_FOR_CONCLUSIONS)

    def test_sources_are_reported_independently(self):
        few = [_action("citation_gap", 40, 10, 20) for _ in range(2)]
        many = [_action("fanout_gap", 40, 10, 5, higher_is_better=False) for _ in range(MIN_SAMPLE_FOR_CONCLUSIONS)]
        report = build_learning_report(few + many, NOW)
        self.assertEqual(report["citation_gap"]["status"], "insufficient_data")
        self.assertEqual(report["fanout_gap"]["status"], "ok")
        self.assertEqual(report["fanout_gap"]["improved"], MIN_SAMPLE_FOR_CONCLUSIONS)

    def test_no_applied_actions_at_all_is_an_empty_report_not_an_error(self):
        self.assertEqual(build_learning_report([], NOW), {})


if __name__ == "__main__":
    unittest.main()
