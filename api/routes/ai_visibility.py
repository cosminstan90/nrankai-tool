"""AI Visibility summary API — aggregates GeoMonitor + CitationTracker data."""
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db
from api.models.infra import GeoMonitorProject, GeoMonitorScan
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
    """Return aggregated AI visibility metrics across all projects."""

    # ── GeoMonitor projects + latest completed scan per project ──────────────
    geo_projects_result = await db.execute(select(GeoMonitorProject))
    geo_projects = geo_projects_result.scalars().all()

    geo_scores: List[float] = []
    geo_provider_data: Dict[str, Dict] = {}
    geo_last_scan_at: Optional[datetime] = None

    for project in geo_projects:
        scan_result = await db.execute(
            select(GeoMonitorScan)
            .where(
                GeoMonitorScan.project_id == project.id,
                GeoMonitorScan.status == "completed",
            )
            .order_by(desc(GeoMonitorScan.completed_at))
            .limit(1)
        )
        scan = scan_result.scalar_one_or_none()
        if scan is None:
            continue

        if scan.visibility_score is not None:
            geo_scores.append(scan.visibility_score)

        # provider_breakdown in GeoMonitorScan: {"chatgpt": {mentioned: 5, total: 10}, ...}
        breakdown = _parse_json_field(getattr(scan, "provider_breakdown", None))
        for provider, data in breakdown.items():
            if not isinstance(data, dict):
                continue
            total = data.get("total", 0) or 0
            mentioned = data.get("mentioned", 0) or 0
            pct = (mentioned / total * 100) if total > 0 else 0.0
            if provider not in geo_provider_data:
                geo_provider_data[provider] = {"mentions_sum": 0.0, "count": 0}
            geo_provider_data[provider]["mentions_sum"] += pct
            geo_provider_data[provider]["count"] += 1

        # Track most recent completed_at
        if scan.completed_at:
            if geo_last_scan_at is None or scan.completed_at > geo_last_scan_at:
                geo_last_scan_at = scan.completed_at

    mention_rate = (sum(geo_scores) / len(geo_scores)) if geo_scores else 0.0

    # ── CitationTracker trackers + latest completed scan per tracker ──────────
    ct_result = await db.execute(select(CitationTracker))
    citation_trackers = ct_result.scalars().all()

    citation_scores: List[float] = []
    citation_provider_data: Dict[str, Dict] = {}
    citation_last_scan_at: Optional[datetime] = None

    for tracker in citation_trackers:
        scan_result = await db.execute(
            select(CitationScan)
            .where(
                CitationScan.tracker_id == tracker.id,
                CitationScan.status == "completed",
            )
            .order_by(desc(CitationScan.completed_at))
            .limit(1)
        )
        scan = scan_result.scalar_one_or_none()
        if scan is None:
            continue

        if scan.citation_rate is not None:
            citation_scores.append(scan.citation_rate)

        # provider_breakdown in CitationScan: {"chatgpt": {citations: 5, mentions: 8, queries: 20}, ...}
        breakdown = _parse_json_field(getattr(scan, "provider_breakdown", None))
        for provider, data in breakdown.items():
            if not isinstance(data, dict):
                continue
            queries = data.get("queries", 0) or 0
            citations = data.get("citations", 0) or 0
            pct = (citations / queries * 100) if queries > 0 else 0.0
            if provider not in citation_provider_data:
                citation_provider_data[provider] = {"citations_sum": 0.0, "count": 0}
            citation_provider_data[provider]["citations_sum"] += pct
            citation_provider_data[provider]["count"] += 1

        if scan.completed_at:
            if citation_last_scan_at is None or scan.completed_at > citation_last_scan_at:
                citation_last_scan_at = scan.completed_at

    citation_rate = (sum(citation_scores) / len(citation_scores)) if citation_scores else 0.0

    # ── Composite score ───────────────────────────────────────────────────────
    composite_score = round(mention_rate * 0.4 + citation_rate * 0.6, 1)

    # ── Provider breakdown (merged) ───────────────────────────────────────────
    all_providers = set(geo_provider_data.keys()) | set(citation_provider_data.keys())
    provider_breakdown: Dict[str, ProviderStats] = {}
    for provider in all_providers:
        geo_d = geo_provider_data.get(provider)
        cit_d = citation_provider_data.get(provider)
        mentions_avg = None
        citations_avg = None
        if geo_d and geo_d["count"] > 0:
            mentions_avg = round(geo_d["mentions_sum"] / geo_d["count"], 1)
        if cit_d and cit_d["count"] > 0:
            citations_avg = round(cit_d["citations_sum"] / cit_d["count"], 1)
        provider_breakdown[provider] = ProviderStats(mentions=mentions_avg, citations=citations_avg)

    # ── Trend: compare to second-most-recent composite ────────────────────────
    trend: Optional[str] = None
    if geo_projects or citation_trackers:
        # Build previous composite from second-latest scans
        prev_geo_scores: List[float] = []
        for project in geo_projects:
            scans_result = await db.execute(
                select(GeoMonitorScan)
                .where(
                    GeoMonitorScan.project_id == project.id,
                    GeoMonitorScan.status == "completed",
                )
                .order_by(desc(GeoMonitorScan.completed_at))
                .limit(2)
            )
            scans = scans_result.scalars().all()
            if len(scans) >= 2 and scans[1].visibility_score is not None:
                prev_geo_scores.append(scans[1].visibility_score)

        prev_citation_scores: List[float] = []
        for tracker in citation_trackers:
            scans_result = await db.execute(
                select(CitationScan)
                .where(
                    CitationScan.tracker_id == tracker.id,
                    CitationScan.status == "completed",
                )
                .order_by(desc(CitationScan.completed_at))
                .limit(2)
            )
            scans = scans_result.scalars().all()
            if len(scans) >= 2 and scans[1].citation_rate is not None:
                prev_citation_scores.append(scans[1].citation_rate)

        if prev_geo_scores or prev_citation_scores:
            prev_mention = (sum(prev_geo_scores) / len(prev_geo_scores)) if prev_geo_scores else mention_rate
            prev_citation = (sum(prev_citation_scores) / len(prev_citation_scores)) if prev_citation_scores else citation_rate
            prev_composite = prev_mention * 0.4 + prev_citation * 0.6
            delta = composite_score - prev_composite
            trend = f"+{delta:.1f}" if delta >= 0 else f"{delta:.1f}"

    # ── last_scan_at ──────────────────────────────────────────────────────────
    last_scan_at: Optional[str] = None
    candidates = [d for d in [geo_last_scan_at, citation_last_scan_at] if d is not None]
    if candidates:
        last_scan_at = max(candidates).isoformat()

    return VisibilitySummary(
        composite_score=composite_score,
        mention_rate=round(mention_rate, 1),
        citation_rate=round(citation_rate, 1),
        provider_breakdown=provider_breakdown,
        trend=trend,
        last_scan_at=last_scan_at,
        geo_projects_count=len(geo_projects),
        citation_trackers_count=len(citation_trackers),
    )


