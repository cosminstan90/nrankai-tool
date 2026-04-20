"""
Mention Seeding Tracker routes (Prompt 32)
==========================================
POST /api/mention-seeding/configs
GET  /api/mention-seeding/configs
POST /api/mention-seeding/configs/{id}/run
GET  /api/mention-seeding/configs/{id}/latest
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, MentionSeedingConfig, MentionSeedingResult
from api.utils.errors import raise_not_found, raise_bad_request

logger = logging.getLogger("mention_seeding")
router = APIRouter(prefix="/api/mention-seeding", tags=["mention-seeding"])


class MentionSeedingConfigCreate(BaseModel):
    project_id: Optional[str] = None
    target_brand: str
    target_domain: str
    vertical: str = "generic"
    monitor_reddit: bool = True
    monitor_quora: bool = True
    monitor_review_sites: bool = True
    monitor_press: bool = True
    keywords: Optional[List[str]] = None
    schedule: str = "weekly"
    is_active: bool = True


@router.post("/configs")
async def create_config(req: MentionSeedingConfigCreate, db: AsyncSession = Depends(get_db)):
    """Create a new mention seeding monitoring config."""
    import json
    cfg = MentionSeedingConfig(
        project_id           = req.project_id,
        target_brand         = req.target_brand.strip(),
        target_domain        = req.target_domain.strip().rstrip("/"),
        vertical             = req.vertical,
        monitor_reddit       = req.monitor_reddit,
        monitor_quora        = req.monitor_quora,
        monitor_review_sites = req.monitor_review_sites,
        monitor_press        = req.monitor_press,
        keywords             = req.keywords or [],
        schedule             = req.schedule,
        is_active            = req.is_active,
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return cfg.to_dict()


@router.get("/configs")
async def list_configs(
    project_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List mention seeding configs."""
    stmt = select(MentionSeedingConfig).order_by(desc(MentionSeedingConfig.id))
    if project_id:
        stmt = stmt.where(MentionSeedingConfig.project_id == project_id)
    configs = (await db.execute(stmt)).scalars().all()
    return [c.to_dict() for c in configs]


@router.post("/configs/{config_id}/run")
async def run_scan(
    config_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Trigger a mention scan for a config (runs in background)."""
    cfg = (await db.execute(
        select(MentionSeedingConfig).where(MentionSeedingConfig.id == config_id)
    )).scalar_one_or_none()
    if not cfg:
        raise_not_found("Mention seeding config")

    async def _run():
        from api.models._base import AsyncSessionLocal
        from api.workers.mention_seeder_worker import run_mention_scan
        async with AsyncSessionLocal() as _db:
            try:
                report = await run_mention_scan(config_id, _db)
                logger.info("Mention scan complete for config %d: %d mentions", config_id, report.total_mentions)
            except Exception as exc:
                logger.error("Mention scan failed for config %d: %s", config_id, exc)

    background_tasks.add_task(_run)
    return {"ok": True, "config_id": config_id, "message": "Scan started in background"}


@router.get("/configs/{config_id}/latest")
async def get_latest_scan(config_id: int, db: AsyncSession = Depends(get_db)):
    """Return aggregated results from the latest scan run."""
    cfg = (await db.execute(
        select(MentionSeedingConfig).where(MentionSeedingConfig.id == config_id)
    )).scalar_one_or_none()
    if not cfg:
        raise_not_found("Mention seeding config")

    # Get the most recent run date
    latest_date_row = (await db.execute(
        select(MentionSeedingResult.run_date)
        .where(MentionSeedingResult.config_id == config_id)
        .order_by(desc(MentionSeedingResult.run_date))
        .limit(1)
    )).scalar_one_or_none()

    if not latest_date_row:
        return {"config_id": config_id, "run_date": None, "mentions": [], "total": 0}

    results = (await db.execute(
        select(MentionSeedingResult)
        .where(
            MentionSeedingResult.config_id == config_id,
            MentionSeedingResult.run_date == latest_date_row,
        )
        .order_by(desc(MentionSeedingResult.discovered_at))
    )).scalars().all()

    # Aggregate by platform
    by_platform: dict = {}
    for r in results:
        by_platform[r.platform] = by_platform.get(r.platform, 0) + 1

    sentiment_breakdown = {"positive": 0, "neutral": 0, "negative": 0}
    for r in results:
        s = r.sentiment or "neutral"
        sentiment_breakdown[s] = sentiment_breakdown.get(s, 0) + 1

    new_count = sum(1 for r in results if r.is_new)
    covered   = sum(1 for cnt in by_platform.values() if cnt > 0)
    total_plat = max(len(by_platform), 1)
    coverage  = round(covered / total_plat * 100, 1)

    return {
        "config_id":          config_id,
        "run_date":           latest_date_row,
        "total_mentions":     len(results),
        "new_this_run":       new_count,
        "by_platform":        by_platform,
        "sentiment_breakdown": sentiment_breakdown,
        "coverage_score":     coverage,
        "mentions":           [r.to_dict() for r in results[:50]],  # cap at 50
    }
