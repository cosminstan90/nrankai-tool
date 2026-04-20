"""
Fan-Out Project Management (Prompt 25)
=======================================
CRUD for FanoutProject + per-project dashboard stats.

Endpoints:
  POST   /api/projects
  GET    /api/projects
  GET    /api/projects/{id}
  GET    /api/projects/{id}/dashboard
  PUT    /api/projects/{id}
  DELETE /api/projects/{id}          — soft delete
  POST   /api/projects/{id}/quick-analyze
  GET    /api/projects/{id}/benchmark-comparison  (Prompt 24)
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import (
    get_db, AsyncSessionLocal,
    FanoutProject, FanoutTrackingConfig, FanoutTrackingRun,
    FanoutSession, FanoutCompetitiveReport, GeoBenchmark,
)
from api.utils.errors import raise_not_found

logger = logging.getLogger("projects")
router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str
    client_name: Optional[str] = None
    target_domain: str
    target_brand: str
    vertical: str = "generic"
    locale: str = "en-US"
    language: str = "en"
    gl: str = "us"
    color: str = "#6366f1"
    notes: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    client_name: Optional[str] = None
    target_domain: Optional[str] = None
    target_brand: Optional[str] = None
    vertical: Optional[str] = None
    locale: Optional[str] = None
    language: Optional[str] = None
    gl: Optional[str] = None
    color: Optional[str] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _project_stats(project_id: str, db: AsyncSession) -> dict:
    """Compute ProjectStats for a single project."""
    # Active tracking configs
    active_configs = (await db.execute(
        select(func.count(FanoutTrackingConfig.id)).where(
            FanoutTrackingConfig.project_id == project_id,
            FanoutTrackingConfig.is_active == True,  # noqa: E712
        )
    )).scalar_one()

    # Total sessions
    total_sessions = (await db.execute(
        select(func.count(FanoutSession.id)).where(FanoutSession.audit_id == project_id)
    )).scalar_one()

    # Latest completed tracking run across all configs for this project
    latest_run = (await db.execute(
        select(FanoutTrackingRun)
        .join(FanoutTrackingConfig, FanoutTrackingRun.config_id == FanoutTrackingConfig.id)
        .where(
            FanoutTrackingConfig.project_id == project_id,
            FanoutTrackingRun.status == "completed",
        )
        .order_by(desc(FanoutTrackingRun.created_at))
        .limit(1)
    )).scalar_one_or_none()

    mention_rate     = None
    composite_score  = None
    last_run_at      = None
    trend            = None
    if latest_run:
        mention_rate    = latest_run.mention_rate
        composite_score = latest_run.composite_score
        last_run_at     = latest_run.created_at.isoformat() if latest_run.created_at else None

        # Trend vs previous run
        prev_run = (await db.execute(
            select(FanoutTrackingRun)
            .join(FanoutTrackingConfig, FanoutTrackingRun.config_id == FanoutTrackingConfig.id)
            .where(
                FanoutTrackingConfig.project_id == project_id,
                FanoutTrackingRun.status == "completed",
                FanoutTrackingRun.id != latest_run.id,
            )
            .order_by(desc(FanoutTrackingRun.created_at))
            .limit(1)
        )).scalar_one_or_none()

        if prev_run and prev_run.mention_rate is not None and mention_rate is not None:
            delta = mention_rate - prev_run.mention_rate
            if delta > 0.05:
                trend = "up"
            elif delta < -0.05:
                trend = "down"
            else:
                trend = "stable"

    return {
        "total_sessions":         total_sessions,
        "active_tracking_configs": active_configs,
        "last_tracking_run":       last_run_at,
        "latest_mention_rate":     mention_rate,
        "latest_composite_score":  composite_score,
        "trend":                   trend,
    }


# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.post("")
async def create_project(req: ProjectCreate, db: AsyncSession = Depends(get_db)):
    """Create a new fan-out project."""
    proj = FanoutProject(
        name          = req.name,
        client_name   = req.client_name,
        target_domain = req.target_domain.lower().strip().rstrip("/"),
        target_brand  = req.target_brand,
        vertical      = req.vertical,
        locale        = req.locale,
        language      = req.language,
        gl            = req.gl,
        color         = req.color,
        notes         = req.notes,
    )
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    stats = await _project_stats(proj.id, db)
    return {**proj.to_dict(), "stats": stats}


@router.get("")
async def list_projects(
    is_active: Optional[bool] = None,
    vertical: Optional[str]   = None,
    db: AsyncSession = Depends(get_db),
):
    """List all projects with stats."""
    stmt = select(FanoutProject).order_by(desc(FanoutProject.created_at))
    if is_active is not None:
        stmt = stmt.where(FanoutProject.is_active == is_active)
    if vertical:
        stmt = stmt.where(FanoutProject.vertical == vertical)

    projects = (await db.execute(stmt)).scalars().all()
    result = []
    for p in projects:
        stats = await _project_stats(p.id, db)
        result.append({**p.to_dict(), "stats": stats})
    return result


@router.get("/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single project with full stats."""
    proj = await db.get(FanoutProject, project_id)
    if not proj:
        raise_not_found("Project")
    stats = await _project_stats(proj.id, db)
    return {**proj.to_dict(), "stats": stats}


