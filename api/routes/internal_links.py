"""
GET /api/internal-links/suggestions -- Pasul 14 of docs/superpowers/plans/2026-09-30-next-steps.md.

Ties the link graph (crawl_pages/crawl_links, Etapa 5) to real content
similarity (core/embeddings.py) and, optionally, real GSC query data for the
proposed anchor text. Backend only this session, matching the Pasul 9/10/12/13
split -- a UI table is separate follow-up work.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query
from sqlalchemy import select

from api.models.database import (
    AsyncSessionLocal, CrawlLink, CrawlPage, GscProperty, PageSnapshot, SiteCrawl,
)
from api.routes.gsc._shared import _get_gsc_credentials
from api.utils.errors import raise_not_found
from api.utils.url_validator import sanitize_website_for_path
from core.embeddings import embed_text
from core.internal_links import build_suggestions, find_link_targets
from core.url_normalize import normalize_url

router = APIRouter(prefix="/api/internal-links", tags=["internal-links"])

# Real, paid OpenAI calls (cache-aware after the first run per page) -- a cap
# keeps one request from silently embedding an entire large site.
DEFAULT_MAX_PAGES_TO_EMBED = 50
MIN_WORDS_TO_EMBED = 20   # a title-only or empty page has nothing meaningful to compare
ANCHOR_WINDOW_DAYS = 90


def _page_text_from_snapshot(snapshot: PageSnapshot) -> str:
    parts = [snapshot.title or ""]
    parts.extend(snapshot.h1 or [])
    parts.extend(snapshot.h2 or [])
    parts.extend(snapshot.h3 or [])
    return "\n".join(p for p in parts if p)


def _page_text_from_html_file(website: str, url: str) -> Optional[str]:
    """
    Falls back to the scraper's own stored HTML (Etapa 6's input_html
    directory) when no page_snapshots row exists for this URL -- a crawl
    imported via Screaming Frog (Etapa 5) has no text of its own at all,
    only link-graph metadata.
    """
    from core.html2llm_converter import extract_content
    from core.web_scraper import safe_filename_stem

    try:
        website_dir = sanitize_website_for_path(website)
    except ValueError:
        return None
    html_path = os.path.join(website_dir, "input_html", safe_filename_stem(url) + ".html")
    if not os.path.exists(html_path):
        return None
    try:
        with open(html_path, "r", encoding="utf-8", errors="replace") as fh:
            return extract_content(fh.read())
    except OSError:
        return None


async def _fetch_queries_for_page(creds, site_url: str, page_url: str, days: int) -> list:
    """Live GSC fetch, one page at a time -- same dimensionFilterGroups pattern
    as api/routes/gsc/oauth_sync.py's get_page_queries."""
    from googleapiclient.discovery import build

    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days)

    def _fetch():
        svc = build("searchconsole", "v1", credentials=creds)
        body = {
            "startDate": start_date.isoformat(), "endDate": end_date.isoformat(),
            "dimensions": ["query"],
            "dimensionFilterGroups": [{"filters": [
                {"dimension": "page", "expression": page_url, "operator": "equals"},
            ]}],
            "rowLimit": 100,
        }
        return svc.searchanalytics().query(siteUrl=site_url, body=body).execute().get("rows", [])

    rows = await asyncio.get_event_loop().run_in_executor(None, _fetch)
    return [{"query": r["keys"][0], "impressions": int(r.get("impressions", 0))}
           for r in rows if r.get("keys")]


@router.get("/suggestions")
async def get_internal_link_suggestions(
    crawl_id: str,
    property_id: Optional[str] = Query(None, description="A connected GSC property, for real-query anchors"),
    max_pages_to_embed: int = Query(DEFAULT_MAX_PAGES_TO_EMBED, ge=1, le=500),
):
    """
    Internal link suggestions for one Screaming Frog crawl: targets
    (orphaned or under-linked indexable pages), each with topically-close
    candidate sources that don't already link to it, and a real-GSC-query
    anchor when `property_id` is given and connected. Pages with no
    captured text anywhere (no page_snapshots row, no stored scraped HTML)
    can't be matched -- excluded, never scored as unrelated.
    """
    async with AsyncSessionLocal() as db:
        crawl = await db.get(SiteCrawl, crawl_id)
        if not crawl:
            raise_not_found("Crawl", crawl_id)

        crawl_pages = (await db.execute(
            select(CrawlPage).where(CrawlPage.crawl_id == crawl_id)
        )).scalars().all()
        crawl_links = (await db.execute(
            select(CrawlLink).where(CrawlLink.crawl_id == crawl_id)
        )).scalars().all()
        snapshot_rows = (await db.execute(
            select(PageSnapshot).where(PageSnapshot.website == crawl.website)
        )).scalars().all()

        gsc_property = await db.get(GscProperty, property_id) if property_id else None

    snapshot_by_normalized = {}
    for s in snapshot_rows:
        snapshot_by_normalized.setdefault(normalize_url(s.url), s)

    pages = [{
        "url": p.url, "content_inlinks": p.content_inlinks, "is_orphan": p.is_orphan,
        "indexability": p.indexability, "status_code": p.status_code,
    } for p in crawl_pages]
    existing_links = {(l.source_url, l.dest_url) for l in crawl_links}

    targets = find_link_targets(pages)
    if not targets:
        return {"crawl_id": crawl_id, "website": crawl.website,
               "targets_considered": 0, "pages_embedded": 0, "suggestions": []}

    # Embed every page in the crawl once (targets and candidate sources both
    # need a vector), capped -- each miss is a real paid call, cache-aware
    # for every call after the first for that exact text.
    vectors = {}
    embedded_count = 0
    for p in pages:
        if embedded_count >= max_pages_to_embed:
            break
        snapshot = snapshot_by_normalized.get(normalize_url(p["url"]))
        text = _page_text_from_snapshot(snapshot) if snapshot else None
        if not text:
            text = _page_text_from_html_file(crawl.website, p["url"])
        if not text or len(text.split()) < MIN_WORDS_TO_EMBED:
            continue
        vectors[p["url"]] = await embed_text(text, source="internal_links")
        embedded_count += 1

    queries_by_url = {}
    if gsc_property:
        creds = await _get_gsc_credentials()
        if creds:
            for target in targets:
                queries_by_url[target["url"]] = await _fetch_queries_for_page(
                    creds, gsc_property.site_url, target["url"], ANCHOR_WINDOW_DAYS)

    suggestions = build_suggestions(targets, pages, vectors, existing_links, queries_by_url)

    return {
        "crawl_id": crawl_id, "website": crawl.website,
        "targets_considered": len(targets), "pages_embedded": len(vectors),
        "anchors_from_gsc": bool(queries_by_url),
        "suggestions": suggestions,
    }