@router.get("/recent-scans", response_model=List[RecentScan])
async def get_recent_scans(db: AsyncSession = Depends(get_db)):
    """Return last 10 scans combined from GeoMonitor and CitationTracker."""
    scans: List[RecentScan] = []

    # Geo scans
    geo_result = await db.execute(
        select(GeoMonitorScan, GeoMonitorProject)
        .join(GeoMonitorProject, GeoMonitorScan.project_id == GeoMonitorProject.id)
        .order_by(desc(GeoMonitorScan.created_at))
        .limit(10)
    )
    for scan, project in geo_result.all():
        scans.append(RecentScan(
            id=scan.id,
            type="geo",
            project_name=project.name,
            website=project.website,
            score=scan.visibility_score,
            status=scan.status,
            completed_at=scan.completed_at.isoformat() if scan.completed_at else None,
            project_url=f"/geo-monitor/projects/{project.id}",
        ))

    # Citation scans
    cit_result = await db.execute(
        select(CitationScan, CitationTracker)
        .join(CitationTracker, CitationScan.tracker_id == CitationTracker.id)
        .order_by(desc(CitationScan.created_at))
        .limit(10)
    )
    for scan, tracker in cit_result.all():
        scans.append(RecentScan(
            id=scan.id,
            type="citation",
            project_name=tracker.name,
            website=tracker.website,
            score=scan.citation_rate,
            status=scan.status,
            completed_at=scan.completed_at.isoformat() if scan.completed_at else None,
            project_url=f"/citations/trackers/{tracker.id}",
        ))

    # Sort combined list by completed_at desc, then return top 10
    def sort_key(s: RecentScan):
        return s.completed_at or ""

    scans.sort(key=sort_key, reverse=True)
    return scans[:10]
