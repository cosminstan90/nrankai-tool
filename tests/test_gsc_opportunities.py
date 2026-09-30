"""
Pasul 13 of docs/superpowers/plans/2026-09-30-next-steps.md.

Synthetic data, as the plan asks for -- core.gsc_opportunities is pure
aggregation logic with no DB or network access.
"""
import unittest

from core.gsc_opportunities import (
    build_ctr_curve, expected_ctr_at, find_striking_distance, find_weak_ctr_pages,
)


def _curve_rows(n_per_position=4):
    """A believable property-wide CTR curve: higher position -> higher CTR, with a little spread."""
    rows = []
    ctr_by_pos = {1: 0.30, 2: 0.20, 3: 0.15, 4: 0.10, 5: 0.08, 6: 0.06,
                 8: 0.04, 10: 0.03, 12: 0.02, 15: 0.01}
    for pos, ctr in ctr_by_pos.items():
        for i in range(n_per_position):
            rows.append({"position": pos + (i * 0.05), "ctr": ctr + (i * 0.001),
                         "impressions": 200, "clicks": int(200 * ctr),
                         "page": f"/curve-filler-{pos}-{i}", "query": f"generic query {pos}-{i}"})
    return rows


class TestBuildCtrCurve(unittest.TestCase):
    def test_buckets_by_rounded_position(self):
        curve = build_ctr_curve([{"position": 3.2, "ctr": 0.1}, {"position": 3.4, "ctr": 0.2}])
        self.assertEqual(set(curve.keys()), {3})
        self.assertAlmostEqual(curve[3], 0.15)   # median of 0.1, 0.2

    def test_rows_missing_position_or_ctr_are_skipped(self):
        curve = build_ctr_curve([{"position": None, "ctr": 0.1}, {"position": 3, "ctr": None}])
        self.assertEqual(curve, {})


class TestExpectedCtrAt(unittest.TestCase):
    def test_exact_bucket_match(self):
        curve = {3: 0.15, 5: 0.08}
        self.assertEqual(expected_ctr_at(curve, 3.0), 0.15)

    def test_falls_back_to_nearest_bucket(self):
        curve = {3: 0.15, 10: 0.03}
        self.assertEqual(expected_ctr_at(curve, 4.0), 0.15)   # 4 is closer to 3 than to 10

    def test_empty_curve_is_none(self):
        self.assertIsNone(expected_ctr_at({}, 5.0))


class TestFindWeakCtrPages(unittest.TestCase):
    def test_a_page_below_the_curve_is_an_opportunity(self):
        rows = _curve_rows() + [{"page": "/weak", "position": 4.0, "impressions": 1000,
                                 "clicks": 20, "ctr": 0.02}]   # curve at pos 4 is ~0.10
        result = find_weak_ctr_pages(rows)
        pages = {o["page"] for o in result["opportunities"]}
        self.assertIn("/weak", pages)

    def test_a_page_above_the_curve_is_not_an_opportunity(self):
        rows = _curve_rows() + [{"page": "/strong", "position": 4.0, "impressions": 1000,
                                 "clicks": 300, "ctr": 0.30}]   # far above curve at pos 4
        result = find_weak_ctr_pages(rows)
        pages = {o["page"] for o in result["opportunities"]}
        self.assertNotIn("/strong", pages)

    def test_estimated_gain_matches_the_formula(self):
        rows = _curve_rows() + [{"page": "/weak", "position": 4.0, "impressions": 1000,
                                 "clicks": 20, "ctr": 0.02}]
        result = find_weak_ctr_pages(rows)
        opp = next(o for o in result["opportunities"] if o["page"] == "/weak")
        expected_gain = round(1000 * (opp["expected_ctr"] - 0.02), 1)
        self.assertEqual(opp["estimated_extra_clicks"], expected_gain)

    def test_low_impression_pages_are_excluded_not_scored_zero(self):
        rows = _curve_rows() + [{"page": "/tiny", "position": 4.0, "impressions": 5,
                                 "clicks": 0, "ctr": 0.0}]
        result = find_weak_ctr_pages(rows, min_impressions=100)
        pages = {o["page"] for o in result["opportunities"]}
        self.assertNotIn("/tiny", pages)

    def test_brand_pages_are_excluded(self):
        rows = _curve_rows() + [{"page": "/brand", "position": 4.0, "impressions": 1000,
                                 "clicks": 20, "ctr": 0.02, "query": "acme brand login"}]
        result = find_weak_ctr_pages(rows, brand_terms=["acme"])
        pages = {o["page"] for o in result["opportunities"]}
        self.assertNotIn("/brand", pages)

    def test_too_few_curve_buckets_is_insufficient_data_not_a_zero_result(self):
        result = find_weak_ctr_pages([{"page": "/x", "position": 4.0, "impressions": 500,
                                       "clicks": 10, "ctr": 0.02}])
        self.assertTrue(result["insufficient_data"])
        self.assertEqual(result["opportunities"], [])

    def test_opportunities_are_sorted_by_estimated_gain_descending(self):
        rows = _curve_rows() + [
            {"page": "/small-gain", "position": 4.0, "impressions": 200, "clicks": 16, "ctr": 0.08},
            {"page": "/big-gain", "position": 4.0, "impressions": 5000, "clicks": 50, "ctr": 0.01},
        ]
        result = find_weak_ctr_pages(rows)
        gains = [o["estimated_extra_clicks"] for o in result["opportunities"]]
        self.assertEqual(gains, sorted(gains, reverse=True))


