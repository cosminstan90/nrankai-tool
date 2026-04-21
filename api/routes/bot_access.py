"""
Bot Access Audit routes (Prompt 33)
=====================================
POST /api/bot-access/audit
GET  /api/bot-access/{project_id}/latest
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, BotAccessAudit
from api.utils.errors import raise_not_found

logger = logging.getLogger("bot_access")
router = APIRouter(prefix="/api/bot-access", tags=["bot-access"])


class BotAccessRequest(BaseModel):
    target_domain: str
    project_id: Optional[str] = None


@router.post("/audit")
async def run_bot_audit(req: BotAccessRequest, db: AsyncSession = Depends(get_db)):
    """
    Audit AI crawler access for a domain (robots.txt + meta robots).
    Free — no paid API calls. Takes ~2-5 seconds.
    """
    from api.utils.bot_access_auditor import audit

    report = await audit(req.target_domain.strip())

    record = BotAccessAudit(
        project_id    = req.project_id,
        target_domain = req.target_domain,
        report_json   = report,
        access_score  = report.get("access_score"),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return record.to_dict()


@router.get("/domain")
async def audit_domain(target_domain: str, project_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """GET convenience wrapper — audit via query param."""
    from api.utils.bot_access_auditor import audit
    report = await audit(target_domain.strip())
    return report


@router.get("/{project_id}/latest")
async def get_latest_audit(project_id: str, db: AsyncSession = Depends(get_db)):
    """Return the most recent bot access audit for a project."""
    row = (await db.execute(
        select(BotAccessAudit)
        .where(BotAccessAudit.project_id == project_id)
        .order_by(desc(BotAccessAudit.audited_at))
        .limit(1)
    )).scalar_one_or_none()
    if not row:
        raise_not_found("Bot access audit")
    return row.to_dict()
