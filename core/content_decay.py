"""
Content decay detection -- Pasul 15 of docs/superpowers/plans/2026-09-30-next-steps.md.

A page losing traffic gradually is the cheapest place to recover it --
ContentIQ's UPDATE verdict (api/workers/contentiq/verdict.py) used no real
trend at all before this. Needs gsc_page_history (Pasul 2) with enough
accumulated weeks; a fresh install, or a property disconnected before this
much history built up, reports insufficient_history rather than "no decay" --
those are different claims.

Pure aggregation, no DB access -- tested against synthetic weekly series, as
the plan asks for.
"""
from datetime import date, timedelta
from typing import List, Optional

MIN_WEEKS_FOR_TREND = 12
MIN_WEEKS_FOR_YOY_COMPARISON = 52
DECAY_DROP_FROM_PEAK_THRESHOLD = 0.30       # 30% below the page's own peak week
CONSISTENCY_MIN_DECLINING_FRACTION = 0.7    # of week-over-week deltas in the trend window
POSITION_WORSENED_THRESHOLD = 1.0           # avg position points, early vs late half of the window
CTR_DROP_THRESHOLD = 0.20                   # relative drop, early vs late half


def build_weekly_series(daily_rows: List[dict]) -> List[dict]:
    """
    daily_rows: [{"period_start": "YYYY-MM-DD", "clicks", "impressions",
    "position"}, ...] for one page, any order, gaps allowed.

    Buckets into non-overlapping 7-day windows anchored on the MOST RECENT
    day present -- "this week" always means the last 7 days of real data,
    not a calendar week that might be cut mid-way by wherever the data
    happens to start or end. Weeks with zero rows (a total gap) are skipped
    rather than inserted as a fake zero week.

    Returns weeks oldest-first: [{"week_start", "week_end", "clicks",
    "impressions", "position", "ctr"}].
    """
    if not daily_rows:
        return []

    by_date = {}
    for r in daily_rows:
        by_date.setdefault(date.fromisoformat(r["period_start"]), []).append(r)
    all_dates = sorted(by_date.keys())
    most_recent, earliest = all_dates[-1], all_dates[0]

    weeks = []
    window_end = most_recent
    while window_end >= earliest:
        window_start = window_end - timedelta(days=6)
        rows_in_week = [r for d in by_date if window_start <= d <= window_end for r in by_date[d]]
        if rows_in_week:
            clicks = sum(r["clicks"] for r in rows_in_week)
            impressions = sum(r["impressions"] for r in rows_in_week)
            positions = [r["position"] for r in rows_in_week if r.get("position") is not None]
            weeks.append({
                "week_start": window_start.isoformat(), "week_end": window_end.isoformat(),
                "clicks": clicks, "impressions": impressions,
                "position": (sum(positions) / len(positions)) if positions else None,
                "ctr": (clicks / impressions) if impressions else None,
            })
        window_end = window_start - timedelta(days=1)

    weeks.reverse()
    return weeks


def linear_slope(values: List[float]) -> float:
    """Ordinary least squares slope of `values` against their index (0..n-1). 0.0 for fewer than 2 points."""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den else 0.0


def _declining_fraction(values: List[float]) -> float:
    """Fraction of week-over-week deltas that are <= 0 -- tolerant of noise,
    unlike requiring a strictly monotonic decline."""
    if len(values) < 2:
        return 0.0
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    declining = sum(1 for d in deltas if d <= 0)
    return declining / len(deltas)


def _classify_cause(recent_weeks: List[dict]) -> str:
    """
    position_drop | ctr_drop | unclear -- compares the early half of the
    trend window against the late half. Position worsening explains a
    decline by itself (competition/relevance); a stable position with
    falling CTR points at something else eating clicks for the same rank
    (a new SERP feature such as an AI Overview -- see serp_features from
    Pasul 8, not cross-referenced here yet).
    """
    half = len(recent_weeks) // 2
    early, late = recent_weeks[:half], recent_weeks[half:]

    early_positions = [w["position"] for w in early if w.get("position") is not None]
    late_positions = [w["position"] for w in late if w.get("position") is not None]
    if not early_positions or not late_positions:
        return "unclear"

    avg_early_pos = sum(early_positions) / len(early_positions)
    avg_late_pos = sum(late_positions) / len(late_positions)
    if (avg_late_pos - avg_early_pos) >= POSITION_WORSENED_THRESHOLD:
        return "position_drop"

    early_ctrs = [w["ctr"] for w in early if w.get("ctr") is not None]
    late_ctrs = [w["ctr"] for w in late if w.get("ctr") is not None]
    if early_ctrs and late_ctrs:
        avg_early_ctr = sum(early_ctrs) / len(early_ctrs)
        avg_late_ctr = sum(late_ctrs) / len(late_ctrs)
        if avg_early_ctr > 0 and (avg_early_ctr - avg_late_ctr) / avg_early_ctr >= CTR_DROP_THRESHOLD:
            return "ctr_drop"

    return "unclear"


