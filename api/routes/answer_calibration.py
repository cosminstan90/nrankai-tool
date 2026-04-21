"""
Answer Calibration routes (Prompt 35)
=======================================
POST /api/fanout/sessions/{id}/calibrate
POST /api/fanout/sessions/{id}/calibrate-all-gaps
GET  /api/answer-calibrations/{project_id}
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, AnswerCalibration, FanoutSession, FanoutProject, FanoutCrossRefResult
from api.utils.errors import raise_not_found, raise_bad_request

logger = logging.getLogger("answer_calibration")
router = APIRouter(tags=["answer-calibration"])


class CalibrateRequest(BaseModel):
    project_id: Optional[str] = None
    crossref_id: Optional[str] = None


class CalibrateAllRequest(BaseModel):
    project_id: Optional[str] = None
    max_calibrations: int = 5


@router.post("/api/fanout/sessions/{session_id}/calibrate")
async def calibrate_session(
    session_id: str,
    req: CalibrateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate the ideal AI response for a session's prompt that naturally includes
    the target brand. Uses claude-sonnet (~$0.015).
    """
    from api.utils.answer_calibrator import calibrate

    session = await db.get(FanoutSession, session_id)
    if not session:
        raise_not_found("Fan-out session")

    # Load project for brand info
    target_brand  = ""
    target_domain = session.target_url or ""
    vertical      = "generic"

    if req.project_id:
        proj = await db.get(FanoutProject, req.project_id)
        if proj:
            target_brand  = proj.target_brand
            target_domain = proj.target_domain
            vertical      = proj.vertical or "generic"

    if not target_brand:
        # Fallback: extract from target_url
        from urllib.parse import urlparse
        target_brand = urlparse(target_domain).netloc or target_domain

    # Load crossref result if provided
    crossref_result = None
    if req.crossref_id:
        xref = await db.get(FanoutCrossRefResult, req.crossref_id)
        if xref and xref.result_json:
            crossref_result = xref.result_json

    result = await calibrate(
        prompt               = session.prompt,
        target_brand         = target_brand,
        target_domain        = target_domain,
        vertical             = vertical,
        crossref_result      = crossref_result,
    )

    record = AnswerCalibration(
        session_id       = session_id,
        project_id       = req.project_id,
        prompt           = session.prompt,
        target_brand     = target_brand,
        calibration_json = result.to_dict(),
        brand_position   = result.brand_position,
        estimated_effort = result.estimated_effort,
        cost_usd         = result.cost_usd,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return record.to_dict()


@router.post("/api/fanout/sessions/{session_id}/calibrate-all-gaps")
async def calibrate_all_gaps(
    session_id: str,
    req: CalibrateAllRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Run calibration for up to max_calibrations gap queries from a crossref result.
    Returns cost estimate and results.
    """
    from api.utils.answer_calibrator import calibrate
    from api.models.database import FanoutCrossRefResult
    from sqlalchemy import select as _sel

    session = await db.get(FanoutSession, session_id)
    if not session:
        raise_not_found("Fan-out session")

    target_brand  = ""
    target_domain = session.target_url or ""
    vertical      = "generic"

    if req.project_id:
        proj = await db.get(FanoutProject, req.project_id)
        if proj:
            target_brand  = proj.target_brand
            target_domain = proj.target_domain
            vertical      = proj.vertical or "generic"

    if not target_brand:
        from urllib.parse import urlparse
        target_brand = urlparse(target_domain).netloc or target_domain

    # Find most recent crossref for this session
    xref = (await db.execute(
        _sel(FanoutCrossRefResult)
        .where(FanoutCrossRefResult.fanout_session_id == session_id)
        .order_by(desc(FanoutCrossRefResult.created_at))
        .limit(1)
    )).scalar_one_or_none()

    gap_queries = []
    if xref and xref.result_json:
        gap_queries = [g.get("query", "") for g in xref.result_json.get("gap_queries", [])]

    if not gap_queries:
        raise_bad_request("No gap queries found for this session — run a crossref first")

    # Cap to max_calibrations
    gap_queries = gap_queries[: req.max_calibrations]
    estimated_cost = round(len(gap_queries) * 0.015, 3)

    results = []
    for query in gap_queries:
        try:
            cal = await calibrate(
                prompt        = query,
                target_brand  = target_brand,
                target_domain = target_domain,
                vertical      = vertical,
            )
            record = AnswerCalibration(
                session_id       = session_id,
                project_id       = req.project_id,
                prompt           = query,
                target_brand     = target_brand,
                calibration_json = cal.to_dict(),
                brand_position   = cal.brand_position,
                estimated_effort = cal.estimated_effort,
                cost_usd         = cal.cost_usd,
            )
            db.add(record)
            results.append(cal.to_dict())
        except Exception as exc:
            logger.error("Calibration failed for %r: %s", query, exc)

    await db.commit()
    return {
        "session_id":       session_id,
        "calibrated_count": len(results),
        "estimated_cost":   estimated_cost,
        "calibrations":     results,
    }


@router.get("/api/answer-calibrations/{project_id}")
async def list_calibrations(project_id: str, db: AsyncSession = Depends(get_db)):
    """Return all calibrations for a project."""
    rows = (await db.execute(
        select(AnswerCalibration)
        .where(AnswerCalibration.project_id == project_id)
        .order_by(desc(AnswerCalibration.created_at))
        .limit(100)
    )).scalars().all()
    return [r.to_dict() for r in rows]
