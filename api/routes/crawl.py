"""Trigger and read site crawls (Etapa 5 of docs/IMPROVEMENTS_PLAN.md)."""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.limiter import limiter
from api.models.database import CrawlLink, CrawlPage, SiteCrawl, get_db
from api.utils.errors import raise_bad_request, raise_not_found
from api.workers.crawl_worker import run_site_crawl
from core.crawl_insights import anchor_distribution, broken_internal_links, depth_histogram
from core.sf_crawler import SfConfigMissing, _config_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crawl", tags=["crawl"])


class StartCrawlRequest(BaseModel):
    website: str = Field(..., min_length=4, max_length=500)

    @field_validator("website")
    @classmethod
    def validate_website(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("website must include the scheme, e.g. https://example.com")
        return v


@router.post("/start")
@limiter.limit("5/hour")
async def start_crawl(request: Request, body: StartCrawlRequest, background: BackgroundTasks):
    """
    Start a crawl in the background. Returns immediately with the crawl id.

    Rate limited to 5/hour deliberately: a crawl is the heaviest thing this
    tool does to someone else's server -- a verified run took over 10 minutes
    -- so accidental repeat triggering should be hard.
    """
    try:
        _config_path()
    except SfConfigMissing as exc:
        # Fail here, where the caller sees it, rather than inside a background
        # task whose exception nobody is watching.
        raise_bad_request(str(exc))

    background.add_task(run_site_crawl, body.website)
    return {"status": "started", "website": body.website}


@router.get("/site/{website:path}/latest")
async def latest_crawl(website: str, db: AsyncSession = Depends(get_db)):
    """Newest completed crawl for a site, with its derived findings."""
    crawl = (await db.execute(
        select(SiteCrawl)
        .where(SiteCrawl.website == website, SiteCrawl.status == "completed")
        .order_by(SiteCrawl.completed_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if crawl is None:
        raise_not_found("Completed crawl", website)

    pages = (await db.execute(
        select(CrawlPage).where(CrawlPage.crawl_id == crawl.id)
    )).scalars().all()
    links = (await db.execute(
        select(CrawlLink).where(CrawlLink.crawl_id == crawl.id)
    )).scalars().all()

    edge_dicts = [link.to_dict() for link in links]
    page_dicts = [page.to_dict() for page in pages]
    orphans = [p for p in page_dicts if p["is_orphan"]]

    return {
        **crawl.to_dict(),
        "orphan_count": len(orphans),
        "orphan_pages": orphans[:100],
        "broken_internal_links": broken_internal_links(edge_dicts),
        "anchor_text": anchor_distribution(edge_dicts),
        "depth_histogram": depth_histogram(page_dicts),
    }


@router.get("/{crawl_id}")
async def get_crawl(crawl_id: str, db: AsyncSession = Depends(get_db)):
    """Status of a single crawl -- poll this after /start."""
    crawl = (await db.execute(
        select(SiteCrawl).where(SiteCrawl.id == crawl_id)
    )).scalar_one_or_none()
    if crawl is None:
        raise_not_found("Crawl", crawl_id)
    return crawl.to_dict()
