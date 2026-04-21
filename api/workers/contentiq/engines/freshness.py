"""Freshness scoring engine (0–100)."""
from datetime import date, datetime
from typing import Optional, Tuple


def score_freshness(page: dict) -> Tuple[int, str]:
    """Score content freshness. Returns (score, reason)."""
    last_modified = page.get("last_modified")
    word_count    = page.get("word_count") or 0
    gsc_clicks    = page.get("gsc_clicks") or 0
    ahrefs_traffic = page.get("ahrefs_traffic") or 0

    # Step 1 — Date freshness (0–60)
    date_score = 20  # default: unknown
    date_note  = "Last modified date unknown"
    if last_modified:
        try:
            if isinstance(last_modified, str):
                mod_date = datetime.fromisoformat(last_modified.split("T")[0]).date()
            else:
                mod_date = last_modified
            days_old = (date.today() - mod_date).days
            if days_old <= 30:   date_score, date_note = 60, f"Last updated {days_old} days ago (very fresh)"
            elif days_old <= 90: date_score, date_note = 50, f"Last updated {days_old} days ago (fresh)"
            elif days_old <= 180: date_score, date_note = 40, f"Last updated {days_old} days ago (moderately fresh)"
            elif days_old <= 365: date_score, date_note = 30, f"Last updated {days_old} days ago (aging)"
            elif days_old <= 730: date_score, date_note = 15, f"Last updated {days_old} days ago (stale)"
            else:                 date_score, date_note = 0,  f"Last updated {days_old} days ago (very stale)"
        except Exception:
            pass

    # Step 2 — Traffic signal bonus (0–25)
    combined = gsc_clicks + ahrefs_traffic
    if combined > 500:   traffic_bonus, traffic_note = 25, f"Strong traffic signal ({combined} combined)"
    elif combined > 100: traffic_bonus, traffic_note = 15, f"Good traffic signal ({combined} combined)"
    elif combined > 10:  traffic_bonus, traffic_note = 8,  f"Some traffic ({combined} combined)"
    else:                traffic_bonus, traffic_note = 0,  "No meaningful traffic"

    # Step 3 — Content depth bonus (0–15)
    if word_count > 1500:   depth_bonus, depth_note = 15, f"Rich content ({word_count} words)"
    elif word_count > 800:  depth_bonus, depth_note = 10, f"Good depth ({word_count} words)"
    elif word_count > 300:  depth_bonus, depth_note = 5,  f"Moderate content ({word_count} words)"
    else:                   depth_bonus, depth_note = 0,  f"Thin content ({word_count} words)"

    score  = min(100, date_score + traffic_bonus + depth_bonus)
    reason = f"{date_note}. {traffic_note}. {depth_note}."
    return score, reason


def batch_score(pages: list) -> list:
    result = []
    for p in pages:
        p = dict(p)
        p["score_freshness"], p["freshness_reason"] = score_freshness(p)
        result.append(p)
    return result
