"""
Content snapshots and diffs (Etapa 6 of docs/IMPROVEMENTS_PLAN.md).

Deliberately not folded into api/routes/compare.py, which the plan told us to
check first: that endpoint compares audit SCORES (distributions, per-page score
deltas, criterion analysis) and never looks at page content. This answers a
different question -- "the client edited the page, what did that break?" -- so
it is a new surface rather than a duplicate.
"""

import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.limiter import limiter
from api.models.database import PageSnapshot, SnapshotRun, get_db
from api.utils.errors import raise_bad_request, raise_not_found
from api.workers.snapshot_worker import capture_snapshot, compare_runs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])


class CaptureRequest(BaseModel):
    website: str = Field(..., min_length=3, max_length=500)
    source_dir: str = Field(None, max_length=1000)

    @field_validator("website")
    @classmethod
    def validate_website(cls, v: str) -> str:
        return v.strip()


def _default_source_dir(website: str) -> str:
    """Where the scrape step writes a site's HTML (see audit_worker)."""
    from api.utils.url_validator import sanitize_website_for_path
    return os.path.join(sanitize_website_for_path(website), "input_html")


@router.post("/capture")
@limiter.limit("20/hour")
async def start_capture(request: Request, body: CaptureRequest, background: BackgroundTasks):
    """
    Snapshot a site's currently stored HTML.

    Reads what the scraper already wrote, so this costs no requests against the
    client's site. Take one before handing a site over and one after, and the
    diff answers what changed in between.
    """
    source_dir = body.source_dir or _default_source_dir(body.website)
    if not os.path.isdir(source_dir):
        raise_bad_request(
            f"No stored HTML at '{source_dir}'. Run an audit for {body.website} first, "
            "or pass source_dir explicitly."
        )

    background.add_task(capture_snapshot, body.website, source_dir)
    return {"status": "started", "website": body.website, "source_dir": source_dir}


@router.get("/site/{website:path}/runs")
async def list_runs(website: str, db: AsyncSession = Depends(get_db)):
    """Snapshot runs for a site, newest first -- the input to a diff."""
    runs = (await db.execute(
        select(SnapshotRun)
        .where(SnapshotRun.website == website)
        .order_by(SnapshotRun.created_at.desc())
        .limit(50)
    )).scalars().all()
    return {"website": website, "runs": [r.to_dict() for r in runs]}


@router.get("/diff")
async def diff_runs(before: str, after: str, db: AsyncSession = Depends(get_db)):
    """
    What changed between two snapshot runs.

    Changed pages come back worst-hit first; unchanged ones are counted rather
    than listed, because on a 545-page site listing them buries the findings.
    """
    for run_id in (before, after):
        exists = (await db.execute(
            select(SnapshotRun.id).where(SnapshotRun.id == run_id)
        )).scalar_one_or_none()
        if exists is None:
            raise_not_found("Snapshot run", run_id)

    if before == after:
        raise_bad_request("before and after are the same run -- the diff would always be empty")

    return await compare_runs(before, after)


@router.get("/run/{run_id}/page")
async def get_page_snapshot(run_id: str, url: str, db: AsyncSession = Depends(get_db)):
    """One page's stored snapshot, for inspecting what was actually captured."""
    page = (await db.execute(
        select(PageSnapshot).where(
            PageSnapshot.run_id == run_id,
            PageSnapshot.url == url,
        )
    )).scalar_one_or_none()
    if page is None:
        raise_not_found("Page snapshot", url)
    return page.to_dict()
