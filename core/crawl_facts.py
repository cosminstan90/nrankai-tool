"""
Renders per-page link-graph facts for prompts/internal_linking.yaml, following
the same shape core/technical_facts.py uses for TECHNICAL_SEO.

Etapa 5 of docs/IMPROVEMENTS_PLAN.md.

Why per-page rather than site-wide: the prompt analyses one page at a time
("Analyze the provided web page"), and its rubric turns on how many BODY links
point at that page -- something no amount of reading the page's own HTML can
reveal. That is precisely the guess this block replaces with a measurement.
"""

import logging
from typing import Optional

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CrawlLink, CrawlPage, SiteCrawl

logger = logging.getLogger(__name__)


def format_crawl_facts_block(page_facts: Optional[dict]) -> str:
    """Render facts as a labeled block to prepend to the LLM's page content."""
    lines = ["=== CRAWL FACTS (measured by a real site crawl -- do NOT contradict these) ==="]

    if not page_facts:
        # Never render missing data as zero. The prompt caps a page at 20/100
        # when it has no body-content inlinks, so a fabricated zero would
        # invent a failing score for a page nobody measured.
        lines.append(
            "no crawl data available for this page -- judge internal linking from the page "
            "content alone, and do NOT assume anything about how many pages link to it"
        )
        lines.append("=== END CRAWL FACTS -- page content follows below ===")
        return "\n".join(lines)

    content_in = page_facts.get("content_inlinks") or 0
    nav_in = page_facts.get("nav_inlinks") or 0

    if content_in == 0:
        lines.append(
            f"inbound internal links: ZERO body-content links point to this page "
            f"(it is an orphan in content terms); {nav_in} navigation/footer/sidebar links do"
        )
    else:
        lines.append(
            f"inbound internal links: {content_in} from body content, plus {nav_in} "
            f"from navigation/footer/sidebar (counted separately)"
        )

    anchors = page_facts.get("inbound_anchors") or []
    if anchors:
        lines.append(f"anchor text used to link here: {', '.join(repr(a) for a in anchors[:10])}")
    elif content_in > 0:
        lines.append("anchor text used to link here: every inbound content link has empty anchor text")

    depth = page_facts.get("crawl_depth")
    if depth is not None:
        lines.append(f"crawl depth: {depth} click(s) from the home page")

    lines.append(f"outbound links from this page: {page_facts.get('outlinks_total') or 0}")

    broken = page_facts.get("broken_outlinks") or []
    if broken:
        listed = "; ".join(f"{b['dest_url']} ({b['status_code']})" for b in broken[:10])
        lines.append(f"BROKEN outbound links on this page: {listed}")
    else:
        # Stated positively on purpose: silence would let the model assume
        # broken links simply were not checked.
        lines.append("broken outbound links on this page: none found")

    lines.append("=== END CRAWL FACTS -- page content follows below ===")
    return "\n".join(lines)


async def load_crawl_url_map(website: str) -> dict:
    """
    {analyzer filename stem: crawled URL} for the newest completed crawl.

    Built from the crawl's own URLs rather than from the scrape state file.
    The state file was the obvious source, but on real data it covered only
    157 of 545 scraped pages -- the rest predate state tracking -- which would
    have left 71% of pages rendering "no crawl data" while the feature looked
    like it worked. The scraper's URL -> filename rule is deterministic and
    verified to reproduce all 157 known filenames exactly, so applying it to
    the crawl's URLs covers every crawled page instead.

    Returns {} when the site has never been crawled, which is the case where
    no mapping is needed anyway.
    """
    from core.web_scraper import safe_filename_stem

    async with AsyncSessionLocal() as db:
        crawl = (await db.execute(
            select(SiteCrawl)
            .where(SiteCrawl.website == website, SiteCrawl.status == "completed")
            .order_by(SiteCrawl.completed_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if crawl is None:
            return {}

        urls = (await db.execute(
            select(CrawlPage.url).where(CrawlPage.crawl_id == crawl.id)
        )).scalars().all()

    return {safe_filename_stem(url): url for url in urls}


async def load_page_facts(website: str, page_url: str) -> Optional[dict]:
    """
    Facts for one page from the newest COMPLETED crawl of that site.

    Filtered to status="completed" and ordered by completed_at so a stale or
    still-running crawl can never shadow a good one -- a running crawl has
    partial counts, and a partial count of zero would trip the prompt's
    "MAXIMUM 20" cap on a page that is actually well linked.

    Returns None when the site has never been crawled or the page was not in
    the crawl. The caller must render that as "no crawl data", never as zero.
    """
    async with AsyncSessionLocal() as db:
        crawl = (await db.execute(
            select(SiteCrawl)
            .where(SiteCrawl.website == website, SiteCrawl.status == "completed")
            .order_by(SiteCrawl.completed_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if crawl is None:
            return None

        page = (await db.execute(
            select(CrawlPage).where(
                CrawlPage.crawl_id == crawl.id,
                CrawlPage.url == page_url,
            )
        )).scalar_one_or_none()
        if page is None:
            return None

        inbound = (await db.execute(
            select(CrawlLink).where(
                CrawlLink.crawl_id == crawl.id,
                CrawlLink.dest_url == page_url,
                CrawlLink.reason == "content",
            )
        )).scalars().all()

        outbound_broken = (await db.execute(
            select(CrawlLink).where(
                CrawlLink.crawl_id == crawl.id,
                CrawlLink.source_url == page_url,
                CrawlLink.reason == "error",
            )
        )).scalars().all()

    return {
        "url": page.url,
        "content_inlinks": page.content_inlinks,
        "nav_inlinks": page.nav_inlinks,
        "crawl_depth": page.crawl_depth,
        "outlinks_total": page.outlinks_total,
        "is_orphan": page.is_orphan,
        "inbound_anchors": [link.anchor for link in inbound if link.anchor],
        "broken_outlinks": [
            {"dest_url": link.dest_url, "status_code": link.dest_status_code}
            for link in outbound_broken
        ],
    }
