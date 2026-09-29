"""E-E-A-T scoring engine (0–100)."""
from datetime import date, datetime
from typing import Tuple


def score_eeat(page: dict) -> Tuple[int, str]:
    """Score E-E-A-T signals. Returns (score, reason)."""
    word_count       = page.get("word_count") or 0
    # None = not measured (no Ahrefs key, or the fetch failed). Kept distinct
    # from a measured 0: collapsing them with `or 0` capped every page at 40
    # and wrote "0 backlinks, DR=0" into the reason as if measured.
    raw_backlinks    = page.get("ahrefs_backlinks")
    raw_dr           = page.get("ahrefs_dr")
    authority_known  = raw_backlinks is not None or raw_dr is not None
    ahrefs_backlinks = raw_backlinks or 0
    ahrefs_dr        = raw_dr or 0
    last_modified    = page.get("last_modified")

    # Authority — backlinks (0–35)
    if ahrefs_backlinks >= 50:   bl_score = 35
    elif ahrefs_backlinks >= 20: bl_score = 25
    elif ahrefs_backlinks >= 5:  bl_score = 15
    elif ahrefs_backlinks >= 1:  bl_score = 8
    else:                        bl_score = 0

    # Domain Rating proxy (0–25)
    if ahrefs_dr >= 50:   dr_score = 25
    elif ahrefs_dr >= 30: dr_score = 18
    elif ahrefs_dr >= 20: dr_score = 12
    elif ahrefs_dr >= 10: dr_score = 6
    else:                  dr_score = 0

    # Content depth (0–25)
    if word_count >= 2000:   depth = 25
    elif word_count >= 1000: depth = 18
    elif word_count >= 500:  depth = 10
    else:                    depth = 0

    # Freshness signal (0–15) — same logic as freshness date_score scaled to 15 max
    fresh = 5  # unknown default
    if last_modified:
        try:
            if isinstance(last_modified, str):
                mod_date = datetime.fromisoformat(last_modified.split("T")[0]).date()
            else:
                mod_date = last_modified
            days_old = (date.today() - mod_date).days
            if days_old <= 30:    fresh = 15
            elif days_old <= 90:  fresh = 12
            elif days_old <= 180: fresh = 10
            elif days_old <= 365: fresh = 7
            elif days_old <= 730: fresh = 3
            else:                  fresh = 0
        except Exception:
            pass

    if authority_known:
        score  = min(100, bl_score + dr_score + depth + fresh)
        reason = (
            f"{ahrefs_backlinks} backlinks, DR={ahrefs_dr}. "
            f"Content depth: {word_count} words."
        )
    else:
        # Authority is 60 of the 100 points. Without it, score what was
        # measured (depth 25 + freshness 15) on the same 0-100 scale instead
        # of treating the missing 60 as a measured zero.
        score  = min(100, round((depth + fresh) * 100 / 40))
        reason = (
            "Authority not measured (no backlink data source); score from content "
            f"depth and freshness only. Content depth: {word_count} words."
        )
    return score, reason
