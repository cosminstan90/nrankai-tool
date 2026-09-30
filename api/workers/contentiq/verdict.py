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

    # "No traffic" and "no backlinks" must be MEASURED to count against a
    # page. With no GSC connection and no Ahrefs key these arrive as None, and
    # collapsing that to 0 let the DELETE rule recommend deleting a page on the
    # strength of data nobody had fetched -- with a reason asserting it.
    gsc_clicks     = page.get("gsc_clicks")
    ahrefs_traffic = page.get("ahrefs_traffic")
    backlinks      = page.get("ahrefs_backlinks")
    traffic_known  = gsc_clicks is not None or ahrefs_traffic is not None
    links_known    = backlinks is not None

    has_traffic = (gsc_clicks or 0) + (ahrefs_traffic or 0) > 50
    has_links   = (backlinks or 0) >= 3
    no_traffic  = traffic_known and not has_traffic
    no_links    = links_known and not has_links
    wc          = page.get("word_count") or 0

    # Rule 1: DELETE
    if st < 20 and no_traffic and no_links and wc < 150:
        return "DELETE", "Very low scores, no traffic, no backlinks, thin content."

    # Rule 1b: would have been a DELETE candidate, but the evidence is missing.
    # Say so, rather than letting it fall through to a generic "mixed signals".
    if st < 20 and wc < 150 and not (traffic_known and links_known):
        return "UPDATE", ("Low scores and thin content, but traffic or backlinks were not measured -- "
                          "deletion cannot be justified without them. Review manually.")

    # Rule 2: CONSOLIDATE (low scores, no traffic)
    if st < 35 and no_traffic:
        return "CONSOLIDATE", "Low scores and no meaningful traffic. Merge with stronger related content."

    # Rule 3: CONSOLIDATE (poor SEO health, no authority)
    if st < 40 and ssh < 30 and no_links:
        return "CONSOLIDATE", "Poor SEO health, no authority signals. Consolidation candidate."

    # Rule 4: UPDATE (traffic but underperforming)
    if has_traffic and st < 50:
        return "UPDATE", "Has traffic but underperforming scores — update to protect rankings."

    # Rule 5: UPDATE (moderate scores or stale)
    if 35 <= st < 65:
        return "UPDATE", "Decent potential but needs improvement across dimensions."
    if st >= 65 and sf < 25:
        return "UPDATE", "Good scores but content is stale — refresh to maintain rankings."

    # Rule 5b (Pasul 15 of docs/superpowers/plans/2026-09-30-next-steps.md): a
    # page whose static scores look fine can still be measurably losing real
    # traffic -- that must not read as a clean KEEP. content_decay is None
    # when it was never measured (gsc_page_history too short, or GSC
    # disconnected) -- only a real, measured decay changes the verdict here,
    # matching the "missing != zero" rule the DELETE rule above already follows.
    if page.get("content_decay") is True and st >= 65 and sf >= 40:
        return "UPDATE", ("Static scores look strong, but real traffic is measurably decaying "
                          "(gsc_page_history) -- refresh before rankings slip further.")

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
