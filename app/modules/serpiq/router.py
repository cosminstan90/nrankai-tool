"""
SerpIQ — FastAPI Router
========================
Endpoints:

  POST   /api/serpiq/snapshot              — run analysis, return full snapshot
  GET    /api/serpiq/history               — paginated list of past snapshots
  GET    /api/serpiq/snapshots/{id}        — single snapshot with SERP items
  DELETE /api/serpiq/snapshots/{id}        — delete snapshot (204)
  GET    /serpiq                           — SPA HTML page
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.limiter import limiter
from api.models.database import get_db
from app.modules.serpiq.models import SiqSnapshot, SiqSerpItem
from app.modules.serpiq.schemas import (
    SerpiqAnalyzeRequest,
    SnapshotListItem,
    SnapshotListResponse,
)
from app.modules.serpiq.services.orchestrator import SerpIQOrchestrator

logger = logging.getLogger("serpiq.router")

router = APIRouter(prefix="/api/serpiq", tags=["SerpIQ"])

_orchestrator = SerpIQOrchestrator()


# ---------------------------------------------------------------------------
# POST /api/serpiq/snapshot — run analysis
# ---------------------------------------------------------------------------

@router.post("/snapshot", status_code=201)
@limiter.limit("10/minute")
async def create_snapshot(
    request: Request,
    body:    SerpiqAnalyzeRequest,
    db:      AsyncSession = Depends(get_db),
):
    """
    Kick off a SerpIQ analysis for a URL or keyword.

    - ``input_type`` is derived automatically: if the value starts with
      ``http://`` or ``https://`` it is treated as a URL, otherwise keyword.
    - ``generate_brief`` triggers an optional Claude 150-word brief (adds ~2s).

    Returns the full snapshot including SERP items.
    """
    # Auto-detect input_type
    val = body.input_value.strip()
    input_type = "url" if val.startswith(("http://", "https://")) else "keyword"

    result = await _orchestrator.run_snapshot(
        input_type=input_type,
        input_value=val,
        location_code=body.location_code,
        language_code=body.language_code,
        generate_brief=body.generate_brief,
        user_id=None,
    )

    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])

    return result


# ---------------------------------------------------------------------------
# GET /api/serpiq/history — paginated history
# ---------------------------------------------------------------------------

@router.get("/history", response_model=SnapshotListResponse)
@limiter.limit("30/minute")
async def list_snapshots(
    request:    Request,
    input_type: Optional[str] = Query(None, description="Filter by 'url' or 'keyword'"),
    page:       int           = Query(1,    ge=1),
    per_page:   int           = Query(20,   ge=1, le=100),
    db:         AsyncSession  = Depends(get_db),
):
    """Return a paginated list of snapshots (newest first)."""
    base_q = select(SiqSnapshot)
    count_q = select(func.count(SiqSnapshot.id))

    if input_type in ("url", "keyword"):
        base_q  = base_q.where(SiqSnapshot.input_type  == input_type)
        count_q = count_q.where(SiqSnapshot.input_type == input_type)

    total = (await db.execute(count_q)).scalar() or 0

    rows = (
        await db.execute(
            base_q.order_by(desc(SiqSnapshot.created_at))
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()

    items = [
        SnapshotListItem(
            id=s.id,
            input_type=s.input_type,
            input_value=s.input_value,
            keyword=s.keyword,
            verdict=s.verdict,
            position_found=s.position_found,
            location_code=s.location_code,
            language_code=s.language_code,
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in rows
    ]

    return SnapshotListResponse(items=items, total=total, page=page, per_page=per_page)


# ---------------------------------------------------------------------------
# GET /api/serpiq/snapshots/{snapshot_id} — single snapshot
# ---------------------------------------------------------------------------

@router.get("/snapshots/{snapshot_id}")
@limiter.limit("30/minute")
async def get_snapshot(
    request:     Request,
    snapshot_id: int,
    db:          AsyncSession = Depends(get_db),
):
    """Return full snapshot including all SERP items."""
    snapshot = await db.get(SiqSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    # Load items
    items_result = await db.execute(
        select(SiqSerpItem)
        .where(SiqSerpItem.snapshot_id == snapshot_id)
        .order_by(SiqSerpItem.position)
    )
    snapshot.serp_items = items_result.scalars().all()

    return snapshot.to_dict(include_items=True)


# ---------------------------------------------------------------------------
# DELETE /api/serpiq/snapshots/{snapshot_id} — delete snapshot
# ---------------------------------------------------------------------------

@router.delete("/snapshots/{snapshot_id}", status_code=204)
@limiter.limit("20/minute")
async def delete_snapshot(
    request:     Request,
    snapshot_id: int,
    db:          AsyncSession = Depends(get_db),
):
    """Delete a snapshot (cascades to serp items)."""
    snapshot = await db.get(SiqSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    await db.delete(snapshot)
    await db.commit()
