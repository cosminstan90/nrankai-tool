"""
Cross-Reference Analysis API route.

Triggers and serves site-wide pattern analysis from the web dashboard.
Wraps cross_reference_analyzer.py as an async background task.

Job state is persisted in the cross_reference_jobs DB table so that
status survives server restarts (replaces the old in-memory _jobs dict).
"""

import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from api.utils.errors import raise_not_found
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import (
    AsyncSessionLocal,
    Audit,
    AuditResult,
    CrossReferenceJob,
    get_db,
)

router = APIRouter(prefix="/api/cross-reference", tags=["cross_reference"])

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _project_root() -> Path:
    return Path(__file__).parent.parent.parent


def _result_path(website: str, audit_type: str, no_llm: bool = False) -> Path:
    suffix = "_RULE_BASED_ONLY" if no_llm else ""
    output_dir = _project_root() / website / f"output_{audit_type.lower()}"
    return output_dir / f"CROSS_REFERENCE_ANALYSIS{suffix}.json"


def _result_meta(path: Path) -> Optional[Dict]:
    if not path.exists():
        return None
    stat = path.stat()
    return {
        "last_modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        "size_kb": round(stat.st_size / 1024, 1),
    }


# ─── Background task ──────────────────────────────────────────────────────────

async def _run_cross_ref(
    job_id: str,
    website: str,
    audit_type: str,
    no_llm: bool,
    provider: Optional[str],
    model: Optional[str],
):
    """Background task: run the cross-reference analyzer and persist job state."""
    async with AsyncSessionLocal() as session:
        job = await session.get(CrossReferenceJob, job_id)
        if job:
            job.status = "running"
            job.started_at = datetime.utcnow()
            await session.commit()

    try:
        from core.cross_reference_analyzer import run_cross_reference_analysis

        output_path = await run_cross_reference_analysis(
            website=website,
            audit_type=audit_type,
            no_llm=no_llm,
            provider=provider,
            model_name=model,
        )

        async with AsyncSessionLocal() as session:
            job = await session.get(CrossReferenceJob, job_id)
            if job:
                job.status = "completed"
                job.output_path = str(output_path) if output_path else None
                job.completed_at = datetime.utcnow()
                await session.commit()

    except Exception as exc:
        async with AsyncSessionLocal() as session:
            job = await session.get(CrossReferenceJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)
                job.completed_at = datetime.utcnow()
                await session.commit()


# ─── Request models ───────────────────────────────────────────────────────────

class CrossRefRunRequest(BaseModel):
    website: str
    audit_type: str
    no_llm: bool = False
    provider: Optional[str] = None
    model: Optional[str] = None


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/run")
async def run_cross_reference(
    req: CrossRefRunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Queue a cross-reference analysis as a background task."""
    job = CrossReferenceJob(
        id=str(uuid.uuid4()),
        website=req.website,
        audit_type=req.audit_type,
        no_llm=int(req.no_llm),
        provider=req.provider,
        model=req.model,
        status="queued",
    )
    db.add(job)
    await db.commit()

    background_tasks.add_task(
        _run_cross_ref,
        job.id,
        req.website,
        req.audit_type,
        req.no_llm,
        req.provider,
        req.model,
    )
    return {"job_id": job.id, "status": "queued"}


@router.get("/status/{job_id}")
async def job_status(job_id: str, db: AsyncSession = Depends(get_db)):
    """Check status of a running/completed cross-reference job."""
    job = await db.get(CrossReferenceJob, job_id)
    if not job:
        raise_not_found("Job")
    return job.to_dict()


@router.get("/jobs")
async def list_jobs(
    website: Optional[str] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """List recent cross-reference jobs, optionally filtered by website."""
    stmt = select(CrossReferenceJob).order_by(desc(CrossReferenceJob.created_at)).limit(limit)
    if website:
        stmt = stmt.where(CrossReferenceJob.website == website)
    rows = (await db.execute(stmt)).scalars().all()
    return [r.to_dict() for r in rows]


@router.get("/results")
async def get_results(website: str, audit_type: str, no_llm: bool = False):
    """Return the most recent cross-reference JSON for a website+audit_type."""
    path = _result_path(website, audit_type, no_llm)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No cross-reference analysis found for {website} / {audit_type}",
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["_meta"] = _result_meta(path)
    return data


@router.get("/sites")
async def list_sites(db: AsyncSession = Depends(get_db)):
    """
    List all (website, audit_type) pairs that have ≥1 completed audit run,
    plus whether a cross-reference analysis file already exists.
    """
    stmt = (
        select(
            Audit.website,
            Audit.audit_type,
            func.count(Audit.id).label("run_count"),
            func.max(Audit.completed_at).label("last_run"),
        )
        .where(Audit.status == "completed")
        .group_by(Audit.website, Audit.audit_type)
        .order_by(Audit.website, Audit.audit_type)
    )
    rows = (await db.execute(stmt)).fetchall()

    results: List[Dict] = []
    for website, audit_type, run_count, last_run in rows:
        full_meta = _result_meta(_result_path(website, audit_type, no_llm=False))
        lite_meta = _result_meta(_result_path(website, audit_type, no_llm=True))
        results.append(
            {
                "website": website,
                "audit_type": audit_type,
                "run_count": run_count,
                "last_run": last_run.strftime("%Y-%m-%d") if last_run else None,
                "full_analysis": full_meta,
                "lite_analysis": lite_meta,
            }
        )
    return results
