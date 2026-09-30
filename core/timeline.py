"""
Per-URL timeline aggregation -- Pasul 10 of docs/superpowers/plans/2026-09-30-next-steps.md.

Pure logic, independent of the database, so it can be tested against
synthetic rows instead of seeding four real tables per scenario.
api/routes/timeline.py loads the actual rows and calls into this module.

"After" a change describes the following window; it is never a claim of
"because of" -- seasonality and unrelated ranking-algorithm changes are not
controlled for here.
"""
from datetime import date, timedelta
from typing import List, Optional

WINDOW_DAYS = 28
MIN_DAYS_FOR_A_WINDOW = 14


def gsc_window_stats(daily_rows: List[dict], start: date, end: date) -> dict:
    """
    Average clicks/impressions/position over GSC daily rows (each a dict with
    a "period_start" ISO date string) whose period_start falls in [start, end]
    (inclusive). days_with_data is the actual count measured, not the size of
    the window -- a gappy window is visible, not silently averaged over as if
    it were complete. avg_position is None if no row in the window has a
    position (impressions with no ranking recorded).
    """
    start_s, end_s = start.isoformat(), end.isoformat()
    in_window = [r for r in daily_rows if start_s <= r["period_start"] <= end_s]
    days_with_data = len(in_window)
    if not days_with_data:
        return {"days_with_data": 0, "avg_clicks": None, "avg_impressions": None, "avg_position": None}

    positions = [r["position"] for r in in_window if r.get("position") is not None]
    return {
        "days_with_data": days_with_data,
        "avg_clicks": round(sum(r["clicks"] for r in in_window) / days_with_data, 2),
        "avg_impressions": round(sum(r["impressions"] for r in in_window) / days_with_data, 2),
        "avg_position": round(sum(positions) / len(positions), 2) if positions else None,
    }


def build_change_windows(event_date: date, daily_rows: List[dict]) -> dict:
    """
    before = the WINDOW_DAYS days up to (not including) event_date.
    after  = event_date and the following WINDOW_DAYS - 1 days.

    insufficient_data is True whenever EITHER window has fewer than
    MIN_DAYS_FOR_A_WINDOW days of actual GSC data -- an average computed on a
    handful of days is not a percentage worth reporting.
    """
    before = gsc_window_stats(daily_rows, event_date - timedelta(days=WINDOW_DAYS), event_date - timedelta(days=1))
    after = gsc_window_stats(daily_rows, event_date, event_date + timedelta(days=WINDOW_DAYS - 1))
    insufficient = (before["days_with_data"] < MIN_DAYS_FOR_A_WINDOW
                   or after["days_with_data"] < MIN_DAYS_FOR_A_WINDOW)
    return {"before": before, "after": after, "insufficient_data": insufficient}


def build_timeline(change_events: List[dict], gsc_daily_rows: List[dict]) -> List[dict]:
    """
    Attach before/after GSC windows to each detected content change.

    change_events: [{"date": date, "changes": [...core.page_diff output...]}],
    already ordered and already filtered to events with real changes.
    gsc_daily_rows: [{"period_start": "YYYY-MM-DD", "clicks", "impressions", "position"}].
    An empty gsc_daily_rows still returns one timeline entry per change, each
    reporting days_with_data=0 / insufficient_data=True rather than raising --
    a URL never connected to GSC still shows what changed on the page.
    """
    timeline = []
    for event in change_events:
        windows = build_change_windows(event["date"], gsc_daily_rows)
        timeline.append({
            "date": event["date"].isoformat(),
            "changes": event["changes"],
            **windows,
        })
    return timeline


def build_applied_action_markers(applied_actions: List[dict], gsc_daily_rows: List[dict]) -> List[dict]:
    """
    Pasul 18: the same before/after GSC window as build_timeline, but for
    action_cards marked applied (api/routes/action_cards.py's PATCH
    .../apply) instead of a detected page-content change -- a distinct kind
    of marker the UI places alongside changes_timeline, not merged into it,
    since "the user applied recommendation X" and "the page's own content
    changed" are different events worth telling apart on the same axis.

    applied_actions: [{"date": date, "source": str, "description": str}, ...].
    """
    markers = []
    for action in applied_actions:
        windows = build_change_windows(action["date"], gsc_daily_rows)
        markers.append({
            "date": action["date"].isoformat(),
            "source": action["source"],
            "description": action["description"],
            **windows,
        })
    return markers
