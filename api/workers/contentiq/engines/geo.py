"""GEO Visibility scoring engine (0–100)."""
from typing import Tuple

QUESTION_WORDS = {"ce", "cum", "când", "de", "care", "what", "how", "when", "why", "which", "where", "who"}


def score_geo(page: dict) -> Tuple[int, str]:
    """Score GEO visibility. Returns (score, reason)."""
    word_count     = page.get("word_count") or 0
    title          = page.get("title") or ""
    h1             = page.get("h1") or ""
    meta_desc      = page.get("meta_description") or ""
    ahrefs_kw      = page.get("ahrefs_keywords") or 0
    gsc_position   = page.get("gsc_position") or 99

    # Structure (0–30)
    struct = 0
    if word_count >= 800:                        struct += 10
    if meta_desc and len(meta_desc) > 80:        struct += 10
    if h1 and 20 < len(h1) < 100:               struct += 10

    # Question/intent signals (0–25)
    intent = 0
    combined_text = (title + " " + h1).lower()
    if any(w in combined_text.split() for w in QUESTION_WORDS): intent += 15
    if word_count >= 1200:                                       intent += 10

    # SERP position bonus (0–25)
    if gsc_position <= 3:    serp = 25
    elif gsc_position <= 10: serp = 15
    elif gsc_position <= 20: serp = 8
    else:                    serp = 0

    # Keyword breadth (0–20)
    if ahrefs_kw >= 50:   kw_score = 20
    elif ahrefs_kw >= 20: kw_score = 12
    elif ahrefs_kw >= 5:  kw_score = 6
    else:                  kw_score = 0

    score = min(100, struct + intent + serp + kw_score)
    pos_str = f"{gsc_position:.1f}" if gsc_position < 99 else "unknown"
    reason = (
        f"Ranks at position {pos_str}. "
        f"{'Answers question intent. ' if intent >= 15 else 'No clear question intent. '}"
        f"{ahrefs_kw} keywords. "
        f"{'Good structure.' if struct >= 20 else 'Needs better structure.'}"
    )
    return score, reason
