"""
Bucla de invatare: actiune aplicata -> efect masurat -- Pasul 18 of
docs/superpowers/plans/2026-09-30-next-steps.md.

Pure logic, independent of the database -- api/routes/action_cards.py loads
the actual ActionCard rows (and, for the "current" side of each comparison,
gsc_page_history) and calls into this module.

"After" an action was applied, never "because of" it -- seasonality and
unrelated ranking changes are not controlled for here. A source with fewer
than MIN_SAMPLE_FOR_CONCLUSIONS eligible (applied >= MIN_DAYS_BEFORE_REPORT
days ago, with a measurable current value) actions reports
insufficient_data instead of a percentage that would look more confident
than it is.
"""
from datetime import datetime
from typing import Dict, List, Optional

MIN_DAYS_BEFORE_REPORT = 28
MIN_SAMPLE_FOR_CONCLUSIONS = 5

# A small dead zone around "no change" -- floating-point/rounding noise in a
# metric like avg_position (e.g. 4.02 vs 4.00) should not read as "worse".
_RELATIVE_NOISE_FLOOR = 0.02


def classify_effect(baseline_value: Optional[float], current_value: Optional[float],
                     higher_is_better: bool) -> str:
    """improved | worse | unchanged | insufficient_data (either value missing)."""
    if baseline_value is None or current_value is None:
        return "insufficient_data"
    if baseline_value == 0:
        delta = current_value - baseline_value
    else:
        delta = (current_value - baseline_value) / abs(baseline_value)
    if abs(delta) <= _RELATIVE_NOISE_FLOOR:
        return "unchanged"
    improved = delta > 0 if higher_is_better else delta < 0
    return "improved" if improved else "worse"


def is_eligible_for_report(applied_at: Optional[datetime], as_of: datetime,
                            min_days: int = MIN_DAYS_BEFORE_REPORT) -> bool:
    """An action needs at least min_days of "after" data to say anything."""
    if applied_at is None:
        return False
    return (as_of - applied_at).days >= min_days


def build_learning_report(applied_actions: List[dict], as_of: datetime,
                           min_days: int = MIN_DAYS_BEFORE_REPORT,
                           min_sample: int = MIN_SAMPLE_FOR_CONCLUSIONS) -> Dict[str, dict]:
    """
    applied_actions: [{
        "source": str, "page_url": str, "applied_at": datetime,
        "metric": Optional[str], "baseline_value": Optional[float],
        "current_value": Optional[float], "higher_is_better": bool,
    }, ...]

    Groups by source. Within a source, only actions eligible per
    is_eligible_for_report count toward min_sample; below that threshold the
    source reports "insufficient_data" with the count so far, not a
    conclusion. current_value missing (metric not re-measured since) always
    classifies as insufficient_data for that one action, and does not by
    itself block the group -- other actions in the same source may still
    have enough data.
    """
    by_source: Dict[str, List[dict]] = {}
    for action in applied_actions:
        by_source.setdefault(action["source"], []).append(action)

    report: Dict[str, dict] = {}
    for source, actions in by_source.items():
        eligible = [a for a in actions if is_eligible_for_report(a.get("applied_at"), as_of, min_days)]
        if len(eligible) < min_sample:
            report[source] = {
                "status": "insufficient_data",
                "eligible_count": len(eligible),
                "min_sample_required": min_sample,
            }
            continue

        classified = [
            classify_effect(a.get("baseline_value"), a.get("current_value"), a.get("higher_is_better", True))
            for a in eligible
        ]
        measured = [c for c in classified if c != "insufficient_data"]
        report[source] = {
            "status": "ok",
            "eligible_count": len(eligible),
            "measured_count": len(measured),
            "improved": classified.count("improved"),
            "worse": classified.count("worse"),
            "unchanged": classified.count("unchanged"),
            "not_yet_remeasured": classified.count("insufficient_data"),
        }
    return report