@router.get("/{project_id}/dashboard")
async def project_dashboard(project_id: str, db: AsyncSession = Depends(get_db)):
    """Project dashboard: KPIs + recent sessions + tracking configs + content gaps."""
    proj = await db.get(FanoutProject, project_id)
    if not proj:
        raise_not_found("Project")

    stats = await _project_stats(proj.id, db)

    # Tracking configs
    configs = (await db.execute(
        select(FanoutTrackingConfig)
        .where(FanoutTrackingConfig.project_id == project_id)
        .order_by(desc(FanoutTrackingConfig.created_at))
        .limit(5)
    )).scalars().all()

    # Recent sessions — use project_id stored on session (we store it in engine field hack or audit_id)
    recent_sessions = (await db.execute(
        select(FanoutSession)
        .where(FanoutSession.audit_id == project_id)
        .order_by(desc(FanoutSession.created_at))
        .limit(10)
    )).scalars().all()

    # Timeline (last 30 days of runs)
    runs_30d = (await db.execute(
        select(FanoutTrackingRun)
        .join(FanoutTrackingConfig, FanoutTrackingRun.config_id == FanoutTrackingConfig.id)
        .where(
            FanoutTrackingConfig.project_id == project_id,
            FanoutTrackingRun.status == "completed",
        )
        .order_by(FanoutTrackingRun.created_at)
        .limit(30)
    )).scalars().all()

    return {
        "project":          proj.to_dict(),
        "stats":            stats,
        "tracking_configs": [c.to_dict() for c in configs],
        "recent_sessions":  [s.to_dict() for s in recent_sessions],
        "timeline": [
            {
                "run_date":       r.run_date,
                "mention_rate":   r.mention_rate,
                "composite_score": r.composite_score,
                "cost_usd":       r.cost_usd,
                "created_at":     r.created_at.isoformat() if r.created_at else None,
            }
            for r in runs_30d
        ],
    }


@router.put("/{project_id}")
async def update_project(
    project_id: str,
    req: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update project fields."""
    proj = await db.get(FanoutProject, project_id)
    if not proj:
        raise_not_found("Project")

    for field, value in req.model_dump(exclude_none=True).items():
        setattr(proj, field, value)
    proj.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(proj)
    stats = await _project_stats(proj.id, db)
    return {**proj.to_dict(), "stats": stats}


@router.delete("/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """Soft-delete a project (sets is_active=False)."""
    proj = await db.get(FanoutProject, project_id)
    if not proj:
        raise_not_found("Project")
    proj.is_active  = False
    proj.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True, "id": project_id}


@router.post("/{project_id}/quick-analyze")
async def quick_analyze(
    project_id: str,
    prompt: str,
    db: AsyncSession = Depends(get_db),
):
    """Run a fan-out analysis using project's settings (locale/language/gl)."""
    proj = await db.get(FanoutProject, project_id)
    if not proj:
        raise_not_found("Project")

    from api.workers.fanout_analyzer import analyze_prompt, PROVIDER_DEFAULTS
    from api.routes.fanout import _save_fanout_result

    result = await analyze_prompt(prompt, provider="openai")
    async with AsyncSessionLocal() as db2:
        session_id = await _save_fanout_result(
            db2, result,
            target_url=f"https://{proj.target_domain}",
            audit_id=project_id,
        )
    return {"session_id": session_id, "prompt": prompt, "project_id": project_id}


# ── Benchmark comparison (Prompt 24) ─────────────────────────────────────────

@router.get("/{project_id}/benchmark-comparison")
async def benchmark_comparison(project_id: str, db: AsyncSession = Depends(get_db)):
    """Compare project's latest metrics against vertical benchmarks."""
    proj = await db.get(FanoutProject, project_id)
    if not proj:
        raise_not_found("Project")

    stats         = await _project_stats(proj.id, db)
    mention_rate  = stats.get("latest_mention_rate")
    period_month  = datetime.now(timezone.utc).strftime("%Y-%m")

    bm = (await db.execute(
        select(GeoBenchmark).where(
            GeoBenchmark.vertical    == (proj.vertical or "generic"),
            GeoBenchmark.locale      == (proj.locale or "en-US"),
            GeoBenchmark.period_month == period_month,
        )
    )).scalar_one_or_none()

    if not bm or bm.sample_size < 3:
        return {"available": False, "reason": "insufficient_data", "project_id": project_id}

    if mention_rate is None:
        return {"available": False, "reason": "no_tracking_data", "project_id": project_id}

    # Percentile rank
    rates = [bm.p25_mention_rate, bm.median_mention_rate, bm.p75_mention_rate]
    if mention_rate >= (bm.p75_mention_rate or 1):
        percentile_rank = 75
        grade = "top"
    elif mention_rate >= (bm.median_mention_rate or 0.5):
        percentile_rank = 50
        grade = "above_median"
    elif mention_rate >= (bm.p25_mention_rate or 0.25):
        percentile_rank = 25
        grade = "below_median"
    else:
        percentile_rank = 10
        grade = "bottom"

    gap_to_top = max(0.0, (bm.p75_mention_rate or 0) - mention_rate)

    return {
        "available":        True,
        "project_id":       project_id,
        "vertical":         proj.vertical,
        "locale":           proj.locale,
        "period_month":     period_month,
        "your_mention_rate": mention_rate,
        "benchmark": {
            "p25":    bm.p25_mention_rate,
            "median": bm.median_mention_rate,
            "p75":    bm.p75_mention_rate,
            "avg":    bm.avg_mention_rate,
            "sample_size": bm.sample_size,
        },
        "percentile_rank":  percentile_rank,
        "grade":            grade,
        "gap_to_top":       round(gap_to_top, 3),
        "message": f"Better than ~{percentile_rank}% of {proj.vertical} projects in {proj.locale}",
    }


# ── Benchmark admin (Prompt 24) ───────────────────────────────────────────────

@router.post("/admin/benchmarks/geo/recalculate")
async def recalculate_geo_benchmarks(db: AsyncSession = Depends(get_db)):
    """Trigger an on-demand recalculation of GEO vertical benchmarks."""
    from api.workers.benchmark_calculator import calculate_geo_benchmarks
    result = await calculate_geo_benchmarks(db)
    return {"ok": True, **result}
