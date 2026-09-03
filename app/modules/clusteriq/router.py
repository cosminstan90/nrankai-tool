"""
ClusterIQ API Router
====================
Prefix : /api/clusteriq
Tags   : clusteriq

All write endpoints launch a background pipeline via create_tracked_task().
Read endpoints use Depends(get_db) for request-scoped sessions.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.limiter import limiter
from api.models._base import AsyncSessionLocal
from api.models.clusteriq import (
    CluCluster,
    CluCompetitorCache,
    CluCompetitorJob,
    CluDecision,
    CluDuplicate,
    CluProject,
    CluSerpData,
    CluUrl,
    CluUrlCluster,
)
from api.models.database import get_db
from api.utils.errors import raise_bad_request, raise_not_found
from api.utils.task_runner import create_tracked_task
from app.modules.clusteriq.tasks.pipeline import PHASE_LABEL, PHASE_PROGRESS

logger = logging.getLogger("clusteriq.router")

router = APIRouter(prefix="/api/clusteriq", tags=["clusteriq"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_project_or_404(project_id: int, db: AsyncSession) -> CluProject:
    row = (await db.execute(
        select(CluProject).where(CluProject.id == project_id)
    )).scalar_one_or_none()
    if row is None:
        raise_not_found("ClusterIQ project", project_id)
    return row  # type: ignore[return-value]


def _estimate_dfs_cost(keyword_limit: int) -> float:
    """Best-effort cost estimate — returns 0.0 if credentials not configured."""
    try:
        from app.modules.clusteriq.services.data_ingestion import DataForSEOIngestionService
        svc = DataForSEOIngestionService()
        return svc.estimate_cost(keyword_limit)
    except (ValueError, Exception):
        return 0.0


# ---------------------------------------------------------------------------
# POST /api/clusteriq/projects
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field


class CreateProjectRequest(BaseModel):
    domain:        str         = Field(..., min_length=1, max_length=512)
    gsc_property:  Optional[str] = Field(None, max_length=512)
    project_id:    Optional[int] = None   # optional link to external project
    keyword_limit: int           = Field(500, ge=1, le=5000)


@router.post("/projects", status_code=202)
@limiter.limit("10/minute")
async def create_project(
    request: Request,
    body:    CreateProjectRequest,
    db:      AsyncSession = Depends(get_db),
):
    """
    Create a ClusterIQ project and immediately launch the full analysis pipeline
    (GSC ingestion → SERP fetch → clustering → decisions) in the background.
    Returns 202 Accepted with the new project ID.
    """
    project = CluProject(
        domain       = body.domain,
        gsc_property = body.gsc_property,
        project_id   = body.project_id,
        status       = "pending",
        created_at   = datetime.now(timezone.utc),
        updated_at   = datetime.now(timezone.utc),
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    estimated_cost = _estimate_dfs_cost(body.keyword_limit)

    from app.modules.clusteriq.tasks.pipeline import run_clusteriq_pipeline
    create_tracked_task(
        run_clusteriq_pipeline(project.id, keyword_limit=body.keyword_limit),
        name=f"clusteriq-pipeline-{project.id}",
    )

    logger.info(
        "[Router] created project id=%d domain=%s — pipeline started", project.id, body.domain
    )
    return {
        "clusteriq_project_id": project.id,
        "status":               "started",
        "estimated_cost_usd":   estimated_cost,
    }


# ---------------------------------------------------------------------------
# GET /api/clusteriq/projects/{id}/status
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/status")
async def get_project_status(
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return the current pipeline status and progress percentage."""
    project = await _get_project_or_404(project_id, db)
    status  = project.status or "pending"
    return {
        "clusteriq_project_id": project.id,
        "domain":               project.domain,
        "status":               status,
        "phase_label":          PHASE_LABEL.get(status, status),
        "progress_pct":         PHASE_PROGRESS.get(status, 0),
        "updated_at":           project.updated_at.isoformat() if project.updated_at else None,
    }


