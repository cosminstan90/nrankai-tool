"""
GSC opportunities: striking distance and weak CTR -- Pasul 13 of
docs/superpowers/plans/2026-09-30-next-steps.md.

Two of the fastest real SEO wins: pages close to page 1 for a real query
("striking distance"), and pages whose click-through rate is below what
this property normally gets at that position ("weak CTR") -- almost always
a title/meta problem, the cheapest fix available in SEO.

Deviates from the original plan text in one way, found while building this:
gsc_page_history / gsc_query_history (Pasul 2) each accumulate ONE GSC
dimension (page, or query) daily -- neither stores the (page, query) PAIR
dimension striking distance needs, and no table anywhere in this project
does. api/routes/gsc/optimizer.py's cannibalization detector hits the exact
same wall and fetches live for the same reason. So:
  - weak_ctr uses gsc_page_history (real accumulated history, Pasul 2)
  - striking_distance needs a live GSC dimensions=[page,query] fetch, done
    by api/routes/gsc_opportunities.py exactly like detect_cannibalization
    already does. This module only takes rows already fetched, so both
    functions stay pure and testable with synthetic data -- no DB, no network.

The expected-CTR-by-position curve is built from the property's OWN data
(median CTR per rounded position), never a curve found online: CTR at a
given position varies hugely by niche and by whether AI Overviews or other
SERP features are already eating clicks for that query set.
"""
from collections import defaultdict
from statistics import median
from typing import List, Optional

MIN_IMPRESSIONS_28D = 100
STRIKING_DISTANCE_MIN_POSITION = 4.0
STRIKING_DISTANCE_MAX_POSITION = 15.0
STRIKING_DISTANCE_TARGET_POSITION = 3.0
MIN_CURVE_BUCKETS = 3   # fewer distinct positions measured than this -- not a curve, noise


def _is_brand_query(query: str, brand_terms: Optional[List[str]]) -> bool:
    if not brand_terms or not query:
        return False
    q = query.lower()
    return any(term.lower() in q for term in brand_terms)


def build_ctr_curve(rows: List[dict]) -> dict:
    """
    Median CTR per rounded position, from this property's own data.
    rows: [{"position": float, "ctr": float, ...}, ...] (extra keys ignored).
    Returns {rounded_position: median_ctr}. Rows with a missing position or
    ctr are skipped, not treated as ctr=0.
    """
    buckets = defaultdict(list)
    for r in rows:
        position, ctr = r.get("position"), r.get("ctr")
        if position is None or ctr is None:
            continue
        buckets[round(position)].append(ctr)
    return {pos: median(values) for pos, values in buckets.items()}


def expected_ctr_at(curve: dict, position: float) -> Optional[float]:
    """The curve's value at `position`'s rounded bucket, or its nearest measured bucket."""
    if not curve:
        return None
    rounded = round(position)
    if rounded in curve:
        return curve[rounded]
    nearest = min(curve.keys(), key=lambda p: abs(p - rounded))
    return curve[nearest]


def _actual_ctr(row: dict) -> float:
    if row.get("ctr") is not None:
        return row["ctr"]
    impressions = row.get("impressions", 0)
    return (row.get("clicks", 0) / impressions) if impressions else 0.0


def find_weak_ctr_pages(page_rows: List[dict], brand_terms: Optional[List[str]] = None,
                        min_impressions: int = MIN_IMPRESSIONS_28D) -> dict:
    """
    page_rows: one row per page, already aggregated over the desired window
    (the caller decides the window -- typically the last 28 days):
    {"page": str, "clicks": int, "impressions": int, "position": float,
     "ctr": Optional[float], "query": Optional[str]}.
    `query` is optional and only used to apply brand-term exclusion when a
    caller happens to have a single representative query per page; most
    callers won't and can omit it.

    Pages below min_impressions are excluded outright -- not enough traffic
    for a CTR problem to matter yet, never scored as a 0-value opportunity.
    """
    eligible = [r for r in page_rows if r.get("impressions", 0) >= min_impressions
               and not _is_brand_query(r.get("query") or "", brand_terms)]

    curve = build_ctr_curve(eligible)
    if len(curve) < MIN_CURVE_BUCKETS:
        return {"curve_pages": len(eligible), "curve_buckets": len(curve),
               "opportunities": [], "insufficient_data": True}

    opportunities = []
    for r in eligible:
        position, impressions = r.get("position"), r.get("impressions", 0)
        if position is None or not impressions:
            continue
        actual_ctr = _actual_ctr(r)
        expected = expected_ctr_at(curve, position)
        if expected is None or actual_ctr >= expected:
            continue
        opportunities.append({
            "page": r["page"], "position": round(position, 1), "impressions": impressions,
            "actual_ctr": round(actual_ctr, 4), "expected_ctr": round(expected, 4),
            "estimated_extra_clicks": round(impressions * (expected - actual_ctr), 1),
            "assumption": (
                f"assumes this page would get the {round(expected * 100, 1)}% CTR this "
                f"property's other pages average at position ~{round(position)}"
            ),
        })

    opportunities.sort(key=lambda o: -o["estimated_extra_clicks"])
    return {"curve_pages": len(eligible), "curve_buckets": len(curve),
           "opportunities": opportunities, "insufficient_data": False}


def find_striking_distance(page_query_rows: List[dict], brand_terms: Optional[List[str]] = None,
                           min_impressions: int = MIN_IMPRESSIONS_28D,
                           min_position: float = STRIKING_DISTANCE_MIN_POSITION,
                           max_position: float = STRIKING_DISTANCE_MAX_POSITION,
                           target_position: float = STRIKING_DISTANCE_TARGET_POSITION) -> dict:
    """
    page_query_rows: one row per (page, query) pair, already limited to the
    desired window (the caller decides -- typically the last 28 days) by a
    live GSC dimensions=[page,query] fetch (see module docstring for why this
    isn't read from accumulated history):
    {"page": str, "query": str, "clicks": int, "impressions": int,
     "position": float, "ctr": Optional[float]}.
    """
    curve = build_ctr_curve(page_query_rows)
    curve_ok = len(curve) >= MIN_CURVE_BUCKETS

    opportunities = []
    for r in page_query_rows:
        position, impressions = r.get("position"), r.get("impressions", 0)
        if position is None:
            continue
        if not (min_position <= position <= max_position):
            continue
        if impressions < min_impressions:
            continue
        if _is_brand_query(r.get("query", ""), brand_terms):
            continue

        actual_ctr = _actual_ctr(r)
        expected = expected_ctr_at(curve, target_position) if curve_ok else None
        estimated_gain = assumption = None
        if expected is not None:
            estimated_gain = round(impressions * max(0.0, expected - actual_ctr), 1)
            assumption = (
                f"assumes reaching position ~{target_position:g} would earn the "
                f"{round(expected * 100, 1)}% CTR this property's queries average there"
            )

        opportunities.append({
            "page": r["page"], "query": r["query"], "position": round(position, 1),
            "impressions": impressions, "actual_ctr": round(actual_ctr, 4),
            "estimated_extra_clicks": estimated_gain, "assumption": assumption,
        })

    # Ranked opportunities (a real gain estimate) first, worst-unknown last --
    # never sort an unscored row as if it were a confirmed 0.
    opportunities.sort(key=lambda o: (o["estimated_extra_clicks"] is None,
                                      -(o["estimated_extra_clicks"] or 0)))
    return {"opportunities": opportunities, "curve_buckets": len(curve), "insufficient_data": not curve_ok}
