"""AI Visibility summary API — aggregates visibility-tracking data.

Etapa 3 of the consolidation (docs/CONSOLIDATION_PLAN.md): previously this
module existed solely to merge GeoMonitorProject/GeoMonitorScan and
CitationTracker/CitationScan back together after a scan, because they were
the same feature tracked in two separate tables. Since api/routes/visibility.py
unified both into CitationTracker/CitationScan, this module now reads one
table pair instead of computing two parallel aggregates and merging them.
"""
import json
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db
from api.models.content import CitationTracker, CitationScan

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai-visibility", tags=["ai_visibility"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class ProviderStats(BaseModel):
    mentions: Optional[float] = None
    citations: Optional[float] = None


class VisibilitySummary(BaseModel):
    composite_score: float
    mention_rate: float
    citation_rate: float
    provider_breakdown: Dict[str, ProviderStats]
    trend: Optional[str] = None
    last_scan_at: Optional[str] = None
    geo_projects_count: int
    citation_trackers_count: int


class RecentScan(BaseModel):
    id: str
    type: str  # "geo" or "citation"
    project_name: str
    website: str
    score: Optional[float]
    status: str
    completed_at: Optional[str]
    project_url: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_field(value: Any) -> dict:
    """Safely parse a Text/JSON column that may be None or a raw string."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/summary", response_model=VisibilitySummary)
async def get_visibility_summary(db: AsyncSession = Depends(get_db)):
    """Return aggregated AI visibility metrics across all tracked targets."""

    trackers_result = await db.execute(select(CitationTracker))
    trackers = trackers_result.scalars().all()

    mention_scores: List[float] = []
    citation_scores: List[float] = []
    provider_data: Dict[str, Dict] = {}
    last_scan_at: Optional[datetime] = None

    async def _latest_completed(tracker_id: str) -> Optional[CitationScan]:
        result = await db.execute(
            select(CitationScan)
            .where(CitationScan.tracker_id == tracker_id, CitationScan.status == "completed")
            .order_by(desc(CitationScan.completed_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    for tracker in trackers:
        scan = await _latest_completed(tracker.id)
        if scan is None:
            continue

        if scan.visibility_score is not None:
            mention_scores.append(scan.visibility_score)
        if scan.citation_rate is not None:
            citation_scores.append(scan.citation_rate)

        # provider_breakdown: {"chatgpt": {citations, mentions, queries, responses, citation_rate, mention_rate}, ...}
        breakdown = _parse_json_field(scan.provider_breakdown)
        for provider, data in breakdown.items():
            if not isinstance(data, dict):
                continue
            bucket = provider_data.setdefault(provider, {"mentions_sum": 0.0, "mentions_n": 0,
                                                          "citations_sum": 0.0, "citations_n": 0})
            if "mention_rate" in data:
                bucket["mentions_sum"] += data["mention_rate"]
                bucket["mentions_n"] += 1
            if "citation_rate" in data:
                bucket["citations_sum"] += data["citation_rate"]
                bucket["citations_n"] += 1

        if scan.completed_at and (last_scan_at is None or scan.completed_at > last_scan_at):
            last_scan_at = scan.completed_at

    mention_rate = (sum(mention_scores) / len(mention_scores)) if mention_scores else 0.0
    citation_rate = (sum(citation_scores) / len(citation_scores)) if citation_scores else 0.0
    composite_score = round(mention_rate * 0.4 + citation_rate * 0.6, 1)

    provider_breakdown: Dict[str, ProviderStats] = {}
    for provider, d in provider_data.items():
        mentions_avg = round(d["mentions_sum"] / d["mentions_n"], 1) if d["mentions_n"] else None
        citations_avg = round(d["citations_sum"] / d["citations_n"], 1) if d["citations_n"] else None
        provider_breakdown[provider] = ProviderStats(mentions=mentions_avg, citations=citations_avg)

    # Trend: compare to second-most-recent completed scan per tracker
    trend: Optional[str] = None
    if trackers:
        prev_mention: List[float] = []
        prev_citation: List[float] = []
        for tracker in trackers:
            result = await db.execute(
                select(CitationScan)
                .where(CitationScan.tracker_id == tracker.id, CitationScan.status == "completed")
                .order_by(desc(CitationScan.completed_at))
                .limit(2)
            )
            scans = result.scalars().all()
            if len(scans) >= 2:
                if scans[1].visibility_score is not None:
                    prev_mention.append(scans[1].visibility_score)
                if scans[1].citation_rate is not None:
                    prev_citation.append(scans[1].citation_rate)

        if prev_mention or prev_citation:
            pm = (sum(prev_mention) / len(prev_mention)) if prev_mention else mention_rate
            pc = (sum(prev_citation) / len(prev_citation)) if prev_citation else citation_rate
            delta = composite_score - (pm * 0.4 + pc * 0.6)
            trend = f"+{delta:.1f}" if delta >= 0 else f"{delta:.1f}"

    return VisibilitySummary(
        composite_score=composite_score,
        mention_rate=round(mention_rate, 1),
        citation_rate=round(citation_rate, 1),
        provider_breakdown=provider_breakdown,
        trend=trend,
        last_scan_at=last_scan_at.isoformat() if last_scan_at else None,
        geo_projects_count=len(trackers),
        citation_trackers_count=len(trackers),
    )


@router.get("/recent-scans", response_model=List[RecentScan])
async def get_recent_scans(db: AsyncSession = Depends(get_db)):
    """Return the last 10 scans across all tracked targets."""
    result = await db.execute(
        select(CitationScan, CitationTracker)
        .join(CitationTracker, CitationScan.tracker_id == CitationTracker.id)
        .order_by(desc(CitationScan.created_at))
        .limit(10)
    )

    scans: List[RecentScan] = []
    for scan, tracker in result.all():
        has_url_patterns = bool(_parse_json_field(tracker.url_patterns) if isinstance(tracker.url_patterns, str) else tracker.url_patterns)
        # Prefer citation_rate when url_patterns is configured (this target cares about
        # URL citations specifically); otherwise fall back to the broader mention rate.
        score = scan.citation_rate if (has_url_patterns and scan.citation_rate is not None) else scan.visibility_score
        scans.append(RecentScan(
            id=scan.id,
            type="citation" if (has_url_patterns and scan.citation_rate is not None) else "geo",
            project_name=tracker.name,
            website=tracker.website,
            score=score,
            status=scan.status,
            completed_at=scan.completed_at.isoformat() if scan.completed_at else None,
            project_url=f"/citations/trackers/{tracker.id}",
        ))

    return scans[:10]