def _check_seasonality(weekly_series: List[dict], trend_window_weeks: int) -> dict:
    """
    Compares the current (decaying) window against the same calendar weeks
    one year earlier, only when at least MIN_WEEKS_FOR_YOY_COMPARISON weeks
    of history exist. Without that much history the seasonality question has
    no answer -- flagged explicitly, never assumed either way.
    """
    if len(weekly_series) < MIN_WEEKS_FOR_YOY_COMPARISON:
        return {"seasonality_checked": False, "yoy_change_pct": None,
               "seasonality_note": "less than 12 months of history -- seasonality uncontrolled"}

    last_year_start = len(weekly_series) - trend_window_weeks - 52
    if last_year_start < 0:
        return {"seasonality_checked": False, "yoy_change_pct": None,
               "seasonality_note": "not enough history at the matching point one year back"}

    current_window = weekly_series[-trend_window_weeks:]
    last_year_window = weekly_series[last_year_start:last_year_start + trend_window_weeks]

    current_avg = sum(w["clicks"] for w in current_window) / len(current_window)
    last_year_avg = sum(w["clicks"] for w in last_year_window) / len(last_year_window)

    if last_year_avg == 0:
        return {"seasonality_checked": True, "yoy_change_pct": None,
               "seasonality_note": "no traffic in the matching period last year either"}

    yoy_change_pct = (current_avg - last_year_avg) / last_year_avg
    if yoy_change_pct >= -0.10:
        note = "last year's same period was similarly low -- likely seasonal, not real decay"
    else:
        note = "last year's same period was NOT similarly low -- likely real decay, not seasonal"
    return {"seasonality_checked": True, "yoy_change_pct": round(yoy_change_pct, 4), "seasonality_note": note}


def detect_decay(weekly_series: List[dict], trend_window_weeks: int = MIN_WEEKS_FOR_TREND) -> dict:
    """
    weekly_series: oldest-first, from build_weekly_series.

    insufficient_history is always present; when True every other key is
    None -- never a confident-looking "no decay" from too little data.
    cause/seasonality are only computed when is_decaying is True: they
    answer "why", which only matters once "whether" is established.
    """
    if len(weekly_series) < trend_window_weeks:
        return {
            "insufficient_history": True, "weeks_available": len(weekly_series),
            "is_decaying": None, "drop_from_peak_pct": None, "slope": None,
            "declining_fraction": None, "cause": None,
            "seasonality_checked": False, "seasonality_note": None, "yoy_change_pct": None,
        }

    recent = weekly_series[-trend_window_weeks:]
    clicks = [w["clicks"] for w in recent]
    slope = linear_slope(clicks)
    declining_fraction = _declining_fraction(clicks)

    peak_clicks = max(w["clicks"] for w in weekly_series)
    smoothing = min(4, len(clicks))
    current_clicks = sum(clicks[-smoothing:]) / smoothing
    drop_from_peak_pct = ((peak_clicks - current_clicks) / peak_clicks) if peak_clicks else 0.0

    is_decaying = (
        slope < 0
        and declining_fraction >= CONSISTENCY_MIN_DECLINING_FRACTION
        and drop_from_peak_pct >= DECAY_DROP_FROM_PEAK_THRESHOLD
    )

    result = {
        "insufficient_history": False, "weeks_available": len(weekly_series),
        "is_decaying": is_decaying, "drop_from_peak_pct": round(drop_from_peak_pct, 4),
        "slope": round(slope, 4), "declining_fraction": round(declining_fraction, 4),
        "cause": None, "seasonality_checked": False, "seasonality_note": None, "yoy_change_pct": None,
    }

    if is_decaying:
        result["cause"] = _classify_cause(recent)
        result.update(_check_seasonality(weekly_series, trend_window_weeks))

    return result
