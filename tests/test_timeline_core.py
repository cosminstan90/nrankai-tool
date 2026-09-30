"""
Pasul 10 of docs/superpowers/plans/2026-09-30-next-steps.md.

Synthetic data, as the plan asks for: core/timeline.py is pure aggregation
logic, independent of the database, so window math is tested directly
against constructed rows rather than by seeding four real tables.
"""
import unittest
from datetime import date, timedelta

from core.timeline import (
    build_applied_action_markers, build_change_windows, build_timeline, gsc_window_stats,
)


def _daily_row(d: date, clicks=10, impressions=100, position=5.0):
    return {"period_start": d.isoformat(), "clicks": clicks, "impressions": impressions, "position": position}


def _days(start: date, n: int, **kw):
    return [_daily_row(start + timedelta(days=i), **kw) for i in range(n)]


class TestGscWindowStats(unittest.TestCase):
    def test_averages_over_the_window(self):
        rows = _days(date(2026, 1, 1), 5, clicks=10, impressions=100, position=4.0)
        stats = gsc_window_stats(rows, date(2026, 1, 1), date(2026, 1, 5))
        self.assertEqual(stats["days_with_data"], 5)
        self.assertEqual(stats["avg_clicks"], 10)
        self.assertEqual(stats["avg_impressions"], 100)
        self.assertEqual(stats["avg_position"], 4.0)

    def test_rows_outside_the_window_are_excluded(self):
        rows = _days(date(2026, 1, 1), 10, clicks=10)
        stats = gsc_window_stats(rows, date(2026, 1, 3), date(2026, 1, 5))
        self.assertEqual(stats["days_with_data"], 3)

    def test_no_data_in_window_returns_none_metrics(self):
        stats = gsc_window_stats([], date(2026, 1, 1), date(2026, 1, 28))
        self.assertEqual(stats["days_with_data"], 0)
        self.assertIsNone(stats["avg_clicks"])
        self.assertIsNone(stats["avg_position"])

    def test_position_none_on_some_rows_is_excluded_from_the_average_not_treated_as_zero(self):
        rows = [_daily_row(date(2026, 1, 1), position=10.0), _daily_row(date(2026, 1, 2), position=None)]
        stats = gsc_window_stats(rows, date(2026, 1, 1), date(2026, 1, 2))
        self.assertEqual(stats["days_with_data"], 2)   # both days have click/impression data
        self.assertEqual(stats["avg_position"], 10.0)   # only the one row with a position counts


class TestBuildChangeWindows(unittest.TestCase):
    def test_full_before_and_after_windows_are_sufficient(self):
        event_date = date(2026, 2, 1)
        rows = _days(event_date - timedelta(days=28), 56)   # 28 before + 28 after, no gaps
        windows = build_change_windows(event_date, rows)
        self.assertEqual(windows["before"]["days_with_data"], 28)
        self.assertEqual(windows["after"]["days_with_data"], 28)
        self.assertFalse(windows["insufficient_data"])

    def test_a_gappy_before_window_is_insufficient(self):
        event_date = date(2026, 2, 1)
        rows = _days(event_date - timedelta(days=10), 10) + _days(event_date, 28)   # only 10 days before
        windows = build_change_windows(event_date, rows)
        self.assertEqual(windows["before"]["days_with_data"], 10)
        self.assertTrue(windows["insufficient_data"])

    def test_a_gappy_after_window_is_insufficient(self):
        event_date = date(2026, 2, 1)
        rows = _days(event_date - timedelta(days=28), 28) + _days(event_date, 5)   # only 5 days after
        windows = build_change_windows(event_date, rows)
        self.assertEqual(windows["after"]["days_with_data"], 5)
        self.assertTrue(windows["insufficient_data"])

    def test_no_gsc_data_at_all_is_insufficient_not_an_error(self):
        windows = build_change_windows(date(2026, 2, 1), [])
        self.assertTrue(windows["insufficient_data"])
        self.assertEqual(windows["before"]["days_with_data"], 0)
        self.assertEqual(windows["after"]["days_with_data"], 0)

    def test_before_and_after_do_not_overlap(self):
        """The change day itself belongs to 'after', never double-counted in 'before'."""
        event_date = date(2026, 2, 1)
        rows = _days(event_date - timedelta(days=1), 2)   # exactly Jan 31 and Feb 1
        windows = build_change_windows(event_date, rows)
        self.assertEqual(windows["before"]["days_with_data"], 1)   # Jan 31 only
        self.assertEqual(windows["after"]["days_with_data"], 1)    # Feb 1 only


class TestBuildTimeline(unittest.TestCase):
    def test_a_url_never_connected_to_gsc_still_reports_its_changes(self):
        events = [{"date": date(2026, 3, 1), "changes": [{"kind": "title_changed"}]}]
        timeline = build_timeline(events, [])
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["changes"], [{"kind": "title_changed"}])
        self.assertTrue(timeline[0]["insufficient_data"])

    def test_multiple_events_each_get_their_own_windows(self):
        d1, d2 = date(2026, 1, 1), date(2026, 3, 1)
        rows = _days(d1 - timedelta(days=28), 28) + _days(d1, 28) + _days(d2 - timedelta(days=28), 28) + _days(d2, 28)
        events = [
            {"date": d1, "changes": [{"kind": "h1_changed"}]},
            {"date": d2, "changes": [{"kind": "word_count_dropped"}]},
        ]
        timeline = build_timeline(events, rows)
        self.assertEqual(len(timeline), 2)
        self.assertEqual(timeline[0]["date"], d1.isoformat())
        self.assertEqual(timeline[1]["date"], d2.isoformat())
        self.assertFalse(timeline[0]["insufficient_data"])
        self.assertFalse(timeline[1]["insufficient_data"])


class TestBuildAppliedActionMarkers(unittest.TestCase):
    """Pasul 18: applied action_cards shown as their own kind of marker."""

    def test_a_marker_carries_source_and_description_alongside_its_gsc_window(self):
        actions = [{"date": date(2026, 3, 1), "source": "decay", "description": "Refreshed the guide"}]
        markers = build_applied_action_markers(actions, [])
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["source"], "decay")
        self.assertEqual(markers[0]["description"], "Refreshed the guide")
        self.assertTrue(markers[0]["insufficient_data"])   # no GSC rows at all

    def test_multiple_actions_each_get_their_own_windows(self):
        d1, d2 = date(2026, 1, 1), date(2026, 3, 1)
        rows = _days(d1 - timedelta(days=28), 28) + _days(d1, 28) + _days(d2 - timedelta(days=28), 28) + _days(d2, 28)
        actions = [
            {"date": d1, "source": "internal_link", "description": "Linked from /related"},
            {"date": d2, "source": "gsc_opportunity", "description": "Added FAQ schema"},
        ]
        markers = build_applied_action_markers(actions, rows)
        self.assertEqual(len(markers), 2)
        self.assertFalse(markers[0]["insufficient_data"])
        self.assertFalse(markers[1]["insufficient_data"])

    def test_no_applied_actions_is_an_empty_list_not_an_error(self):
        self.assertEqual(build_applied_action_markers([], []), [])


if __name__ == "__main__":
    unittest.main()
