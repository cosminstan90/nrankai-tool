"""
Before/After Tracking — Monitor score improvements over time.

Zero LLM cost: purely compares audit scores across snapshots.
Creates tracking projects that link multiple audits for the same website,
calculates deltas automatically, and shows progress charts.
"""

import json
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from api.utils.errors import raise_not_found
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import (
    get_db, AsyncSessionLocal,
    Audit, AuditResult,
    TrackingProject, TrackingSnapshot
)

router = APIRouter(prefix="/api/tracking", tags=["tracking"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    website: str = Field(..., min_length=1)
    audit_type: Optional[str] = None
    description: Optional[str] = None
    baseline_audit_id: str = Field(..., description="Audit ID to use as baseline")


class AddSnapshotRequest(BaseModel):
    audit_id: str = Field(..., description="Audit ID for this snapshot")
    label: Optional[str] = None
    notes: Optional[str] = None


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


# ============================================================================
# HELPERS
# ============================================================================

async def _get_audit_score_data(session: AsyncSession, audit_id: str):
    """Get average score and per-page scores from an audit."""
    audit = (await session.execute(
        select(Audit).where(Audit.id == audit_id)
    )).scalar_one_or_none()
    
    if not audit:
        return None, None, None, None
    
    results = (await session.execute(
        select(AuditResult).where(AuditResult.audit_id == audit_id)
    )).scalars().all()
    
    page_scores = []
    for r in results:
        page_scores.append({
            "page_url": r.page_url,
            "score": r.score,
            "classification": r.classification
        })
    
    avg_score = audit.average_score
    if avg_score is None and page_scores:
        scores = [p["score"] for p in page_scores if p["score"] is not None]
        avg_score = sum(scores) / len(scores) if scores else None
    
    return avg_score, len(results), page_scores, audit.completed_at or audit.created_at


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("", status_code=201)
async def create_project(request: CreateProjectRequest, db: AsyncSession = Depends(get_db)):
    """Create a tracking project with a baseline audit."""
    
    # Validate baseline audit exists
    score, pages, page_scores, audit_date = await _get_audit_score_data(db, request.baseline_audit_id)
    if score is None and pages is None:
        raise_not_found("Baseline audit", request.baseline_audit_id)
    
    import uuid
    project_id = str(uuid.uuid4())
    
    project = TrackingProject(
        id=project_id,
        name=request.name,
        website=request.website,
        audit_type=request.audit_type,
        description=request.description,
        baseline_audit_id=request.baseline_audit_id,
        baseline_score=score,
        baseline_date=audit_date,
        current_audit_id=request.baseline_audit_id,
        current_score=score,
        current_date=audit_date,
        score_delta=0.0
    )
    db.add(project)
    
    # Create baseline snapshot
    snapshot = TrackingSnapshot(
        project_id=project_id,
        audit_id=request.baseline_audit_id,
        label="Baseline",
        score=score,
        pages_analyzed=pages,
        delta_from_previous=0.0,
        delta_from_baseline=0.0,
        page_scores_json=json.dumps(page_scores, ensure_ascii=False),
        notes="Initial baseline measurement"
    )
    db.add(snapshot)
    
    await db.commit()
    await db.refresh(project)
    
    return project.to_dict()


@router.post("/{project_id}/snapshots", status_code=201)
async def add_snapshot(project_id: str, request: AddSnapshotRequest, db: AsyncSession = Depends(get_db)):
    """Add a new snapshot (milestone) to a tracking project."""
    
    project = (await db.execute(
        select(TrackingProject).where(TrackingProject.id == project_id)
    )).scalar_one_or_none()
    
    if not project:
        raise_not_found("Tracking project")
    
    # Get score data from the audit
    score, pages, page_scores, audit_date = await _get_audit_score_data(db, request.audit_id)
    if score is None and pages is None:
        raise_not_found("Audit", request.audit_id)
    
    # Calculate deltas
    delta_from_baseline = round(score - project.baseline_score, 2) if score and project.baseline_score else None
    
    # Get previous snapshot for delta
    prev = (await db.execute(
        select(TrackingSnapshot)
        .where(TrackingSnapshot.project_id == project_id)
        .order_by(desc(TrackingSnapshot.created_at))
        .limit(1)
    )).scalar_one_or_none()
    
    delta_from_previous = round(score - prev.score, 2) if score and prev and prev.score else None
    
    # Auto-label if not provided
    existing_count = (await db.execute(
        select(func.count(TrackingSnapshot.id))
        .where(TrackingSnapshot.project_id == project_id)
    )).scalar()
    
    label = request.label or f"Snapshot #{existing_count + 1}"
    
    snapshot = TrackingSnapshot(
        project_id=project_id,
        audit_id=request.audit_id,
        label=label,
        score=score,
        pages_analyzed=pages,
        delta_from_previous=delta_from_previous,
        delta_from_baseline=delta_from_baseline,
        page_scores_json=json.dumps(page_scores, ensure_ascii=False),
        notes=request.notes
    )
    db.add(snapshot)
    
    # Update project current state
    project.current_audit_id = request.audit_id
    project.current_score = score
    project.current_date = audit_date
    project.score_delta = delta_from_baseline
    
    await db.commit()
    
    return snapshot.to_dict()


@router.get("")
async def list_projects(
    status: Optional[str] = Query(None),
    website: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """List all tracking projects."""
    query = select(TrackingProject).order_by(desc(TrackingProject.updated_at))
    
    if status:
        query = query.where(TrackingProject.status == status)
    if website:
        query = query.where(TrackingProject.website.contains(website))
    
    result = await db.execute(query)
    projects = result.scalars().all()
    
    return [p.to_dict() for p in projects]


@router.get("/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """Get a tracking project with all snapshots."""
    project = (await db.execute(
        select(TrackingProject).where(TrackingProject.id == project_id)
    )).scalar_one_or_none()
    
    if not project:
        raise_not_found("Tracking project")
    
    # Get snapshots
    snapshots = (await db.execute(
        select(TrackingSnapshot)
        .where(TrackingSnapshot.project_id == project_id)
        .order_by(TrackingSnapshot.created_at)
    )).scalars().all()
    
    data = project.to_dict()
    data["snapshots"] = [s.to_dict() for s in snapshots]
    
    return data


@router.patch("/{project_id}")
async def update_project(project_id: str, request: UpdateProjectRequest, db: AsyncSession = Depends(get_db)):
    """Update project name, description, or status."""
    project = (await db.execute(
        select(TrackingProject).where(TrackingProject.id == project_id)
    )).scalar_one_or_none()
    
    if not project:
        raise_not_found("Tracking project")
    
    if request.name is not None:
        project.name = request.name
    if request.description is not None:
        project.description = request.description
    if request.status is not None:
        project.status = request.status
    
    await db.commit()
    return project.to_dict()


@router.delete("/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a tracking project and all its snapshots."""
    project = (await db.execute(
        select(TrackingProject).where(TrackingProject.id == project_id)
    )).scalar_one_or_none()
    
    if not project:
        raise_not_found("Tracking project")
    
    await db.delete(project)
    await db.commit()
    
    return {"status": "deleted", "id": project_id}


@router.get("/{project_id}/compare")
async def compare_snapshots(
    project_id: str,
    snapshot_a: int = Query(..., description="First snapshot ID"),
    snapshot_b: int = Query(..., description="Second snapshot ID"),
    db: AsyncSession = Depends(get_db)
):
    """Compare two snapshots page-by-page."""
    snap_a = (await db.execute(
        select(TrackingSnapshot).where(TrackingSnapshot.id == snapshot_a)
    )).scalar_one_or_none()
    
    snap_b = (await db.execute(
        select(TrackingSnapshot).where(TrackingSnapshot.id == snapshot_b)
    )).scalar_one_or_none()
    
    if not snap_a or not snap_b:
        raise_not_found("Snapshot")
    
    # Build page-level comparison
    pages_a = {p["page_url"]: p for p in (json.loads(snap_a.page_scores_json) if snap_a.page_scores_json else [])}
    pages_b = {p["page_url"]: p for p in (json.loads(snap_b.page_scores_json) if snap_b.page_scores_json else [])}
    
    all_urls = sorted(set(list(pages_a.keys()) + list(pages_b.keys())))
    
    comparison = []
    improved = 0
    declined = 0
    unchanged = 0
    
    for url in all_urls:
        a_score = pages_a.get(url, {}).get("score")
        b_score = pages_b.get(url, {}).get("score")
        delta = round(b_score - a_score, 2) if a_score is not None and b_score is not None else None
        
        if delta is not None:
            if delta > 0:
                improved += 1
            elif delta < 0:
                declined += 1
            else:
                unchanged += 1
        
        comparison.append({
            "page_url": url,
            "score_a": a_score,
            "score_b": b_score,
            "delta": delta
        })
    
    # Sort by delta descending (biggest improvements first)
    comparison.sort(key=lambda x: x["delta"] if x["delta"] is not None else 0, reverse=True)
    
    return {
        "snapshot_a": snap_a.to_dict(),
        "snapshot_b": snap_b.to_dict(),
        "overall_delta": round(snap_b.score - snap_a.score, 2) if snap_a.score and snap_b.score else None,
        "pages_improved": improved,
        "pages_declined": declined,
        "pages_unchanged": unchanged,
        "pages": comparison
    }


@router.get("/audits-for-website/{website:path}")
async def get_audits_for_website(website: str, db: AsyncSession = Depends(get_db)):
    """Get completed audits for a website (for snapshot selection)."""
    result = await db.execute(
        select(Audit)
        .where(Audit.website.contains(website), Audit.status == "completed")
        .order_by(desc(Audit.completed_at))
        .limit(50)
    )
    audits = result.scalars().all()
    
    return [{
        "id": a.id,
        "audit_type": a.audit_type,
        "average_score": a.average_score,
        "pages_analyzed": a.pages_analyzed,
        "completed_at": a.completed_at.isoformat() if a.completed_at else None
    } for a in audits]