# ---------------------------------------------------------------------------
# GET /api/clusteriq/projects/{id}/overview
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/overview")
async def get_project_overview(
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Site demand snapshot — mirrors the summary panel in the ClusterIQ UI.
    All counts are computed on-the-fly from the project's stored data.
    """
    await _get_project_or_404(project_id, db)

    # Total site URLs crawled
    total_urls = (await db.execute(
        select(func.count(CluUrl.id)).where(CluUrl.project_id == project_id)
    )).scalar() or 0

    # URLs with confirmed demand (any impressions)
    demand_rows = (await db.execute(
        select(CluSerpData.url)
        .where(CluSerpData.project_id == project_id)
        .having(func.sum(CluSerpData.impressions) > 0)
        .group_by(CluSerpData.url)
    )).scalars().all()
    demand_confirmed = len(demand_rows)
    demand_pct       = round(demand_confirmed / total_urls * 100, 1) if total_urls else 0.0

    # Total clusters
    clusters_total = (await db.execute(
        select(func.count(CluCluster.id)).where(CluCluster.project_id == project_id)
    )).scalar() or 0

    # Multi-topic URLs (appear in >1 cluster edge, flagged by clustering engine)
    multi_topic = (await db.execute(
        select(func.count(func.distinct(CluUrlCluster.url_id)))
        .join(CluUrl, CluUrl.id == CluUrlCluster.url_id)
        .where(
            CluUrl.project_id == project_id,
            CluUrlCluster.is_multi_topic == True,  # noqa: E712
        )
    )).scalar() or 0

    # Verdict distribution from decisions
    verdict_rows = (await db.execute(
        select(CluDecision.verdict, func.count(CluDecision.id).label("cnt"))
        .join(CluCluster, CluCluster.id == CluDecision.cluster_id)
        .where(CluCluster.project_id == project_id)
        .group_by(CluDecision.verdict)
    )).all()
    verdicts = {r.verdict.lower(): r.cnt for r in verdict_rows}

    # URLs with actionable issues (Fix or Consolidate verdicts)
    fix_consolidate_cluster_ids = (await db.execute(
        select(CluDecision.cluster_id)
        .join(CluCluster, CluCluster.id == CluDecision.cluster_id)
        .where(
            CluCluster.project_id == project_id,
            CluDecision.verdict.in_(["Fix", "Consolidate"]),
        )
        .distinct()
    )).scalars().all()

    urls_with_issues = 0
    if fix_consolidate_cluster_ids:
        urls_with_issues = (await db.execute(
            select(func.count(func.distinct(CluUrlCluster.url_id)))
            .where(CluUrlCluster.cluster_id.in_(fix_consolidate_cluster_ids))
        )).scalar() or 0

    return {
        "total_urls":            total_urls,
        "urls_with_issues":      urls_with_issues,
        "clusters_total":        clusters_total,
        "demand_confirmed_urls": demand_confirmed,
        "demand_confirmed_pct":  demand_pct,
        "no_demand_urls":        total_urls - demand_confirmed,
        "multi_topic_urls":      multi_topic,
        "verdicts": {
            "fix":         verdicts.get("fix",         0),
            "consolidate": verdicts.get("consolidate", 0),
            "optimize":    verdicts.get("optimize",    0),
            "keep":        verdicts.get("keep",        0),
            "prune":       verdicts.get("prune",       0),
        },
    }


# ---------------------------------------------------------------------------
# GET /api/clusteriq/projects/{id}/clusters
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/clusters")
async def list_clusters(
    project_id:     int,
    verdict_filter: Optional[str] = Query(None, description="Fix|Consolidate|Optimize|Keep|Prune"),
    min_impressions: int           = Query(0, ge=0),
    page:           int            = Query(1, ge=1),
    per_page:       int            = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """
    Paginated list of clusters for a project.
    Optionally filter by verdict and minimum total impressions.
    Each cluster includes its primary decision (verdict + evidence).
    """
    await _get_project_or_404(project_id, db)

    # Base cluster query
    stmt = select(CluCluster).where(CluCluster.project_id == project_id)

    if min_impressions > 0:
        stmt = stmt.where(CluCluster.total_impressions >= min_impressions)

    # Filter by verdict (join to decisions)
    if verdict_filter:
        verdict_norm = verdict_filter.capitalize()
        stmt = stmt.join(
            CluDecision,
            (CluDecision.cluster_id == CluCluster.id)
            & (CluDecision.verdict == verdict_norm),
        )

    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar() or 0

    offset   = (page - 1) * per_page
    clusters = (await db.execute(
        stmt.order_by(CluCluster.total_impressions.desc().nullslast())
            .offset(offset)
            .limit(per_page)
    )).scalars().all()

    # Fetch decisions for the returned cluster IDs in one query
    cluster_ids   = [c.id for c in clusters]
    decision_rows = []
    if cluster_ids:
        decision_rows = (await db.execute(
            select(CluDecision).where(CluDecision.cluster_id.in_(cluster_ids))
        )).scalars().all()
    decisions_by_cluster: dict[int, CluDecision] = {d.cluster_id: d for d in decision_rows}

    items = []
    for c in clusters:
        d = decisions_by_cluster.get(c.id)
        items.append({
            **c.to_dict(),
            "verdict":    d.verdict    if d else None,
            "confidence": d.confidence if d else None,
            "evidence":   d.evidence   if d else [],
        })

    return {
        "clusters": items,
        "total":    total,
        "page":     page,
        "per_page": per_page,
    }


# ---------------------------------------------------------------------------
# GET /api/clusteriq/projects/{id}/clusters/{cluster_id}
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/clusters/{cluster_id}")
async def get_cluster_detail(
    project_id: int,
    cluster_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Full cluster detail: all member URLs with rank, impression share,
    is_multi_topic, and the cluster's decision.
    """
    await _get_project_or_404(project_id, db)

    cluster = (await db.execute(
        select(CluCluster).where(
            CluCluster.id == cluster_id,
            CluCluster.project_id == project_id,
        )
    )).scalar_one_or_none()
    if cluster is None:
        raise_not_found("Cluster", cluster_id)

    # URL members
    member_rows = (await db.execute(
        select(
            CluUrlCluster.url_id,
            CluUrlCluster.role,
            CluUrlCluster.impression_share_pct,
            CluUrlCluster.rank_in_cluster,
            CluUrlCluster.is_multi_topic,
            CluUrl.url,
            CluUrl.status_code,
            CluUrl.is_indexable,
            CluUrl.title,
        )
        .join(CluUrl, CluUrl.id == CluUrlCluster.url_id)
        .where(CluUrlCluster.cluster_id == cluster_id)
        .order_by(CluUrlCluster.rank_in_cluster.asc().nullslast())
    )).all()

    # Per-URL aggregated impressions from SERP data
    url_strings = [r.url for r in member_rows]
    agg_rows    = []
    if url_strings:
        agg_rows = (await db.execute(
            select(
                CluSerpData.url,
                func.sum(CluSerpData.impressions).label("imp"),
                func.sum(CluSerpData.clicks).label("clk"),
                func.avg(CluSerpData.position).label("avg_pos"),
            )
            .where(
                CluSerpData.project_id == project_id,
                CluSerpData.url.in_(url_strings),
            )
            .group_by(CluSerpData.url)
        )).all()
    agg_by_url = {
        r.url: {"imp": r.imp or 0, "clk": r.clk or 0,
                "avg_pos": round(float(r.avg_pos), 1) if r.avg_pos else None}
        for r in agg_rows
    }

    # Decision
    decision = (await db.execute(
        select(CluDecision).where(CluDecision.cluster_id == cluster_id)
    )).scalar_one_or_none()

    members = []
    for r in member_rows:
        agg = agg_by_url.get(r.url, {})
        members.append({
            "url_id":               r.url_id,
            "url":                  r.url,
            "title":                r.title,
            "status_code":          r.status_code,
            "is_indexable":         r.is_indexable,
            "role":                 r.role,
            "rank_in_cluster":      r.rank_in_cluster,
            "impression_share_pct": r.impression_share_pct,
            "is_multi_topic":       r.is_multi_topic,
            "impressions":          agg.get("imp", 0),
            "clicks":               agg.get("clk", 0),
            "avg_position":         agg.get("avg_pos"),
        })

    return {
        **cluster.to_dict(),
        "members":  members,
        "decision": decision.to_dict() if decision else None,
    }


# ---------------------------------------------------------------------------
# GET /api/clusteriq/projects/{id}/urls/{url_encoded:path}
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/urls/{url_encoded:path}")
async def get_url_clusters(
    project_id:  int,
    url_encoded: str,
    db: AsyncSession = Depends(get_db),
):
    """
    All clusters in which a given URL appears, with rank and impression share.
    url_encoded must be URL-encoded (e.g., https%3A%2F%2Fsite.com%2Fpage).
    """
    await _get_project_or_404(project_id, db)
    url = unquote(url_encoded)

    url_row = (await db.execute(
        select(CluUrl).where(
            CluUrl.project_id == project_id,
            CluUrl.url == url,
        )
    )).scalar_one_or_none()
    if url_row is None:
        raise_not_found("URL in project", url)

    membership_rows = (await db.execute(
        select(
            CluUrlCluster.cluster_id,
            CluUrlCluster.role,
            CluUrlCluster.rank_in_cluster,
            CluUrlCluster.impression_share_pct,
            CluUrlCluster.is_multi_topic,
            CluCluster.cluster_label,
            CluCluster.primary_url,
            CluCluster.primary_keyword,
            CluCluster.total_impressions,
        )
        .join(CluCluster, CluCluster.id == CluUrlCluster.cluster_id)
        .where(
            CluUrlCluster.url_id == url_row.id,
            CluCluster.project_id == project_id,
        )
        .order_by(CluUrlCluster.impression_share_pct.desc().nullslast())
    )).all()

    clusters = [
        {
            "cluster_id":           r.cluster_id,
            "cluster_label":        r.cluster_label,
            "primary_url":          r.primary_url,
            "primary_keyword":      r.primary_keyword,
            "cluster_impressions":  r.total_impressions,
            "role":                 r.role,
            "rank_in_cluster":      r.rank_in_cluster,
            "impression_share_pct": r.impression_share_pct,
            "is_multi_topic":       r.is_multi_topic,
        }
        for r in membership_rows
    ]

    return {
        "url":             url,
        "url_id":          url_row.id,
        "is_multi_topic":  any(r.is_multi_topic for r in membership_rows),
        "cluster_count":   len(clusters),
        "clusters":        clusters,
    }


# ---------------------------------------------------------------------------
# GET /api/clusteriq/projects/{id}/duplicates
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/duplicates")
async def list_duplicates(
    project_id: int,
    page:     int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """
    Duplicate cohorts for the project, grouped by winner URL.
    Each cohort contains the winner and the list of URLs to be redirected.
    """
    await _get_project_or_404(project_id, db)

    total = (await db.execute(
        select(func.count(CluDuplicate.id)).where(CluDuplicate.project_id == project_id)
    )).scalar() or 0

    offset = (page - 1) * per_page
    dup_rows = (await db.execute(
        select(CluDuplicate)
        .where(CluDuplicate.project_id == project_id)
        .order_by(CluDuplicate.similarity_score.desc().nullslast())
        .offset(offset)
        .limit(per_page)
    )).scalars().all()

    # Gather URL strings for winner and duplicate IDs in one query
    url_ids = set()
    for d in dup_rows:
        if d.winner_url_id:
            url_ids.add(d.winner_url_id)
        if d.duplicate_url_id:
            url_ids.add(d.duplicate_url_id)

    url_map: dict[int, str] = {}
    if url_ids:
        url_rows = (await db.execute(
            select(CluUrl.id, CluUrl.url).where(CluUrl.id.in_(url_ids))
        )).all()
        url_map = {r.id: r.url for r in url_rows}

    items = []
    for d in dup_rows:
        items.append({
            "id":               d.id,
            "winner_url_id":    d.winner_url_id,
            "winner_url":       url_map.get(d.winner_url_id or -1),
            "duplicate_url_id": d.duplicate_url_id,
            "duplicate_url":    url_map.get(d.duplicate_url_id or -1),
            "similarity_score": d.similarity_score,
            "overlap_keywords": d.overlap_keywords,
            "winner_reason":    d.winner_reason,
            "action":           d.action,
            "dev_brief":        d.dev_brief,
        })

    return {
        "duplicates": items,
        "total":      total,
        "page":       page,
        "per_page":   per_page,
    }


# ---------------------------------------------------------------------------
# GET /api/clusteriq/projects/{id}/decisions
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/decisions")
async def list_decisions(
    project_id:    int,
    verdict:       Optional[str] = Query(None, description="Fix|Consolidate|Optimize|Keep|Prune"),
    min_confidence: float        = Query(0.0, ge=0.0, le=1.0),
    page:          int           = Query(1, ge=1),
    per_page:      int           = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """
    All KUCD decisions for a project.  Filterable by verdict and confidence.
    Each decision includes its cluster label and primary keyword for context.
    """
    await _get_project_or_404(project_id, db)

    stmt = (
        select(CluDecision, CluCluster.cluster_label, CluCluster.primary_keyword)
        .join(CluCluster, CluCluster.id == CluDecision.cluster_id)
        .where(CluCluster.project_id == project_id)
    )

    if verdict:
        stmt = stmt.where(CluDecision.verdict == verdict.capitalize())
    if min_confidence > 0:
        stmt = stmt.where(CluDecision.confidence >= min_confidence)

    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar() or 0

    offset = (page - 1) * per_page
    rows   = (await db.execute(
        stmt.order_by(CluDecision.confidence.desc().nullslast())
            .offset(offset)
            .limit(per_page)
    )).all()

    items = []
    for decision, cluster_label, primary_keyword in rows:
        items.append({
            **decision.to_dict(),
            "cluster_label":   cluster_label,
            "primary_keyword": primary_keyword,
        })

    return {
        "decisions": items,
        "total":     total,
        "page":      page,
        "per_page":  per_page,
    }


# ---------------------------------------------------------------------------
# Export endpoints
# ---------------------------------------------------------------------------

import csv
import io
import json as _json
from fastapi.responses import StreamingResponse, PlainTextResponse


async def _avg_position_by_cluster(
    db: AsyncSession, project_id: int, cluster_ids: list,
) -> dict:
    """
    Average SERP position across every URL belonging to each cluster.

    CluCluster has no avg_position column of its own (confirmed against
    api/models/clusteriq.py) -- it's only ever computed per-URL, on the
    fly, joining CluSerpData (see get_cluster_detail() above). The two
    export endpoints below used to reference CluCluster.avg_position
    directly, which doesn't exist and raised AttributeError before the
    query could even run -- both endpoints crashed on every call. This
    reuses the same per-URL join pattern, aggregated one level further
    (per cluster instead of per URL).
    """
    if not cluster_ids:
        return {}
    rows = (await db.execute(
        select(
            CluUrlCluster.cluster_id,
            func.avg(CluSerpData.position).label("avg_pos"),
        )
        .join(CluUrl, CluUrl.id == CluUrlCluster.url_id)
        .join(CluSerpData, (CluSerpData.url == CluUrl.url) & (CluSerpData.project_id == project_id))
        .where(CluUrlCluster.cluster_id.in_(cluster_ids))
        .group_by(CluUrlCluster.cluster_id)
    )).all()
    return {
        r.cluster_id: round(float(r.avg_pos), 1) if r.avg_pos is not None else None
        for r in rows
    }


@router.get("/projects/{project_id}/export/decisions.csv")
async def export_decisions_csv(
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Download all KUCD decisions as a CSV file."""
    await _get_project_or_404(project_id, db)

    rows = (await db.execute(
        select(
            CluDecision.cluster_id,
            CluDecision.verdict,
            CluDecision.confidence,
            CluDecision.evidence,
            CluDecision.notes,
            CluCluster.cluster_label,
            CluCluster.primary_keyword,
            CluCluster.primary_url,
            CluCluster.total_impressions,
            CluCluster.url_count,
        )
        .join(CluCluster, CluCluster.id == CluDecision.cluster_id)
        .where(CluCluster.project_id == project_id)
        .order_by(CluDecision.verdict.asc(), CluDecision.confidence.desc().nullslast())
    )).all()

    avg_pos_by_cluster = await _avg_position_by_cluster(db, project_id, [r.cluster_id for r in rows])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "verdict", "confidence_pct", "cluster_label", "primary_keyword",
        "primary_url", "total_impressions", "avg_position", "url_count",
        "evidence", "notes",
    ])
    for r in rows:
        evidence = r.evidence or []
        if isinstance(evidence, str):
            try:
                evidence = _json.loads(evidence)
            except Exception:
                evidence = [evidence]
        avg_pos = avg_pos_by_cluster.get(r.cluster_id)
        writer.writerow([
            r.verdict,
            f"{round((r.confidence or 0) * 100)}%",
            r.cluster_label or "",
            r.primary_keyword or "",
            r.primary_url or "",
            r.total_impressions or 0,
            avg_pos if avg_pos is not None else "",
            r.url_count or 0,
            " | ".join(evidence),
            r.notes or "",
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=\"clusteriq_{project_id}_decisions.csv\""},
    )


@router.get("/projects/{project_id}/export/prune-list.csv")
async def export_prune_list_csv(
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Download all URLs with a Prune verdict as a CSV file."""
    await _get_project_or_404(project_id, db)

    rows = (await db.execute(
        select(
            CluCluster.id,
            CluCluster.primary_url,
            CluCluster.primary_keyword,
            CluCluster.cluster_label,
            CluCluster.total_impressions,
            CluDecision.confidence,
            CluDecision.evidence,
        )
        .join(CluDecision, CluDecision.cluster_id == CluCluster.id)
        .where(
            CluCluster.project_id == project_id,
            CluDecision.verdict == "Prune",
        )
        .order_by(CluCluster.total_impressions.asc().nullslast())
    )).all()

    avg_pos_by_cluster = await _avg_position_by_cluster(db, project_id, [r.id for r in rows])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "url", "primary_keyword", "cluster_label",
        "impressions", "avg_position", "confidence_pct", "reason",
    ])
    for r in rows:
        evidence = r.evidence or []
        if isinstance(evidence, str):
            try:
                evidence = _json.loads(evidence)
            except Exception:
                evidence = [evidence]
        avg_pos = avg_pos_by_cluster.get(r.id)
        writer.writerow([
            r.primary_url or "",
            r.primary_keyword or "",
            r.cluster_label or "",
            r.total_impressions or 0,
            avg_pos if avg_pos is not None else "",
            f"{round((r.confidence or 0) * 100)}%",
            " | ".join(evidence),
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=\"clusteriq_{project_id}_prune_list.csv\""},
    )


@router.get("/projects/{project_id}/export/dev-brief.txt")
async def export_dev_brief_txt(
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Download a plain-text dev handoff document with all recommended
    301 redirects (Consolidate verdict) and prune candidates.
    """
    project = await _get_project_or_404(project_id, db)

    # Consolidate: duplicate pairs with dev briefs
    dup_rows = (await db.execute(
        select(CluDuplicate)
        .where(CluDuplicate.project_id == project_id)
        .order_by(CluDuplicate.similarity_score.desc().nullslast())
    )).scalars().all()

    url_ids = set()
    for d in dup_rows:
        if d.winner_url_id:    url_ids.add(d.winner_url_id)
        if d.duplicate_url_id: url_ids.add(d.duplicate_url_id)
    url_map: dict[int, str] = {}
    if url_ids:
        url_rows = (await db.execute(
            select(CluUrl.id, CluUrl.url).where(CluUrl.id.in_(url_ids))
        )).all()
        url_map = {r.id: r.url for r in url_rows}

    # Prune candidates
    prune_rows = (await db.execute(
        select(CluCluster.primary_url, CluCluster.primary_keyword, CluDecision.evidence)
        .join(CluDecision, CluDecision.cluster_id == CluCluster.id)
        .where(
            CluCluster.project_id == project_id,
            CluDecision.verdict == "Prune",
        )
        .order_by(CluCluster.total_impressions.asc().nullslast())
    )).all()

    lines: list[str] = []
    lines.append(f"ClusterIQ Dev Brief — {project.domain}")
    lines.append(f"Generated: {__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 60)
    lines.append("")

    # ── Section 1: 301 Redirects ──────────────────────────────────
    lines.append("SECTION 1 — RECOMMENDED 301 REDIRECTS")
    lines.append("-" * 40)
    if dup_rows:
        for i, d in enumerate(dup_rows, 1):
            winner = url_map.get(d.winner_url_id or -1, "(unknown)")
            loser  = url_map.get(d.duplicate_url_id or -1, "(unknown)")
            sim    = f"{round((d.similarity_score or 0) * 100)}%" if d.similarity_score else "—"
            lines.append(f"\n[{i}] Similarity: {sim}")
            lines.append(f"    FROM (redirect): {loser}")
            lines.append(f"    TO   (keep):     {winner}")
            if d.winner_reason:
                lines.append(f"    Why keep winner: {d.winner_reason}")
            if d.dev_brief:
                lines.append(f"    Brief: {d.dev_brief}")
    else:
        lines.append("No consolidation pairs detected.")

    lines.append("")
    lines.append("")

    # ── Section 2: Prune Candidates ───────────────────────────────
    lines.append("SECTION 2 — PRUNE CANDIDATES (noindex or delete)")
    lines.append("-" * 40)
    if prune_rows:
        for i, r in enumerate(prune_rows, 1):
            evidence = r.evidence or []
            if isinstance(evidence, str):
                try:
                    evidence = _json.loads(evidence)
                except Exception:
                    evidence = [evidence]
            lines.append(f"\n[{i}] {r.primary_url or '(no URL)'}")
            lines.append(f"    Keyword: {r.primary_keyword or '—'}")
            for bullet in evidence:
                lines.append(f"    • {bullet}")
    else:
        lines.append("No prune candidates detected.")

    lines.append("")
    lines.append("=" * 60)
    lines.append("END OF BRIEF")

    content = "\n".join(lines)
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f"attachment; filename=\"clusteriq_{project_id}_dev_brief.txt\""},
    )


# ---------------------------------------------------------------------------
# Competitor analysis endpoints
# ---------------------------------------------------------------------------

class CompetitorAnalysisRequest(BaseModel):
    competitor_domain: str  = Field(..., min_length=1, max_length=512)
    keyword_limit:     int  = Field(1000, ge=10, le=5000)


class AddToCalendarRequest(BaseModel):
    keyword:          str           = Field(..., min_length=1, max_length=500)
    competitor_url:   Optional[str] = Field(None, max_length=2048)
    volume:           Optional[int] = Field(None, ge=0)
    their_position:   Optional[float] = None


@router.post("/projects/{project_id}/competitors", status_code=202)
@limiter.limit("5/minute")
async def start_competitor_analysis(
    request:    Request,
    project_id: int,
    body:       CompetitorAnalysisRequest,
    db:         AsyncSession = Depends(get_db),
):
    """
    Launch a competitor gap analysis for the given domain.
    Fetches the competitor's organic keywords via Ahrefs, maps them against
    the project's SERP data, and creates "Create" decisions for gaps.
    Returns 202 with a job_id to poll for status.
    """
    project = await _get_project_or_404(project_id, db)

    domain = body.competitor_domain.lower().strip().lstrip("https://").lstrip("http://").rstrip("/")

    # Upsert job row (reset if previous run exists)
    existing_job = (await db.execute(
        select(CluCompetitorJob).where(
            CluCompetitorJob.project_id        == project_id,
            CluCompetitorJob.competitor_domain == domain,
        )
    )).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if existing_job:
        existing_job.status        = "pending"
        existing_job.keywords_fetched        = 0
        existing_job.gap_keywords_found      = 0
        existing_job.create_decisions_added  = 0
        existing_job.error_message           = None
        existing_job.started_at              = None
        existing_job.completed_at            = None
        existing_job.created_at              = now
        await db.commit()
        job_id = existing_job.id
    else:
        job = CluCompetitorJob(
            project_id        = project_id,
            competitor_domain = domain,
            status            = "pending",
            created_at        = now,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = job.id

    from app.modules.clusteriq.services.competitor_analysis import (
        CompetitorClusterAnalyzer, estimate_ahrefs_credits,
    )
    create_tracked_task(
        CompetitorClusterAnalyzer().run_full_analysis(
            project_id, domain,
            keyword_limit=body.keyword_limit,
            job_id=job_id,
        ),
        name=f"clusteriq-competitor-{project_id}-{domain}",
    )

    cost_estimate = estimate_ahrefs_credits(body.keyword_limit)
    logger.info(
        "[Router] competitor analysis started project=%d domain=%s job=%d",
        project_id, domain, job_id,
    )
    return {
        "job_id":                  job_id,
        "competitor_domain":       domain,
        "status":                  "started",
        "estimated_credits":       cost_estimate,
    }


@router.get("/projects/{project_id}/competitors/{competitor_domain}/status")
async def get_competitor_job_status(
    project_id:        int,
    competitor_domain: str,
    db: AsyncSession = Depends(get_db),
):
    """Return the current status of a competitor analysis job."""
    await _get_project_or_404(project_id, db)
    domain = competitor_domain.lower().strip()

    job = (await db.execute(
        select(CluCompetitorJob).where(
            CluCompetitorJob.project_id        == project_id,
            CluCompetitorJob.competitor_domain == domain,
        ).order_by(CluCompetitorJob.created_at.desc())
    )).scalar_one_or_none()

    if job is None:
        return {"status": "not_started", "competitor_domain": domain}
    return job.to_dict()


@router.get("/projects/{project_id}/competitors/{competitor_domain}/gap")
async def get_competitor_gap(
    project_id:        int,
    competitor_domain: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Return the full gap report for a competitor domain.
    Returns {status: "pending"/"running"} if the job is not yet done.
    Returns {status: "not_started"} if no job has been launched.
    """
    await _get_project_or_404(project_id, db)
    domain = competitor_domain.lower().strip()

    # Check job status first
    job = (await db.execute(
        select(CluCompetitorJob).where(
            CluCompetitorJob.project_id        == project_id,
            CluCompetitorJob.competitor_domain == domain,
        ).order_by(CluCompetitorJob.created_at.desc())
    )).scalar_one_or_none()

    if job is None:
        return {"status": "not_started", "competitor_domain": domain}
    if job.status in ("pending", "running"):
        return {
            "status":            job.status,
            "competitor_domain": domain,
            "keywords_fetched":  job.keywords_fetched or 0,
        }
    if job.status == "error":
        return {
            "status":        "error",
            "error_message": job.error_message,
        }

    from app.modules.clusteriq.services.competitor_analysis import CompetitorClusterAnalyzer
    report = await CompetitorClusterAnalyzer().generate_gap_report(project_id, domain)
    return report


@router.post("/projects/{project_id}/competitors/{competitor_domain}/add-to-calendar", status_code=201)
@limiter.limit("30/minute")
async def add_gap_keyword_to_calendar(
    request:           Request,
    project_id:        int,
    competitor_domain: str,
    body:              AddToCalendarRequest,
    db:                AsyncSession = Depends(get_db),
):
    """
    Create a standalone ContentBrief (audit_id=None) for a gap keyword so
    it appears in the existing briefs list, ready for LLM enrichment.
    """
    await _get_project_or_404(project_id, db)
    domain = competitor_domain.lower().strip()

    from api.models.content import ContentBrief
    import json as _json_local

    pos_str = f"#{round(body.their_position, 1)}" if body.their_position else "top"
    vol_str = f" ({body.volume:,} searches/mo)" if body.volume else ""
    summary = (
        f"ClusterIQ gap opportunity: {domain} ranks {pos_str} "
        f"for '{body.keyword}'{vol_str}. "
        "Recommendation: create a new page targeting this keyword."
    )
    brief_context = {
        "source":            "clusteriq_competitor",
        "keyword":           body.keyword,
        "competitor_domain": domain,
        "competitor_url":    body.competitor_url or "",
        "competitor_position": body.their_position,
        "volume":            body.volume,
        "clusteriq_project_id": project_id,
    }

    brief = ContentBrief(
        audit_id          = None,
        result_id         = None,
        page_url          = body.keyword,
        status            = "pending",
        priority          = "high",
        executive_summary = summary,
        brief_json        = _json_local.dumps(brief_context, ensure_ascii=False),
    )
    db.add(brief)
    await db.commit()
    await db.refresh(brief)

    logger.info(
        "[Router] gap keyword added to calendar: project=%d domain=%s keyword=%r brief_id=%d",
        project_id, domain, body.keyword, brief.id,
    )
    return {"brief_id": brief.id, "keyword": body.keyword, "status": "created"}


# ---------------------------------------------------------------------------
# ContentIQ Bridge — generate a ContentBrief from a cluster verdict
# ---------------------------------------------------------------------------

@router.post("/clusters/{cluster_id}/generate-brief", status_code=201)
@limiter.limit("20/minute")
async def generate_cluster_brief(
    request:    Request,
    cluster_id: int,
    db:         AsyncSession = Depends(get_db),
):
    """
    Create a ContentBrief in ContentIQ for a ClusterIQ cluster.

    Supported verdicts: Optimize, Create, Consolidate.
    For Consolidate clusters without an existing LLM brief, also generates
    one via Claude before creating the ContentBrief (best-effort).

    Returns 201 { brief_id, redirect_url }.
    """
    # Verify cluster exists
    cluster = (await db.execute(
        select(CluCluster).where(CluCluster.id == cluster_id)
    )).scalar_one_or_none()
    if cluster is None:
        raise_not_found("Cluster", cluster_id)

    decision = (await db.execute(
        select(CluDecision)
        .where(CluDecision.cluster_id == cluster_id)
        .order_by(CluDecision.created_at.desc())
    )).scalar_one_or_none()

    if decision is None:
        from api.utils.errors import raise_bad_request as _bad
        _bad("No decision found for this cluster. Run the pipeline first.")

    verdict = decision.verdict
    if verdict not in ("Optimize", "Create", "Consolidate"):
        from api.utils.errors import raise_bad_request as _bad
        _bad(
            f"generate-brief only supports Optimize/Create/Consolidate verdicts, "
            f"got '{verdict}'"
        )

    # For Consolidate without an LLM brief — generate one first (best-effort)
    if verdict == "Consolidate" and not decision.notes:
        try:
            from app.modules.clusteriq.services.decision_engine import ClusterDecisionEngine
            await ClusterDecisionEngine().generate_llm_evidence(cluster_id)
        except Exception as _llm_exc:
            logger.warning(
                "[Router] LLM evidence generation failed (non-fatal) cluster=%d: %s",
                cluster_id, _llm_exc,
            )

    # Create the ContentBrief
    from app.modules.clusteriq.services.contentiq_bridge import create_contentiq_brief_from_cluster
    try:
        brief_id = await create_contentiq_brief_from_cluster(cluster_id)
    except ValueError as exc:
        from api.utils.errors import raise_bad_request as _bad
        _bad(str(exc))

    logger.info(
        "[Router] ContentBrief #%d created for cluster=%d via generate-brief",
        brief_id, cluster_id,
    )
    return {
        "brief_id":     brief_id,
        "redirect_url": f"/briefs",
        "status":       "created",
    }
