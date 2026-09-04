"""
Core Web Vitals — Etapa 2 of docs/IMPROVEMENTS_PLAN.md.

Fetches real-user field data (CrUX) and lab data (PageSpeed Insights) for a
URL, stores it in performance_snapshots, and exposes the accumulated trend.
See core/performance_client.py for why field and lab data are never mixed
into one row, and PerformanceSnapshot's docstring in api/models/infra.py for
why this is dated/additive rather than a "latest snapshot" table.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from api.limiter import limiter
from api.models.database import AsyncSessionLocal, PerformanceSnapshot
from core.performance_client import fetch_crux, fetch_crux_history, fetch_psi

router = APIRouter(prefix="/api/performance", tags=["performance"])


class CheckRequest(BaseModel):
    url: str
    strategy: str = "mobile"     # "mobile" | "desktop"
    is_origin: bool = False       # True to query CrUX for the whole origin, not one URL
    include_psi: bool = True      # PSI runs a real Lighthouse pass -- can take 30-60s


async def _upsert_snapshot(db, **fields) -> None:
    stmt = sqlite_insert(PerformanceSnapshot).values(**fields)
    update_cols = {
        k: getattr(stmt.excluded, k)
        for k in fields
        if k not in ("url", "strategy", "source", "period_start", "period_end")
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=["url", "strategy", "source", "period_start", "period_end"],
        set_=update_cols,
    )
    await db.execute(stmt)


@router.post("/check")
@limiter.limit("20/minute")
async def check_performance(request: Request, req: CheckRequest):
    """
    Fetch current CrUX field data (+ up to 25 weeks of CrUX history on first
    check) and, unless include_psi=False, one PSI lab run. Stores everything,
    returns the current reading plus how many history points were captured.

    Neither Google API requires a key to exist server-side before calling --
    if PAGESPEED_API_KEY isn't set, fetch_crux/fetch_psi log a warning and
    return None, and this degrades to reporting "no data" per source rather
    than crashing. That is the actual state of this deployment right now.
    """
    if req.strategy not in ("mobile", "desktop"):
        raise HTTPException(422, "strategy must be 'mobile' or 'desktop'")
    if not os.getenv("PAGESPEED_API_KEY"):
        raise HTTPException(
            503,
            "PAGESPEED_API_KEY is not configured. Get a free key at "
            "https://console.cloud.google.com (enable 'PageSpeed Insights API'), "
            "then set PAGESPEED_API_KEY in .env.",
        )

    today = datetime.now(timezone.utc).date().isoformat()

    crux = await fetch_crux(req.url, req.strategy, req.is_origin)
    history = await fetch_crux_history(req.url, req.strategy, req.is_origin)
    psi = await fetch_psi(req.url, req.strategy) if req.include_psi else None

    history_written = 0
    async with AsyncSessionLocal() as db:
        if crux:
            await _upsert_snapshot(
                db, url=req.url, strategy=req.strategy, source="crux",
                p75_lcp=crux["p75"]["lcp"], p75_inp=crux["p75"]["inp"], p75_cls=crux["p75"]["cls"],
                p75_fcp=crux["p75"]["fcp"], p75_ttfb=crux["p75"]["ttfb"],
                lcp_rating=crux["rating"]["lcp"], inp_rating=crux["rating"]["inp"],
                cls_rating=crux["rating"]["cls"], fcp_rating=crux["rating"]["fcp"],
                ttfb_rating=crux["rating"]["ttfb"],
                period_start=crux["period_start"] or today, period_end=crux["period_end"] or today,
                raw_json=None,
            )
            history_written += 1

        for point in history:
            if not point["period_start"] or not point["period_end"]:
                continue
            await _upsert_snapshot(
                db, url=req.url, strategy=req.strategy, source="crux",
                p75_lcp=point["p75"]["lcp"], p75_inp=point["p75"]["inp"], p75_cls=point["p75"]["cls"],
                p75_fcp=point["p75"]["fcp"], p75_ttfb=point["p75"]["ttfb"],
                lcp_rating=point["rating"]["lcp"], inp_rating=point["rating"]["inp"],
                cls_rating=point["rating"]["cls"], fcp_rating=point["rating"]["fcp"],
                ttfb_rating=point["rating"]["ttfb"],
                period_start=point["period_start"], period_end=point["period_end"],
                raw_json=None,
            )
            history_written += 1

        if psi:
            await _upsert_snapshot(
                db, url=req.url, strategy=req.strategy, source="psi",
                performance_score=psi["performance_score"], lab_lcp=psi["lcp"], lab_cls=psi["cls"],
                lab_tbt=psi["tbt"], lab_fcp=psi["fcp"], lab_speed_index=psi["speed_index"],
                period_start=today, period_end=today, raw_json=None,
            )

        await db.commit()

    return {
        "url": req.url,
        "strategy": req.strategy,
        "field": {
            "p75": crux["p75"], "rating": crux["rating"],
            "period": {"start": crux["period_start"], "end": crux["period_end"]},
        } if crux else None,
        "lab": {
            "performance_score": psi["performance_score"], "lcp": psi["lcp"], "cls": psi["cls"],
            "tbt": psi["tbt"], "fcp": psi["fcp"], "speed_index": psi["speed_index"],
        } if psi else None,
        "history_points_captured": history_written,
    }


@router.get("/history")
async def get_performance_history(
    url: str,
    strategy: str = "mobile",
    source: str = "crux",
    days: int = 180,
):
    """Stored trend for a URL. source="crux" for field data, "psi" for lab runs."""
    if source not in ("crux", "psi"):
        raise HTTPException(422, "source must be 'crux' or 'psi'")

    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(PerformanceSnapshot)
            .where(
                PerformanceSnapshot.url == url,
                PerformanceSnapshot.strategy == strategy,
                PerformanceSnapshot.source == source,
                PerformanceSnapshot.period_start >= cutoff,
            )
            .order_by(PerformanceSnapshot.period_start)
        )).scalars().all()

    return {
        "url": url, "strategy": strategy, "source": source, "days": days,
        "points": [r.to_dict() for r in rows],
    }
