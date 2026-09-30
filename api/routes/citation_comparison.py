"""
GET /api/citations/trackers/{tracker_id}/citation-comparison -- Pasul 16 of
docs/superpowers/plans/2026-09-30-next-steps.md.

"Optimize for AI" is generic advice. This measures the pages an AI Overview
or an LLM actually cited for one tracked query and compares them, on
deterministic features (core/citation_features.py), against the tracker's
own best-ranked page for that query. Backend only this session, matching
the Pasul 9/10/12/13/14/15 split -- an optional LLM-phrased recommendation
is available via ?with_recommendation=true, but never runs by default
(a real, paid call).
"""
import json
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Query
from sqlalchemy import select

from api.models.database import (
    AsyncSessionLocal, CitationScan, CitationTracker, SerpAioReference, SerpRankObservation,
)
from api.utils.errors import raise_not_found
from core.citation_comparison import build_recommendation_prompt, compare_to_citations
from core.citation_features import extract_features
from core.citation_fetch import fetch_and_cache

router = APIRouter(prefix="/api/citations", tags=["citations"])

MAX_CITED_PAGES = 10   # real fetches, bounded per request


def _same_site(url: str, website: str) -> bool:
    from core.serp_client import normalize_host
    return normalize_host(url) == normalize_host(website)


async def _own_best_page(db, tracker_id: str, query: str) -> Optional[str]:
    """The tracker's own best (lowest rank_group) observed URL for this query, most recent first."""
    rows = (await db.execute(
        select(SerpRankObservation)
        .where(SerpRankObservation.tracker_id == tracker_id, SerpRankObservation.query == query,
              SerpRankObservation.ranking_url.isnot(None))
        .order_by(SerpRankObservation.observed_at.desc())
    )).scalars().all()
    if not rows:
        return None
    best = min(rows, key=lambda r: (r.rank_group is None, r.rank_group or 0))
    return best.ranking_url


async def _cited_urls_for_query(db, tracker_id: str, query: str, website: str) -> List[str]:
    """
    Distinct URLs actually cited for this query: AI Overview references
    (Pasul 8) and LLM citation sources from the latest completed scan's
    results_json. The tracker's own site is excluded -- these are the pages
    to compare AGAINST, not our own.
    """
    urls = set()

    aio_rows = (await db.execute(
        select(SerpAioReference)
        .join(SerpRankObservation, SerpAioReference.observation_id == SerpRankObservation.id)
        .where(SerpRankObservation.tracker_id == tracker_id, SerpRankObservation.query == query,
              SerpAioReference.url.isnot(None))
    )).scalars().all()
    for r in aio_rows:
        urls.add(r.url)

    latest_scan = (await db.execute(
        select(CitationScan)
        .where(CitationScan.tracker_id == tracker_id, CitationScan.status == "completed")
        .order_by(CitationScan.completed_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if latest_scan and latest_scan.results_json:
        try:
            all_results = json.loads(latest_scan.results_json)
        except (ValueError, TypeError):
            all_results = []
        for entry in all_results:
            if entry.get("query") != query:
                continue
            for provider_result in (entry.get("providers") or {}).values():
                for url in (provider_result.get("cited_urls") or []):
                    urls.add(url)

    return [u for u in urls if u and not _same_site(u, website)]


@router.get("/trackers/{tracker_id}/citation-comparison")
async def get_citation_comparison(
    tracker_id: str,
    query: str = Query(..., min_length=1),
    with_recommendation: bool = Query(False, description="Also generate an LLM-phrased recommendation (a real, paid call)"),
    provider: str = Query("anthropic"),
    model: str = Query("claude-haiku-4-5-20251001"),
):
    """
    Compares the tracker's own best-ranked page for `query` against every
    page actually cited for it (AI Overview references + LLM citation
    sources), on deterministic features only. A page that can't be fetched
    (robots.txt disallows it, or the request fails) is simply excluded from
    the sample -- never scored as lacking every feature.
    """
    async with AsyncSessionLocal() as db:
        tracker = await db.get(CitationTracker, tracker_id)
        if not tracker:
            raise_not_found("Tracker", tracker_id)

        own_url = await _own_best_page(db, tracker_id, query)
        cited_urls = (await _cited_urls_for_query(db, tracker_id, query, tracker.website))[:MAX_CITED_PAGES]

    if not own_url:
        return {"tracker_id": tracker_id, "query": query, "own_url": None,
               "note": "no observed ranking URL for this query yet -- run a scan with google_aio enabled first",
               "cited_urls_considered": 0, "report": None}

    own_html = await fetch_and_cache(own_url)
    own_features = extract_features(own_html, query=query) if own_html else None

    cited_features = []
    for url in cited_urls:
        html = await fetch_and_cache(url)
        if html:
            cited_features.append(extract_features(html, query=query))

    if own_features is None:
        return {"tracker_id": tracker_id, "query": query, "own_url": own_url,
               "note": "the tracker's own page could not be fetched (robots.txt or a request error)",
               "cited_urls_considered": len(cited_features), "report": None}

    report = compare_to_citations(own_features, cited_features)

    recommendation = None
    if with_recommendation and report["findings"]:
        prompt = build_recommendation_prompt(query, report)
        from api.utils.llm_json_client import call_llm_for_summary

        text, in_tok, out_tok = await call_llm_for_summary(
            provider=provider, model=model,
            system_prompt="You are a GEO (generative engine optimization) analyst. "
                         "You only see a table of measured features, never any page's actual content.",
            user_content=prompt,
        )
        from api.routes.costs import track_cost
        await track_cost(source="citation_comparison", provider=provider, model=model,
                         input_tokens=in_tok, output_tokens=out_tok)
        recommendation = text

    return {
        "tracker_id": tracker_id, "query": query, "own_url": own_url,
        "cited_urls_considered": len(cited_features), "cited_urls_requested": len(cited_urls),
        "report": report, "recommendation": recommendation,
    }
