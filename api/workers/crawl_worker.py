"""
Runs a Screaming Frog crawl and persists the filtered link graph.

Etapa 5 of docs/IMPROVEMENTS_PLAN.md.

Deliberately NOT a step in the 4-step audit pipeline in audit_worker.py. A
verified crawl of a ~100-route app with zero network latency took over 10
minutes; making every audit wait on one would be a large regression and would
re-crawl a client's site on every run. Crawls are triggered explicitly per
site, and audits read the newest completed one if there is one.
"""

import asyncio
import logging
import tempfile
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CrawlLink, CrawlPage, SiteCrawl
from core.sf_crawler import CrawlArtifacts, run_crawl
from core.sf_parser import parse_inlinks, parse_internal_html

logger = logging.getLogger(__name__)


async def persist_crawl(crawl_id: str, website: str, artifacts: CrawlArtifacts) -> None:
    """Parse the exports and write one SiteCrawl plus its pages and edges."""
    pages = parse_internal_html(artifacts.internal_html_csv)
    edges, counts = parse_inlinks(artifacts.inlinks_csv)

    nav_discarded = sum(c["non_content"] for c in counts.values())

    async with AsyncSessionLocal() as db:
        crawl = (await db.execute(
            select(SiteCrawl).where(SiteCrawl.id == crawl_id)
        )).scalar_one_or_none()
        if crawl is None:
            crawl = SiteCrawl(
                id=crawl_id, website=website,
                started_at=datetime.now(timezone.utc),
            )
            db.add(crawl)

        for page in pages:
            page_counts = counts.get(page["url"], {"content": 0, "non_content": 0})
            db.add(CrawlPage(
                id=str(uuid.uuid4()),
                crawl_id=crawl_id,
                url=page["url"],
                status_code=page["status_code"],
                indexability=page["indexability"],
                crawl_depth=page["crawl_depth"],
                inlinks_total=page["inlinks_total"],
                unique_inlinks=page["unique_inlinks"],
                outlinks_total=page["outlinks_total"],
                unique_outlinks=page["unique_outlinks"],
                content_inlinks=page_counts["content"],
                nav_inlinks=page_counts["non_content"],
                # "Orphan" here means reachable by the crawler but with zero
                # body-content links pointing at it -- the crawl-vulnerable
                # case prompts/internal_linking.yaml scores on. Depth 0 is
                # exempt: nothing links to a home page from body content on
                # most sites, so flagging it would put a false finding at the
                # top of every report.
                is_orphan=(page_counts["content"] == 0 and (page["crawl_depth"] or 0) > 0),
            ))

        for edge in edges:
            db.add(CrawlLink(
                id=str(uuid.uuid4()),
                crawl_id=crawl_id,
                source_url=edge["source_url"],
                dest_url=edge["dest_url"],
                anchor=edge["anchor"],
                link_position=edge["link_position"],
                follow=edge["follow"],
                dest_status_code=edge["dest_status_code"],
                reason=edge["reason"],
            ))

        crawl.status = "completed"
        crawl.pages_crawled = len(pages)
        crawl.content_edges = sum(1 for e in edges if e["reason"] == "content")
        crawl.nav_edges_discarded = nav_discarded
        crawl.completed_at = datetime.now(timezone.utc)
        await db.commit()

    logger.info(
        "Crawl %s persisted: %d pages, %d stored edges, %d nav edges discarded",
        crawl_id, len(pages), len(edges), nav_discarded,
    )


async def run_site_crawl(website: str) -> str:
    """
    Full run: crawl, parse, persist. Returns the crawl id.

    The SiteCrawl row is created up front with status="running" so a crawl that
    dies mid-way is visible as a failure rather than simply never appearing.
    """
    crawl_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        db.add(SiteCrawl(
            id=crawl_id, website=website, status="running",
            started_at=datetime.now(timezone.utc),
        ))
        await db.commit()

    try:
        with tempfile.TemporaryDirectory(prefix=f"sfcrawl_{crawl_id[:8]}_") as tmp:
            # Off the event loop, deliberately. run_crawl uses subprocess.run,
            # which blocks, and FastAPI runs async BackgroundTasks on the loop
            # -- calling it directly froze the entire API for the length of the
            # crawl (measured: 1 heartbeat tick in 600ms, and crawls run for
            # 10+ minutes). Every request, /api/health included, would hang.
            artifacts = await asyncio.to_thread(run_crawl, website, tmp)
            await persist_crawl(crawl_id, website, artifacts)
    except Exception as exc:
        logger.error("Crawl %s of %s failed: %s", crawl_id, website, exc)
        async with AsyncSessionLocal() as db:
            crawl = (await db.execute(
                select(SiteCrawl).where(SiteCrawl.id == crawl_id)
            )).scalar_one_or_none()
            if crawl:
                crawl.status = "failed"
                crawl.error = str(exc)[:2000]
                crawl.completed_at = datetime.now(timezone.utc)
                await db.commit()
        raise

    return crawl_id
