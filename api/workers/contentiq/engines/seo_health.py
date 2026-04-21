"""SEO Health scoring engine (0–100)."""
from typing import Tuple


def score_seo_health(page: dict) -> Tuple[int, str]:
    """Score on-page SEO health. Returns (score, reason)."""
    title        = page.get("title") or ""
    h1           = page.get("h1") or ""
    meta_desc    = page.get("meta_description") or ""
    word_count   = page.get("word_count") or 0
    canonical    = page.get("canonical") or ""
    url          = page.get("url") or ""
    status_code  = page.get("status_code") or 0

    notes = []

    # Title (0–25)
    t_score = 0
    if title:
        t_score += 15; notes.append("Title present")
        if 40 <= len(title) <= 65:
            t_score += 10; notes.append("Title length optimal")
        else:
            notes.append(f"Title length off ({len(title)} chars)")
    else:
        notes.append("Title missing")

    # Meta description (0–20)
    m_score = 0
    if meta_desc:
        m_score += 10; notes.append("Meta desc present")
        if 100 <= len(meta_desc) <= 160:
            m_score += 10; notes.append("Meta desc length optimal")
    else:
        notes.append("Meta desc missing")

    # H1 (0–20)
    h_score = 0
    if h1:
        h_score += 10; notes.append("H1 present")
        if len(h1) >= 10:
            h_score += 10
    else:
        notes.append("H1 missing")

    # Canonical (0–10)
    if canonical:
        c_score = 10 if canonical.rstrip("/") == url.rstrip("/") else 0
    else:
        c_score = 5  # ambiguous

    # Content length (0–15)
    if word_count >= 600:   wc_score = 15
    elif word_count >= 300: wc_score = 10
    elif word_count >= 100: wc_score = 5
    else:                   wc_score = 0

    # Status code (0–10)
    ok_score = 10 if status_code == 200 else 0

    score  = min(100, t_score + m_score + h_score + c_score + wc_score + ok_score)
    reason = ". ".join(notes[:4]) + "."
    return score, reason
