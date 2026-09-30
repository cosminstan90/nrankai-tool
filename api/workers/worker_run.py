"""
Shared recorder for WorkerRun rows -- Pasul 7 of
docs/superpowers/plans/2026-09-30-next-steps.md.

Every background worker calls record_worker_run() exactly once per meaningful
unit of work (one audit, one archive pass, one snapshot capture, one lead job
-- never once per poll tick, which would be thousands of rows a day for no
benefit). GET /api/status reads the latest row per worker to answer "when did
this last succeed".

Opens its own short session, per api/models/_base.py's rule: never called
from inside a worker's own long-lived session, and the write here is never
held open across anything slow -- it's always the very last thing a run does.
"""
import logging
from datetime import datetime, timezone

from api.models._base import AsyncSessionLocal
from api.models.database import WorkerRun

logger = logging.getLogger(__name__)


async def record_worker_run(worker: str, started_at: datetime, ok: bool, detail: str = None) -> None:
    """Record one completed run. Never raises -- a failure to record must not fail the worker."""
    try:
        async with AsyncSessionLocal() as db:
            db.add(WorkerRun(
                worker=worker,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
                ok=ok,
                detail=(detail or "")[:2000] or None,
            ))
            await db.commit()
    except Exception:
        logger.exception("Failed to record worker_runs row for worker=%s", worker)
