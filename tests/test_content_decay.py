"""
Pasul 15 of docs/superpowers/plans/2026-09-30-next-steps.md.

Synthetic weekly series, as the plan asks for -- core.content_decay is pure
aggregation with no DB access.
"""
import unittest
from datetime import date, timedelta

from core.content_decay import (
    build_weekly_series, detect_decay, linear_slope,
)


def _daily_rows_for_weeks(weekly_clicks, start_date, impressions_per_day=100, position=5.0):
    """weekly_clicks: oldest-first list of TOTAL clicks for that week; spread
    evenly across 7 daily rows so build_weekly_series reconstructs it exactly."""
    rows = []
    d = start_date
    for week_clicks in weekly_clicks:
        per_day = week_clicks // 7
        remainder = week_clicks - per_day * 7
        for i in range(7):
            clicks = per_day + (1 if i < remainder else 0)
            rows.append({"period_start": d.isoformat(), "clicks": clicks,
                        "impressions": impressions_per_day, "position": position})
            d += timedelta(days=1)
    return rows


class TestBuildWeeklySeries(unittest.TestCase):
    def test_reconstructs_the_right_number_of_weeks(self):
        rows = _daily_rows_for_weeks([100, 90, 80], date(2026, 1, 1))
        weeks = build_weekly_series(rows)
        self.assertEqual(len(weeks), 3)

    def test_weeks_are_returned_oldest_first(self):
        rows = _daily_rows_for_weeks([100, 50], date(2026, 1, 1))
        weeks = build_weekly_series(rows)
        self.assertLess(weeks[0]["week_start"], weeks[1]["week_start"])

    def test_clicks_are_summed_correctly_per_week(self):
        rows = _daily_rows_for_weeks([140], date(2026, 1, 1))
        weeks = build_weekly_series(rows)
        self.assertEqual(weeks[0]["clicks"], 140)

    def test_empty_input_is_an_empty_list_not_an_error(self):
        self.assertEqual(build_weekly_series([]), [])

    def test_a_gap_week_is_skipped_not_inserted_as_zero(self):
        early = _daily_rows_for_weeks([100], date(2026, 1, 1))
        late = _daily_rows_for_weeks([100], date(2026, 3, 1))   # a real gap in between
        weeks = build_weekly_series(early + late)
        # No week in between has clicks=0 inserted -- only weeks with real data appear.
        self.assertTrue(all(w["clicks"] > 0 for w in weeks))


class TestLinearSlope(unittest.TestCase):
    def test_a_steady_decline_has_a_negative_slope(self):
        self.assertLess(linear_slope([100, 90, 80, 70, 60]), 0)

    def test_a_steady_increase_has_a_positive_slope(self):
        self.assertGreater(linear_slope([60, 70, 80, 90, 100]), 0)

    def test_a_flat_series_has_a_zero_slope(self):
        self.assertEqual(linear_slope([50, 50, 50, 50]), 0.0)

    def test_fewer_than_two_points_is_zero_not_an_error(self):
        self.assertEqual(linear_slope([50]), 0.0)
        self.assertEqual(linear_slope([]), 0.0)


class TestDetectDecayInsufficientHistory(unittest.TestCase):
    def test_too_few_weeks_reports_insufficient_history_not_a_verdict(self):
        rows = _daily_rows_for_weeks([100, 90, 80], date(2026, 1, 1))   # only 3 weeks
        weeks = build_weekly_series(rows)
        result = detect_decay(weeks)
        self.assertTrue(result["insufficient_history"])
        self.assertIsNone(result["is_decaying"])
        self.assertIsNone(result["drop_from_peak_pct"])


class TestDetectDecayLinearDecline(unittest.TestCase):
    def _weeks(self, n=16, start_clicks=1000, weekly_drop=40):
        clicks = [start_clicks - i * weekly_drop for i in range(n)]
        rows = _daily_rows_for_weeks(clicks, date(2026, 1, 1))
        return build_weekly_series(rows)

    def test_a_real_linear_decline_is_flagged_as_decaying(self):
        result = detect_decay(self._weeks())
        self.assertFalse(result["insufficient_history"])
        self.assertTrue(result["is_decaying"])

    def test_drop_from_peak_is_computed_correctly(self):
        weeks = self._weeks(n=16, start_clicks=1000, weekly_drop=40)
        result = detect_decay(weeks)
        peak = max(w["clicks"] for w in weeks)
        smoothed_current = sum(w["clicks"] for w in weeks[-4:]) / 4
        expected = round((peak - smoothed_current) / peak, 4)
        self.assertEqual(result["drop_from_peak_pct"], expected)

    def test_a_mild_decline_below_the_peak_drop_threshold_is_not_decaying(self):
        # Small, steady decline that never drops 30% below its own peak.
        weeks = self._weeks(n=16, start_clicks=1000, weekly_drop=2)
        result = detect_decay(weeks)
        self.assertFalse(result["is_decaying"])

    def test_a_flat_series_is_not_decaying(self):
        rows = _daily_rows_for_weeks([500] * 16, date(2026, 1, 1))
        result = detect_decay(build_weekly_series(rows))
        self.assertFalse(result["is_decaying"])

    def test_a_growing_series_is_not_decaying(self):
        rows = _daily_rows_for_weeks([500 + i * 30 for i in range(16)], date(2026, 1, 1))
        result = detect_decay(build_weekly_series(rows))
        self.assertFalse(result["is_decaying"])

    def test_a_noisy_but_not_actually_declining_series_is_not_decaying(self):
        """Alternating up/down around a flat mean -- must not be mistaken for decay."""
        clicks = [500, 520, 480, 510, 490, 505, 495, 500, 515, 485, 500, 500, 500, 500, 500, 500]
        rows = _daily_rows_for_weeks(clicks, date(2026, 1, 1))
        result = detect_decay(build_weekly_series(rows))
        self.assertFalse(result["is_decaying"])


