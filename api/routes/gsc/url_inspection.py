"""
GSC URL Inspection — Etapa 3 of docs/IMPROVEMENTS_PLAN.md.

Everything else in this package calls searchanalytics: how a URL performs
in search. This is the one endpoint that answers a different question --
can Google find, crawl, and index this URL at all -- which nothing else in
the tool could answer before this existed.

Reuses the same OAuth credentials as oauth_sync.py (_get_gsc_credentials,
including its refresh/expiry handling) rather than a second auth flow. Calls
the REST endpoint directly via httpx instead of through
google-api-python-client's discovery-generated method, matching
core/performance_client.py's style and making this independently mockable
without needing a real discovery document.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from api.limiter import limiter
from api.models.database import (
    AsyncSessionLocal, GscProperty, UrlInspection, UrlInspectionQuotaLog,
)
from ._shared import _get_gsc_credentials

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gsc", tags=["gsc"])

_INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"

# Google's own documented courtesy limit is ~2000/day/property. Enforcing
# slightly under that (not exactly 2000) leaves headroom for other tools or
# manual Search Console use against the same property hitting the same
# quota concurrently -- this app refusing at exactly 2000 could still get a
# live 429 from Google if anything else also called the API that day.
_DAILY_QUOTA_LIMIT = 1900


class QuotaExceeded(Exception):
    pass


async def _quota_used_today(db, property_id: str, today: str) -> int:
    return (await db.execute(
        select(func.count(UrlInspectionQuotaLog.id)).where(
            UrlInspectionQuotaLog.property_id == property_id,
            UrlInspectionQuotaLog.checked_date == today,
        )
    )).scalar() or 0


async def _fetch_inspection(access_token: str, site_url: str, page_url: str) -> dict:
    """
    Raises QuotaExceeded on Google's 429, HTTPException(502) on any other
    failure -- there is no "no data" outcome to degrade to here the way
    CrUX's 404 was: every valid page URL on a verified property gets an
    inspection result, even if that result says "never crawled".
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _INSPECT_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"inspectionUrl": page_url, "siteUrl": site_url},
            )
    except httpx.HTTPError as exc:
        logger.warning("URL Inspection request failed for %s: %s", page_url, exc)
        raise HTTPException(502, "Could not reach Google's URL Inspection API")

    if resp.status_code == 429:
        raise QuotaExceeded()
    if resp.status_code != 200:
        logger.warning("URL Inspection returned %s for %s: %s", resp.status_code, page_url, resp.text[:300])
        raise HTTPException(502, f"URL Inspection API returned {resp.status_code}")

    return resp.json()


def _parse_inspection(data: dict) -> dict:
    result = data.get("inspectionResult", {})
    idx = result.get("indexStatusResult", {})
    mobile = result.get("mobileUsabilityResult", {})
    rich = result.get("richResultsResult", {})

    last_crawl = idx.get("lastCrawlTime")
    last_crawl_dt = None
    if last_crawl:
        try:
            last_crawl_dt = datetime.fromisoformat(last_crawl.replace("Z", "+00:00"))
        except ValueError:
            pass

    return {
        "verdict": idx.get("verdict"),
        "coverage_state": idx.get("coverageState"),
        "robots_txt_state": idx.get("robotsTxtState"),
        "indexing_state": idx.get("indexingState"),
        "page_fetch_state": idx.get("pageFetchState"),
        "google_canonical": idx.get("googleCanonical"),
        "user_canonical": idx.get("userCanonical"),
        "sitemaps": idx.get("sitemap", []),
        "last_crawl_time": last_crawl_dt,
        "mobile_usability_verdict": mobile.get("verdict"),
        "rich_results_verdict": rich.get("verdict"),
        "raw": data,
    }


class InspectRequest(BaseModel):
    url: str
    force: bool = False       # bypass the cache and spend a quota unit even if a recent inspection exists
    max_age_days: int = 3     # a cached inspection this fresh is returned without calling Google


