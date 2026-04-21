"""
Multilingual Gap Detector routes (Prompt 36)
=============================================
POST /api/multilingual/detect-gaps
GET  /api/multilingual/{project_id}/latest
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, MultilingualGapReport
from api.utils.errors import raise_not_found

logger = logging.getLogger("multilingual")
router = APIRouter(prefix="/api/multilingual", tags=["multilingual"])


class MultilingualRequest(BaseModel):
    project_id: Optional[str] = None
    target_domain: str
    key_pages: Optional[List[str]] = None
    prompt_languages: List[str] = ["ro", "en"]


@router.post("/detect-gaps")
async def detect_multilingual_gaps(req: MultilingualRequest, db: AsyncSession = Depends(get_db)):
    """
    Check if key pages exist in the languages used by monitored prompts.
    Free — uses httpx only. Takes ~5-15s depending on page count.
    """
    from api.utils.multilingual_gap_detector import detect_gaps

    report = await detect_gaps(
        target_domain    = req.target_domain.strip(),
        key_pages        = req.key_pages or ["/"],
        prompt_languages = req.prompt_languages,
        db               = db,
    )

    record = MultilingualGapReport(
        project_id     = req.project_id,
        target_domain  = req.target_domain,
        report_json    = report.to_dict(),
        coverage_score = report.coverage_score,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return record.to_dict()


@router.get("/{project_id}/latest")
async def get_latest_report(project_id: str, db: AsyncSession = Depends(get_db)):
    """Return the most recent multilingual gap report for a project."""
    row = (await db.execute(
        select(MultilingualGapReport)
        .where(MultilingualGapReport.project_id == project_id)
        .order_by(desc(MultilingualGapReport.analyzed_at))
        .limit(1)
    )).scalar_one_or_none()
    if not row:
        raise_not_found("Multilingual gap report")
    return row.to_dict()