class TestDecayCause(unittest.TestCase):
    def test_worsening_position_is_flagged_as_the_cause(self):
        clicks = [1000 - i * 40 for i in range(16)]
        rows = []
        d = date(2026, 1, 1)
        for week_i, week_clicks in enumerate(clicks):
            position = 3.0 + (week_i / 15) * 10   # position 3 -> 13 across the window
            for _ in range(7):
                rows.append({"period_start": d.isoformat(), "clicks": week_clicks // 7,
                            "impressions": 500, "position": position})
                d += timedelta(days=1)
        result = detect_decay(build_weekly_series(rows))
        self.assertTrue(result["is_decaying"])
        self.assertEqual(result["cause"], "position_drop")

    def test_stable_position_with_falling_ctr_is_flagged_as_ctr_drop(self):
        clicks = [1000 - i * 40 for i in range(16)]
        rows = []
        d = date(2026, 1, 1)
        for week_clicks in clicks:
            # Impressions RISE while clicks fall -- position stays put, CTR falls.
            impressions = 700 + (1000 - week_clicks)
            for _ in range(7):
                rows.append({"period_start": d.isoformat(), "clicks": week_clicks // 7,
                            "impressions": impressions // 7, "position": 5.0})
                d += timedelta(days=1)
        result = detect_decay(build_weekly_series(rows))
        self.assertTrue(result["is_decaying"])
        self.assertEqual(result["cause"], "ctr_drop")


class TestSeasonality(unittest.TestCase):
    def test_pure_seasonality_is_not_flagged_as_real_decay_when_last_year_matches(self):
        """
        A page ramping down into a winter trough, both this year and last,
        by the same amount over the same weeks -- a real ongoing decline
        WITHIN the trend window (so is_decaying is legitimately True by the
        plan's own definition: a consistent negative slope AND a drop from
        peak), but one seasonality should recognise as not new.
        """
        def _year(base_week):
            # Summer peak (1000) for the first 40 weeks, then a smooth 12-week
            # ramp down into a 600 winter trough -- a real negative slope
            # across exactly the trend window (the last 12 weeks of the year).
            year_weeks = [1000] * 40
            year_weeks += [1000 - (i + 1) * ((1000 - 600) / 12) for i in range(12)]
            return year_weeks

        weeks = _year(0) + _year(52)
        rows = _daily_rows_for_weeks([round(w) for w in weeks], date(2024, 1, 1))
        result = detect_decay(build_weekly_series(rows))
        self.assertTrue(result["is_decaying"])   # a real, ongoing decline this window
        self.assertTrue(result["seasonality_checked"])
        self.assertIn("seasonal", result["seasonality_note"])

    def test_less_than_12_months_of_history_reports_seasonality_uncontrolled(self):
        weeks = [1000 - i * 40 for i in range(20)]   # only ~20 weeks, well under 52
        rows = _daily_rows_for_weeks(weeks, date(2026, 1, 1))
        result = detect_decay(build_weekly_series(rows))
        self.assertTrue(result["is_decaying"])
        self.assertFalse(result["seasonality_checked"])
        self.assertIn("uncontrolled", result["seasonality_note"])

    def test_a_real_yoy_decline_is_distinguished_from_seasonality(self):
        """This year's trough is genuinely lower than last year's trough at the same calendar point."""
        weeks = []
        for week in range(52):
            weeks.append(1000)   # last year: flat and healthy all year
        for week in range(52):
            # This year: healthy most of the year, then a real recent decline
            # in the last 16 weeks that does NOT match last year's (there was none).
            weeks.append(1000 if week < 36 else 1000 - (week - 35) * 40)
        rows = _daily_rows_for_weeks(weeks, date(2024, 1, 1))
        result = detect_decay(build_weekly_series(rows))
        self.assertTrue(result["is_decaying"])
        self.assertTrue(result["seasonality_checked"])
        self.assertIn("NOT similarly low", result["seasonality_note"])


if __name__ == "__main__":
    unittest.main()
