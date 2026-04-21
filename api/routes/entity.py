"""
Entity Presence Checker routes (Prompt 31)
==========================================
POST /api/entity/check          — run full entity audit (~15 s)
GET  /api/entity/check/{project_id}/latest  — last stored result
"""

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, EntityCheck
from api.utils.errors import raise_not_found, raise_bad_request

logger = logging.getLogger("entity")
router = APIRouter(prefix="/api/entity", tags=["entity"])


class EntityCheckRequest(BaseModel):
    target_domain: str
    target_brand: str
    project_id: Optional[str] = None


@router.post("/check")
async def run_entity_check(req: EntityCheckRequest, db: AsyncSession = Depends(get_db)):
    """
    Run a full entity authority audit for a brand/domain.
    Takes ~10-15 seconds (parallel HTTP checks).
    Requires SERPER_API_KEY for Knowledge Panel, Crunchbase, LinkedIn checks.
    """
    from api.workers.entity_checker import check_entity

    serper_key = os.getenv("SERPER_API_KEY")
    report = await check_entity(
        target_domain=req.target_domain.strip().rstrip("/"),
        target_brand=req.target_brand.strip(),
        serper_api_key=serper_key,
    )

    record = EntityCheck(
        project_id            = req.project_id,
        target_domain         = req.target_domain,
        target_brand          = req.target_brand,
        report_json           = json.dumps(report.to_dict()),
        entity_authority_score = report.entity_authority_score,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return record.to_dict()


@router.get("/check/{project_id}/latest")
async def get_latest_entity_check(project_id: str, db: AsyncSession = Depends(get_db)):
    """Return the most recent entity check for a project."""
    row = (await db.execute(
        select(EntityCheck)
        .where(EntityCheck.project_id == project_id)
        .order_by(desc(EntityCheck.analyzed_at))
        .limit(1)
    )).scalar_one_or_none()

    if not row:
        raise_not_found("Entity check")
    return row.to_dict()
