"""
GET /api/gsc/properties/{property_id}/decay -- Pasul 15 of
docs/superpowers/plans/2026-09-30-next-steps.md.

Content decay per page, from gsc_page_history (Pasul 2, real accumulated
history) -- needs no live GSC call, unlike striking-distance
(api/routes/gsc/opportunities.py). Backend only this session, matching the
Pasul 9/10/12/13/14 split -- a UI table is separate follow-up work.
"""
from typing import Optional

from fastapi import APIRouter, Query
from sqlalchemy import select

from api.models.database import AsyncSessionLocal, GscPageHistory, GscProperty
from api.utils.errors import raise_not_found
from core.content_decay import build_weekly_series, detect_decay

router = APIRouter(prefix="/api/gsc", tags=["gsc"])


@router.get("/properties/{property_id}/decay")
async def get_content_decay(
    property_id: str,
    page: Optional[str] = Query(None, description="Limit to one page URL; omit for every page with enough history"),
):
    """
    Content decay per page. A page with fewer than 12 weeks of accumulated
    gsc_page_history reports insufficient_history rather than a
    confident-looking "no decay" -- those are different claims.
    """
    async with AsyncSessionLocal() as db:
        prop = await db.get(GscProperty, property_id)
        if not prop:
            raise_not_found("Property")

        query = select(GscPageHistory).where(GscPageHistory.property_id == property_id)
        if page:
            query = query.where(GscPageHistory.page == page)
        rows = (await db.execute(query)).scalars().all()

    by_page = {}
    for r in rows:
        by_page.setdefault(r.page, []).append({
            "period_start": r.period_start, "clicks": r.clicks,
            "impressions": r.impressions, "position": r.position,
        })

    results = []
    for page_url, daily_rows in by_page.items():
        weekly = build_weekly_series(daily_rows)
        decay = detect_decay(weekly)
        results.append({"page": page_url, **decay})

    # Decaying pages first (the ones worth acting on); insufficient-history
    # pages last -- they're not "clean", just unmeasured.
    results.sort(key=lambda r: (not bool(r.get("is_decaying")), bool(r.get("insufficient_history"))))

    return {"property_id": property_id, "pages_checked": len(results), "results": results}