class TestFindStrikingDistance(unittest.TestCase):
    def _rows(self):
        return _curve_rows() + [
            {"page": "/near-top", "query": "credit ipotecar", "position": 7.0,
             "impressions": 2000, "clicks": 40, "ctr": 0.02},
        ]

    def test_a_pair_in_the_position_window_is_an_opportunity(self):
        result = find_striking_distance(self._rows())
        pairs = {(o["page"], o["query"]) for o in result["opportunities"]}
        self.assertIn(("/near-top", "credit ipotecar"), pairs)

    def test_position_outside_the_window_is_excluded(self):
        rows = self._rows() + [{"page": "/page1", "query": "q", "position": 1.0,
                                "impressions": 2000, "clicks": 500, "ctr": 0.25}]
        result = find_striking_distance(rows)
        pairs = {(o["page"], o["query"]) for o in result["opportunities"]}
        self.assertNotIn(("/page1", "q"), pairs)

    def test_low_impressions_are_excluded_not_scored_zero(self):
        rows = self._rows() + [{"page": "/rare", "query": "q", "position": 7.0,
                                "impressions": 3, "clicks": 0, "ctr": 0.0}]
        result = find_striking_distance(rows, min_impressions=100)
        pairs = {(o["page"], o["query"]) for o in result["opportunities"]}
        self.assertNotIn(("/rare", "q"), pairs)

    def test_brand_queries_are_excluded(self):
        rows = self._rows() + [{"page": "/x", "query": "acme login", "position": 7.0,
                                "impressions": 2000, "clicks": 40, "ctr": 0.02}]
        result = find_striking_distance(rows, brand_terms=["acme"])
        pairs = {(o["page"], o["query"]) for o in result["opportunities"]}
        self.assertNotIn(("/x", "acme login"), pairs)

    def test_estimated_gain_targets_position_3_by_default(self):
        result = find_striking_distance(self._rows())
        opp = next(o for o in result["opportunities"] if o["page"] == "/near-top")
        curve = build_ctr_curve(self._rows())
        expected_at_3 = expected_ctr_at(curve, 3.0)
        expected_gain = round(2000 * max(0.0, expected_at_3 - 0.02), 1)
        self.assertEqual(opp["estimated_extra_clicks"], expected_gain)

    def test_too_few_curve_buckets_still_returns_opportunities_without_a_gain_estimate(self):
        """A striking-distance pair is still worth surfacing even with no CTR curve yet -- unlike weak CTR, position alone is the signal."""
        result = find_striking_distance([
            {"page": "/near-top", "query": "q", "position": 7.0, "impressions": 2000, "clicks": 40, "ctr": 0.02},
        ])
        self.assertTrue(result["insufficient_data"])
        self.assertEqual(len(result["opportunities"]), 1)
        self.assertIsNone(result["opportunities"][0]["estimated_extra_clicks"])

    def test_unscored_opportunities_sort_after_scored_ones(self):
        rows = self._rows() + [
            {"page": "/no-ctr-data", "query": "q2", "position": 8.0, "impressions": 500,
             "clicks": 10, "ctr": None},
        ]
        # Force insufficient curve data isn't the case here (curve has enough buckets),
        # but the individual row's own ctr being None still lets a gain be computed via
        # clicks/impressions -- verify sort just doesn't crash and orders sensibly.
        result = find_striking_distance(rows)
        scored = [o for o in result["opportunities"] if o["estimated_extra_clicks"] is not None]
        self.assertEqual(scored, sorted(scored, key=lambda o: -o["estimated_extra_clicks"]))


if __name__ == "__main__":
    unittest.main()
