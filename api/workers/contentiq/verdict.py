"""KUCD Verdict Engine — combines 4 scores into KEEP/UPDATE/CONSOLIDATE/DELETE."""
from typing import Tuple

from api.workers.contentiq.engines import (
    score_freshness, score_geo, score_eeat, score_seo_health,
)


def assign_verdict(page: dict) -> Tuple[str, str]:
    """Assign KUCD verdict based on page scores. Returns (verdict, reason)."""
    sf  = page.get("score_freshness")  or 0
    sg  = page.get("score_geo")        or 0
    se  = page.get("score_eeat")       or 0
    ssh = page.get("score_seo_health") or 0
    st  = page.get("score_total")      or 0

    has_traffic = (page.get("gsc_clicks") or 0) + (page.get("ahrefs_traffic") or 0) > 50
    has_links   = (page.get("ahrefs_backlinks") or 0) >= 3
    wc          = page.get("word_count") or 0

    # Rule 1: DELETE
    if st < 20 and not has_traffic and not has_links and wc < 150:
        return "DELETE", "Very low scores, no traffic, no backlinks, thin content."

    # Rule 2: CONSOLIDATE (low scores, no traffic)
    if st < 35 and not has_traffic:
        return "CONSOLIDATE", "Low scores and no meaningful traffic. Merge with stronger related content."

    # Rule 3: CONSOLIDATE (poor SEO health, no authority)
    if st < 40 and ssh < 30 and not has_links:
        return "CONSOLIDATE", "Poor SEO health, no authority signals. Consolidation candidate."

    # Rule 4: UPDATE (traffic but underperforming)
    if has_traffic and st < 50:
        return "UPDATE", "Has traffic but underperforming scores — update to protect rankings."

    # Rule 5: UPDATE (moderate scores or stale)
    if 35 <= st < 65:
        return "UPDATE", "Decent potential but needs improvement across dimensions."
    if st >= 65 and sf < 25:
        return "UPDATE", "Good scores but content is stale — refresh to maintain rankings."

    # Rule 6: KEEP
    if st >= 65 and sf >= 40:
        return "KEEP", "Strong scores across all dimensions. Performing well."

    # Fallback
    return "UPDATE", "Mixed signals — review manually."


def score_and_verdict(page: dict) -> dict:
    """Run all 4 engines + assign verdict. Returns updated page dict."""
    p = dict(page)

    p["score_freshness"],  p["freshness_reason"]   = score_freshness(p)
    p["score_geo"],        p["geo_reason"]          = score_geo(p)
    p["score_eeat"],       p["eeat_reason"]         = score_eeat(p)
    p["score_seo_health"], p["seo_health_reason"]   = score_seo_health(p)

    p["score_total"] = round(
        p["score_freshness"]  * 0.30 +
        p["score_geo"]        * 0.25 +
        p["score_eeat"]       * 0.25 +
        p["score_seo_health"] * 0.20
    )
    p["verdict"], p["verdict_reason"] = assign_verdict(p)
    return p


def batch_score_and_verdict(pages: list) -> list:
    return [score_and_verdict(p) for p in pages]