@router.post("/properties/{property_id}/inspect")
@limiter.limit("30/minute")
async def inspect_url(request: Request, property_id: str, req: InspectRequest):
    creds = await _get_gsc_credentials()
    if not creds:
        raise HTTPException(401, "Google account not connected. Connect at /gsc first.")

    async with AsyncSessionLocal() as db:
        prop = await db.get(GscProperty, property_id)
        if not prop:
            raise HTTPException(404, "Property not found")

        today = datetime.now(timezone.utc).date().isoformat()

        if not req.force:
            cutoff = (datetime.now(timezone.utc).date() - timedelta(days=req.max_age_days)).isoformat()
            cached = (await db.execute(
                select(UrlInspection)
                .where(
                    UrlInspection.property_id == property_id,
                    UrlInspection.page_url == req.url,
                    UrlInspection.checked_date >= cutoff,
                )
                .order_by(UrlInspection.checked_date.desc())
                .limit(1)
            )).scalar_one_or_none()
            if cached:
                body = cached.to_dict()
                body["from_cache"] = True
                return body

        used = await _quota_used_today(db, property_id, today)
        if used >= _DAILY_QUOTA_LIMIT:
            raise HTTPException(
                429,
                f"Daily URL Inspection quota reached for this property "
                f"({used}/{_DAILY_QUOTA_LIMIT}). Try again tomorrow, or inspect a "
                f"different property.",
            )

        try:
            raw = await _fetch_inspection(creds.token, prop.site_url, req.url)
        except QuotaExceeded:
            raise HTTPException(429, "Google's URL Inspection quota was reached for this property")

        parsed = _parse_inspection(raw)

        db.add(UrlInspectionQuotaLog(property_id=property_id, checked_date=today))

        stmt = sqlite_insert(UrlInspection).values(
            property_id=property_id, page_url=req.url, checked_date=today,
            verdict=parsed["verdict"], coverage_state=parsed["coverage_state"],
            robots_txt_state=parsed["robots_txt_state"], indexing_state=parsed["indexing_state"],
            page_fetch_state=parsed["page_fetch_state"], google_canonical=parsed["google_canonical"],
            user_canonical=parsed["user_canonical"], sitemaps_json=json.dumps(parsed["sitemaps"]),
            last_crawl_time=parsed["last_crawl_time"],
            mobile_usability_verdict=parsed["mobile_usability_verdict"],
            rich_results_verdict=parsed["rich_results_verdict"], raw_json=None,
        )
        update_cols = {
            k: getattr(stmt.excluded, k) for k in (
                "verdict", "coverage_state", "robots_txt_state", "indexing_state",
                "page_fetch_state", "google_canonical", "user_canonical", "sitemaps_json",
                "last_crawl_time", "mobile_usability_verdict", "rich_results_verdict",
            )
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=["property_id", "page_url", "checked_date"], set_=update_cols,
        )
        await db.execute(stmt)
        await db.commit()

        row = (await db.execute(
            select(UrlInspection).where(
                UrlInspection.property_id == property_id,
                UrlInspection.page_url == req.url,
                UrlInspection.checked_date == today,
            )
        )).scalar_one()
        body = row.to_dict()
        body["from_cache"] = False
        return body


@router.get("/properties/{property_id}/inspect/quota")
async def get_inspection_quota(property_id: str):
    today = datetime.now(timezone.utc).date().isoformat()
    async with AsyncSessionLocal() as db:
        used = await _quota_used_today(db, property_id, today)
    return {"date": today, "used": used, "limit": _DAILY_QUOTA_LIMIT, "remaining": max(0, _DAILY_QUOTA_LIMIT - used)}


@router.get("/properties/{property_id}/inspections")
async def list_inspections(property_id: str, days: int = 30):
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(UrlInspection)
            .where(UrlInspection.property_id == property_id, UrlInspection.checked_date >= cutoff)
            .order_by(UrlInspection.checked_date.desc())
        )).scalars().all()
    return {"property_id": property_id, "days": days, "items": [r.to_dict() for r in rows]}
