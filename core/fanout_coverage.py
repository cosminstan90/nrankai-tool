"""
Fan-Out sub-query coverage -- Pasul 17 of docs/superpowers/plans/2026-09-30-next-steps.md.

For each sub-query Fan-Out generated, finds the site's own best-matching
passage (core/passages.py + core/embeddings.py cosine similarity) and
classifies coverage: covered / weak / uncovered.

Thresholds calibrated against 4 real (query, passage) pairs -- text-embedding-3-small,
a real ing.ro page chunked by core/passages.py, not chosen from nothing:

  query                                    best similarity   judgment
  "cum aleg creditul potrivit pentru mine"      0.5352        clearly on-topic
  "ce este dobanda variabila la un credit"      0.5146        related, on-topic
  "cum deschid un cont curent la banca"         0.4501        banking-adjacent, off this page's topic
  "retete de prajituri traditionale"            0.3159        clearly unrelated

Query-vs-passage similarity sits in a much lower range than the page-vs-page
comparison core/internal_links.py calibrated for Pasul 14 (0.75-0.85 there) --
a short query and a long prose passage are structurally very different
texts, so the numbers are not comparable across the two use cases.
tests/test_fanout_coverage.py's TestThresholdCalibration reproduces these
four exact similarity values with synthetic vectors (no network) and checks
each still lands where it did for real.
"""
from typing import Dict, List, Optional

from core.embeddings import cosine_similarity

COVERED_THRESHOLD = 0.50
WEAK_THRESHOLD = 0.38


def classify_coverage(similarity: Optional[float]) -> str:
    """covered | weak | uncovered. None (no passage to compare against at all) is uncovered."""
    if similarity is None:
        return "uncovered"
    if similarity >= COVERED_THRESHOLD:
        return "covered"
    if similarity >= WEAK_THRESHOLD:
        return "weak"
    return "uncovered"


def find_best_passage(query_vector: List[float], passages: List[dict]) -> Optional[dict]:
    """
    passages: [{"url": str, "text": str, "vector": [float]}, ...].
    Returns the single best-matching passage (with an added "similarity"
    key), or None when there are no passages to compare against at all --
    that's a distinct case from every passage scoring low.
    """
    if not passages:
        return None
    scored = [{**p, "similarity": cosine_similarity(query_vector, p["vector"])} for p in passages]
    return max(scored, key=lambda p: p["similarity"])


def build_coverage_report(sub_queries: List[dict], passages: List[dict]) -> dict:
    """
    sub_queries: [{"query": str, "vector": [float], "cluster": Optional[str]}, ...]
    passages: [{"url": str, "text": str, "vector": [float]}, ...] (site-wide,
    not per-query -- the same passage list is compared against every query).

    Returns per-query coverage, counts, and uncovered/weak queries grouped by
    cluster (or "uncategorized") for the content-brief follow-up action.
    """
    results = []
    for sq in sub_queries:
        best = find_best_passage(sq["vector"], passages)
        similarity = best["similarity"] if best else None
        results.append({
            "query": sq["query"], "cluster": sq.get("cluster"),
            "status": classify_coverage(similarity),
            "similarity": round(similarity, 4) if similarity is not None else None,
            "closest_url": best["url"] if best else None,
        })

    grouped: Dict[str, List[dict]] = {}
    for r in results:
        if r["status"] in ("uncovered", "weak"):
            grouped.setdefault(r["cluster"] or "uncategorized", []).append(r)

    return {
        "queries": results,
        "covered_count": sum(1 for r in results if r["status"] == "covered"),
        "weak_count": sum(1 for r in results if r["status"] == "weak"),
        "uncovered_count": sum(1 for r in results if r["status"] == "uncovered"),
        "gaps_by_cluster": grouped,
    }
