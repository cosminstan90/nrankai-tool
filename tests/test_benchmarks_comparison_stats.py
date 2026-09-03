"""
Pins the fix from Etapa 5.2 of the consolidation (docs/CONSOLIDATION_PLAN.md):
the page-count-weighted competitor average / best-competitor-score /
target-rank computation was duplicated verbatim in
_build_benchmark_data_payload() and get_benchmark_detail() inside
api/routes/benchmarks.py. Extracted into _compute_comparison_stats(), used
by both.
"""
import unittest

from api.routes.benchmarks import _compute_comparison_stats


class TestComputeComparisonStats(unittest.TestCase):
    def test_weighted_average_favors_larger_competitor(self):
        competitors = [
            {"avg_score": 80.0, "pages_analyzed": 100},
            {"avg_score": 40.0, "pages_analyzed": 10},
        ]
        stats = _compute_comparison_stats(50.0, competitors)
        # weighted toward the 100-page competitor (score 80), not a plain mean (60)
        self.assertGreater(stats["weighted_avg"], 60.0)

    def test_best_competitor_score_is_max(self):
        competitors = [
            {"avg_score": 80.0, "pages_analyzed": 10},
            {"avg_score": 40.0, "pages_analyzed": 10},
        ]
        stats = _compute_comparison_stats(50.0, competitors)
        self.assertEqual(stats["best_competitor_score"], 80.0)

    def test_target_rank_first_place(self):
        competitors = [{"avg_score": 60.0, "pages_analyzed": 10}]
        stats = _compute_comparison_stats(90.0, competitors)
        self.assertEqual(stats["target_rank"], 1)
        self.assertEqual(stats["total_count"], 2)

    def test_target_rank_last_place(self):
        competitors = [
            {"avg_score": 60.0, "pages_analyzed": 10},
            {"avg_score": 70.0, "pages_analyzed": 10},
        ]
        stats = _compute_comparison_stats(30.0, competitors)
        self.assertEqual(stats["target_rank"], 3)

    def test_no_competitors_does_not_crash(self):
        stats = _compute_comparison_stats(50.0, [])
        self.assertEqual(stats["weighted_avg"], 0)
        self.assertEqual(stats["best_competitor_score"], 0)
        self.assertEqual(stats["target_rank"], 1)
        self.assertEqual(stats["total_count"], 1)


if __name__ == "__main__":
    unittest.main()
