"""
Internal link suggestions -- Pasul 14 of docs/superpowers/plans/2026-09-30-next-steps.md.

Ties together the link graph (crawl_pages/crawl_links, Etapa 5) and GSC
opportunities (Pasul 13): a page close to page 1 for a real query, or one
that's simply orphaned, but starved of body-content internal links, is
exactly the kind of page a few added links could push further -- nothing
connected these two existing pieces before this.

Pure aggregation, no DB/network access, so it's testable against a
synthetic crawl. Topical similarity between two pages comes from
core/embeddings.py, computed by the caller and passed in as a plain
{url: vector} map -- this module only does the matching/ranking once
vectors exist.
"""
from typing import Dict, List, Optional, Set, Tuple

from core.embeddings import cosine_similarity

MIN_CONTENT_INLINKS_FOR_A_TARGET = 2   # pages with this few body-content links are starved
# Calibrated against 3 real ing.ro pages (text-embedding-3-small), not chosen
# from nothing: two pages in the same financial-education section scored
# 0.85; that same pair against an unrelated "contact us" page scored
# 0.76-0.79. text-embedding-3-small's cosine similarities cluster in a
# narrow high band for full-page prose, so 0.75 (the first guess) let both
# unrelated comparisons through. 0.80 excludes them on this one real
# example -- still only 3 data points, worth widening once more real pages
# are embedded, not a rigorously calibrated cutoff.
MIN_SIMILARITY = 0.80
MAX_SOURCES_PER_TARGET = 5


def _is_indexable_and_live(page: dict) -> bool:
    # Case-insensitive: Screaming Frog exports "Indexable"/"Non-Indexable"
    # (CrawlPage.indexability), but callers building this dict from other
    # sources may reasonably use lowercase -- comparing case-insensitively
    # here means every caller doesn't have to get that normalization right.
    indexability = page.get("indexability")
    if indexability is not None and indexability.lower() != "indexable":
        return False
    status = page.get("status_code")
    if status is not None and status >= 400:
        return False
    return True


def find_link_targets(pages: List[dict], opportunity_urls: Optional[Set[str]] = None) -> List[dict]:
    """
    Which pages are worth pushing with more internal links: orphaned pages,
    plus pages with too few content_inlinks (navigation links don't count --
    see CrawlPage.content_inlinks). Restricted to `opportunity_urls` (from
    Pasul 13's striking-distance/weak-CTR results) when given, since a
    starved page nobody searches for isn't worth the effort of linking to.

    pages: [{"url", "content_inlinks", "is_orphan", "indexability", "status_code"}, ...]

    Orphaned pages are sorted first -- no path to the page at all is a more
    urgent problem than merely few links -- then by fewest content_inlinks.
    """
    targets = []
    for p in pages:
        if not _is_indexable_and_live(p):
            continue
        if opportunity_urls is not None and p["url"] not in opportunity_urls:
            continue
        is_orphan = bool(p.get("is_orphan"))
        content_inlinks = p.get("content_inlinks") or 0
        starved = content_inlinks < MIN_CONTENT_INLINKS_FOR_A_TARGET
        if is_orphan or starved:
            targets.append({**p, "is_orphan": is_orphan, "content_inlinks": content_inlinks})

    targets.sort(key=lambda t: (not t["is_orphan"], t["content_inlinks"]))
    return targets


def find_candidate_sources(target_url: str, target_vector: List[float],
                           pages: List[dict], vectors: Dict[str, List[float]],
                           existing_links: Set[Tuple[str, str]],
                           min_similarity: float = MIN_SIMILARITY,
                           max_sources: int = MAX_SOURCES_PER_TARGET) -> List[dict]:
    """
    Indexable, live pages topically close to target_url that don't already
    link to it. pages: same shape as find_link_targets's input.
    existing_links: {(source_url, dest_url), ...} already in crawl_links.
    """
    candidates = []
    for p in pages:
        url = p["url"]
        if url == target_url:
            continue
        if not _is_indexable_and_live(p):
            continue
        if (url, target_url) in existing_links:
            continue
        vector = vectors.get(url)
        if vector is None:
            continue
        similarity = cosine_similarity(vector, target_vector)
        if similarity < min_similarity:
            continue
        candidates.append({"url": url, "similarity": round(similarity, 4)})

    candidates.sort(key=lambda c: -c["similarity"])
    return candidates[:max_sources]


def suggest_anchor(target_queries: List[dict]) -> Optional[dict]:
    """
    The real GSC query with the most impressions for the target page --
    never invented text. target_queries: [{"query": str, "impressions": int}, ...].
    None when there's no real query data for this page at all.
    """
    if not target_queries:
        return None
    best = max(target_queries, key=lambda q: q.get("impressions", 0))
    return {"anchor": best["query"], "impressions": best.get("impressions", 0)}


def build_suggestions(targets: List[dict], pages: List[dict], vectors: Dict[str, List[float]],
                      existing_links: Set[Tuple[str, str]], queries_by_url: Dict[str, List[dict]],
                      min_similarity: float = MIN_SIMILARITY,
                      max_sources: int = MAX_SOURCES_PER_TARGET) -> List[dict]:
    """
    One suggestion per target (targets already ordered by find_link_targets:
    orphans first), each with its candidate sources and a real-query anchor.
    A target with zero qualifying sources is left out entirely -- an empty
    suggestion helps no one.
    """
    suggestions = []
    for target in targets:
        target_url = target["url"]
        target_vector = vectors.get(target_url)
        sources = (
            find_candidate_sources(target_url, target_vector, pages, vectors, existing_links,
                                   min_similarity=min_similarity, max_sources=max_sources)
            if target_vector is not None else []
        )
        if not sources:
            continue

        anchor_info = suggest_anchor(queries_by_url.get(target_url, []))
        if target["is_orphan"]:
            reason = "orphaned -- no internal links point to it"
        else:
            reason = f"only {target['content_inlinks']} content link(s) point to it"

        suggestions.append({
            "target_url": target_url,
            "target_content_inlinks": target["content_inlinks"],
            "target_is_orphan": target["is_orphan"],
            "reason": reason,
            "anchor": anchor_info["anchor"] if anchor_info else None,
            "anchor_impressions": anchor_info["impressions"] if anchor_info else None,
            "candidate_sources": sources,
        })
    return suggestions
