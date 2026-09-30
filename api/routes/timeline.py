"""
GET /api/timeline -- Pasul 10 of docs/superpowers/plans/2026-09-30-next-steps.md.

Puts four independently-collected sources on one axis for a single URL: what
changed on the page (page_snapshots + core/page_diff.py), daily GSC
performance (gsc_page_history, Pasul 2), Google ranking observations
(serp_rank_observations, Pasul 8) and AI citation counts
(citation_scans.top_cited_urls, capped at each scan's own top 10). Depends on
Pasul 2 for the GSC side -- with no GSC history yet (OAuth was disconnected
when this was written), the endpoint still returns the other three sources.

Scale note: each source is loaded in full and matched by normalized URL in
Python, rather than with a SQL WHERE on a stored normalized column (none of
these tables has one). Fine at this project's actual data volume; revisit
with an indexed normalized-URL column if any of these tables grows large
enough for that scan to matter.

"After" a change describes the following window; it is never a claim of
"because of" -- see core/timeline.py's docstring.
"""
import json
import logging
from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import (
    get_db, PageSnapshot, GscPageHistory, SerpRankObservation, CitationScan,
)
from api.utils.errors import raise_bad_request
from core.page_diff import diff_snapshots
from core.timeline import build_timeline
from core.url_normalize import normalize_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/timeline", tags=["timeline"])


async def _matching_page_snapshots(db: AsyncSession, target: str) -> List[PageSnapshot]:
    rows = (await db.execute(select(PageSnapshot))).scalars().all()
    matched = [r for r in rows if normalize_url(r.url) == target and r.captured_at is not None]
    matched.sort(key=lambda r: r.captured_at)
    return matched


async def _matching_gsc_rows(db: AsyncSession, target: str) -> List[dict]:
    rows = (await db.execute(select(GscPageHistory))).scalars().all()
    matched = [r for r in rows if normalize_url(r.page) == target]
    matched.sort(key=lambda r: r.period_start)
    return [{"period_start": r.period_start, "clicks": r.clicks,
             "impressions": r.impressions, "ctr": r.ctr, "position": r.position}
            for r in matched]


async def _matching_serp_observations(db: AsyncSession, target: str) -> List[dict]:
    rows = (await db.execute(
        select(SerpRankObservation).where(SerpRankObservation.ranking_url.isnot(None))
    )).scalars().all()
    matched = [r for r in rows if normalize_url(r.ranking_url) == target]
    matched.sort(key=lambda r: r.observed_at or "")
    return [{"date": r.observed_at.isoformat() if r.observed_at else None,
             "query": r.query, "rank_group": r.rank_group, "rank_absolute": r.rank_absolute}
            for r in matched]


async def _matching_ai_citations(db: AsyncSession, target: str) -> List[dict]:
    scans = (await db.execute(
        select(CitationScan).where(CitationScan.status == "completed").order_by(CitationScan.completed_at)
    )).scalars().all()
    out = []
    for scan in scans:
        if not scan.top_cited_urls:
            continue
        try:
            urls = json.loads(scan.top_cited_urls)
        except (ValueError, TypeError):
            continue
        for entry in urls:
            if normalize_url(entry.get("url", "")) == target:
                out.append({
                    "date": scan.completed_at.isoformat() if scan.completed_at else None,
                    "scan_id": scan.id, "count": entry.get("count"),
                })
                break   # a count per scan, not a list of instances -- one match is enough
    return out


@router.get("")
async def get_timeline(url: str = Query(..., min_length=1), db: AsyncSession = Depends(get_db)):
    """Everything known about one URL, on one timeline."""
    target = normalize_url(url)
    if not target:
        raise_bad_request("url is required")

    snapshots = await _matching_page_snapshots(db, target)
    gsc_rows = await _matching_gsc_rows(db, target)
    serp_observations = await _matching_serp_observations(db, target)
    ai_citations = await _matching_ai_citations(db, target)

    change_events = []
    for before, after in zip(snapshots, snapshots[1:]):
        if before.content_hash and before.content_hash == after.content_hash:
            continue
        changes = diff_snapshots(before.to_fields(), after.to_fields())
        if not changes:
            continue
        change_events.append({"date": after.captured_at.date(), "changes": changes})

    changes_timeline = build_timeline(change_events, gsc_rows)

    return {
        "url": target,
        "gsc": {"available": bool(gsc_rows), "days_with_data": len(gsc_rows), "series": gsc_rows},
        "serp": {"available": bool(serp_observations), "observations": serp_observations},
        "ai_citations": {"available": bool(ai_citations), "citations": ai_citations},
        "changes": changes_timeline,
    }
