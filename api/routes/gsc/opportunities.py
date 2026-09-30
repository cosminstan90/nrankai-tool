"""
GSC opportunities: striking distance and weak CTR -- Pasul 13 of
docs/superpowers/plans/2026-09-30-next-steps.md.

See core/gsc_opportunities.py's module docstring for why these two use
different data sources: weak-ctr reads accumulated history
(gsc_page_history, Pasul 2); striking-distance needs a live
dimensions=[page,query] GSC fetch, since no table anywhere in this project
accumulates that combined dimension.
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query
from sqlalchemy import select

from api.models.database import AsyncSessionLocal, GscPageHistory, GscProperty
from api.utils.errors import raise_bad_request, raise_not_found
from core.gsc_opportunities import find_striking_distance, find_weak_ctr_pages

from ._shared import _get_gsc_credentials

router = APIRouter(prefix="/api/gsc", tags=["gsc"])

DEFAULT_WINDOW_DAYS = 28


def _parse_brand_terms(brand_terms: Optional[str]):
    return [t.strip() for t in brand_terms.split(",") if t.strip()] if brand_terms else None


@router.get("/properties/{property_id}/opportunities/weak-ctr")
async def get_weak_ctr_opportunities(
    property_id: str,
    days: int = Query(DEFAULT_WINDOW_DAYS, ge=7, le=90),
    brand_terms: Optional[str] = Query(None, description="Comma-separated brand terms to exclude"),
):
    """
    Pages whose CTR is below what THIS property's own data shows for pages
    at that position -- built from gsc_page_history (Pasul 2), so it needs
    real accumulated history. A fresh install, or one where GSC OAuth has
    been disconnected since before this history existed, reports
    insufficient_data rather than an empty "no opportunities found" list --
    those are different claims.
    """
    async with AsyncSessionLocal() as db:
        prop = await db.get(GscProperty, property_id)
        if not prop:
            raise_not_found("Property")

        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
        rows = (await db.execute(
            select(GscPageHistory).where(
                GscPageHistory.property_id == property_id,
                GscPageHistory.period_start >= cutoff,
            )
        )).scalars().all()

    per_page = defaultdict(lambda: {"clicks": 0, "impressions": 0, "weighted_position": 0.0})
    for r in rows:
        bucket = per_page[r.page]
        bucket["clicks"] += r.clicks
        bucket["impressions"] += r.impressions
        if r.position is not None:
            bucket["weighted_position"] += r.position * r.impressions

    page_rows = []
    for page, agg in per_page.items():
        impressions = agg["impressions"]
        if not impressions:
            continue
        page_rows.append({
            "page": page, "clicks": agg["clicks"], "impressions": impressions,
            "position": agg["weighted_position"] / impressions,
            "ctr": agg["clicks"] / impressions,
        })

    result = find_weak_ctr_pages(page_rows, brand_terms=_parse_brand_terms(brand_terms))
    return {"property_id": property_id, "window_days": days, "pages_in_window": len(page_rows), **result}


@router.get("/properties/{property_id}/opportunities/striking-distance")
async def get_striking_distance_opportunities(
    property_id: str,
    days: int = Query(DEFAULT_WINDOW_DAYS, ge=7, le=90),
    brand_terms: Optional[str] = Query(None, description="Comma-separated brand terms to exclude"),
):
    """
    Page+query pairs at position 4-15 with real impressions. Requires Google
    OAuth connected: needs a live GSC dimensions=[page,query] fetch (same
    pattern as api/routes/gsc/optimizer.py's cannibalization detector) since
    no table in this project accumulates that combined dimension over time.
    """
    async with AsyncSessionLocal() as db:
        prop = await db.get(GscProperty, property_id)
        if not prop:
            raise_not_found("Property")
        site_url = prop.site_url

    creds = await _get_gsc_credentials()
    if not creds:
        raise_bad_request("No GSC credentials. Please reconnect Google Search Console.")

    try:
        from googleapiclient.discovery import build
    except ImportError:
        raise_bad_request("google-api-python-client not installed")

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    def _fetch():
        svc = build("searchconsole", "v1", credentials=creds)
        body = {
            "startDate": start_date.isoformat(), "endDate": end_date.isoformat(),
            "dimensions": ["page", "query"], "rowLimit": 25000,
        }
        return svc.searchanalytics().query(siteUrl=site_url, body=body).execute().get("rows", [])

    raw_rows = await asyncio.get_event_loop().run_in_executor(None, _fetch)

    page_query_rows = []
    for r in raw_rows:
        keys = r.get("keys") or []
        if len(keys) < 2:
            continue
        page_query_rows.append({
            "page": keys[0], "query": keys[1],
            "clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
            "position": r.get("position"), "ctr": r.get("ctr"),
        })

    result = find_striking_distance(page_query_rows, brand_terms=_parse_brand_terms(brand_terms))
    return {"property_id": property_id, "window_days": days, "pairs_fetched": len(page_query_rows), **result}
