"""
Co-citation Map routes (Prompt 34)
=====================================
POST /api/cocitation/analyze
GET  /api/cocitation/{project_id}/latest
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, CocitationMap
from api.utils.errors import raise_not_found

logger = logging.getLogger("cocitation")
router = APIRouter(prefix="/api/cocitation", tags=["cocitation"])


class CocitationRequest(BaseModel):
    project_id: Optional[str] = None
    target_brand: str
    target_domain: str
    session_ids: Optional[List[str]] = None
    period_days: int = 30


@router.post("/analyze")
async def analyze_cocitations(req: CocitationRequest, db: AsyncSession = Depends(get_db)):
    """
    Build a co-citation map for target_domain across recent fan-out sessions.
    $0 cost — analyzes existing DB data.
    """
    from api.workers.cocitation_analyzer import build_cocitation_map

    result = await build_cocitation_map(
        target_brand       = req.target_brand.strip(),
        target_domain      = req.target_domain.strip(),
        fanout_session_ids = req.session_ids,
        db                 = db,
        period_days        = req.period_days,
    )

    record = CocitationMap(
        project_id        = req.project_id,
        target_domain     = req.target_domain,
        sessions_analyzed = req.session_ids,
        map_json          = result,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return {**record.to_dict(), **result}


@router.get("/{project_id}/latest")
async def get_latest_map(project_id: str, db: AsyncSession = Depends(get_db)):
    """Return the most recent co-citation map for a project."""
    row = (await db.execute(
        select(CocitationMap)
        .where(CocitationMap.project_id == project_id)
        .order_by(desc(CocitationMap.generated_at))
        .limit(1)
    )).scalar_one_or_none()
    if not row:
        raise_not_found("Co-citation map")
    return row.to_dict()
