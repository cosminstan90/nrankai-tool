"""
ContentIQ Sitemap Crawler + Page Meta Extractor (Prompt 02)
============================================================
crawl_sitemap()   — parse sitemap XML (including sitemap index)
extract_page_meta() — fetch page and extract title/word_count/last_modified/etc.
crawl_audit()     — orchestrate full crawl for an audit, update DB
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

logger = logging.getLogger("contentiq.crawler")

_USER_AGENT = "nrankai-contentiq/1.0"
_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


async def crawl_sitemap(sitemap_url: str, max_urls: int = 2000) -> list[str]:
    """
    Fetch and parse a sitemap (or sitemap index) and return deduplicated URL list.
    """
    import httpx
    from lxml import etree

    visited: set[str] = set()
    urls: list[str]   = []

    async def _process(url: str):
        if url in visited or len(urls) >= max_urls:
            return
        visited.add(url)
        try:
            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
                content = r.content
                # Handle gzip
                if url.endswith(".gz") or r.headers.get("content-encoding") == "gzip":
                    import gzip
                    content = gzip.decompress(content)
        except Exception as exc:
            logger.warning("Sitemap fetch failed for %s: %s", url, exc)
            return

        try:
            root = etree.fromstring(content, parser=etree.XMLParser(recover=True))
        except Exception as exc:
            logger.warning("Sitemap parse failed for %s: %s", url, exc)
            return

        # Determine namespace
        tag = root.tag
        ns  = _SITEMAP_NS if _SITEMAP_NS in tag else ""
        pfx = f"{{{ns}}}" if ns else ""

        if "sitemapindex" in tag:
            # Sitemap index — recurse into children
            for sitemap_el in root.findall(f"{pfx}sitemap"):
                loc_el = sitemap_el.find(f"{pfx}loc")
                if loc_el is not None and loc_el.text:
                    await _process(loc_el.text.strip())
        else:
            # Regular sitemap
            for url_el in root.findall(f"{pfx}url"):
                loc_el = url_el.find(f"{pfx}loc")
                if loc_el is not None and loc_el.text:
                    u = loc_el.text.strip()
                    if u not in set(urls):
                        urls.append(u)
                        if len(urls) >= max_urls:
                            return

    await _process(sitemap_url)
    return urls


async def extract_page_meta(url: str, client) -> dict:
    """
    Fetch a URL and extract metadata: title, word_count, last_modified, canonical, etc.
    """
    base: dict = {"url": url, "status_code": 0}
    try:
        from bs4 import BeautifulSoup
        from dateutil import parser as date_parser

        r = await client.get(url)
        base["status_code"] = r.status_code
        if r.status_code >= 400:
            return base

        soup = BeautifulSoup(r.text, "lxml")

        # Title
        title_tag = soup.find("title")
        base["title"] = title_tag.get_text(strip=True) if title_tag else None

        # H1
        h1_tag = soup.find("h1")
        base["h1"] = h1_tag.get_text(strip=True) if h1_tag else None

        # Meta description
        md = soup.find("meta", attrs={"name": re.compile(r"description", re.I)})
        base["meta_description"] = md.get("content", "").strip() if md else None

        # Canonical
        canon = soup.find("link", rel="canonical")
        base["canonical"] = canon.get("href", "").strip() if canon else None

        # Word count — strip scripts/styles, prefer <main>/<article>/<body>
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        content_el = soup.find("main") or soup.find("article") or soup.find("body")
        text = content_el.get_text(" ", strip=True) if content_el else ""
        base["word_count"] = len(text.split())

        # Last modified
        lm = None
        # 1. Meta tags
        for attr in [("name", "last-modified"), ("property", "article:modified_time")]:
            m = soup.find("meta", attrs={attr[0]: attr[1]})
            if m and m.get("content"):
                lm = m.get("content")
                break
        # 2. <time datetime="...">
        if not lm:
            time_el = soup.find("time", datetime=True)
            if time_el:
                lm = time_el.get("datetime")
        # 3. HTTP header
        if not lm:
            lm = r.headers.get("last-modified")
        # Parse to ISO date
        if lm:
            try:
                base["last_modified"] = date_parser.parse(lm).date().isoformat()
            except Exception:
                base["last_modified"] = None
        else:
            base["last_modified"] = None

    except Exception as exc:
        base["error"] = str(exc)
        logger.debug("extract_page_meta error for %s: %s", url, exc)

    return base


async def crawl_audit(
    audit_id: int,
    sitemap_url: str,
    db,
    max_urls: int = 2000,
    concurrency: int = 5,
) -> None:
    """
    Full crawl for a CiqAudit: parse sitemap, fetch each page, upsert CiqPage rows.
    Updates audit status from pending -> crawling -> scoring.
    """
    import httpx
    from datetime import datetime, timezone
    from sqlalchemy import select, update
    from api.models.contentiq import CiqAudit, CiqPage

    # Get URL list
    try:
        urls = await crawl_sitemap(sitemap_url, max_urls)
    except Exception as exc:
        logger.error("crawl_audit: sitemap failed for audit %d: %s", audit_id, exc)
        await db.execute(
            update(CiqAudit).where(CiqAudit.id == audit_id).values(status="failed")
        )
        await db.commit()
        return

    total = len(urls)
    logger.info("[Crawler] Found %d URLs for audit %d", total, audit_id)

    await db.execute(
        update(CiqAudit).where(CiqAudit.id == audit_id).values(
            status="crawling", total_urls=total
        )
    )
    await db.commit()

    sem     = asyncio.Semaphore(concurrency)
    crawled = 0

    async with httpx.AsyncClient(
        timeout=15,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:

        async def _fetch_and_save(url: str):
            nonlocal crawled
            async with sem:
                meta = await extract_page_meta(url, client)

            # Upsert CiqPage
            existing = (await db.execute(
                select(CiqPage).where(CiqPage.audit_id == audit_id, CiqPage.url == url)
            )).scalar_one_or_none()

            now = datetime.now(timezone.utc)
            if existing:
                for k, v in meta.items():
                    if k not in ("url",) and hasattr(existing, k):
                        setattr(existing, k, v)
                existing.crawled_at = now
            else:
                db.add(CiqPage(
                    audit_id        = audit_id,
                    url             = url,
                    title           = meta.get("title"),
                    h1              = meta.get("h1"),
                    meta_description= meta.get("meta_description"),
                    canonical       = meta.get("canonical"),
                    word_count      = meta.get("word_count"),
                    last_modified   = meta.get("last_modified"),
                    status_code     = meta.get("status_code"),
                    crawled_at      = now,
                ))

            crawled += 1
            if crawled % 25 == 0:
                logger.info("[Crawler] %d/%d pages crawled for audit %d", crawled, total, audit_id)
                await db.commit()

        await asyncio.gather(*[_fetch_and_save(u) for u in urls])
        await db.commit()

    await db.execute(
        update(CiqAudit).where(CiqAudit.id == audit_id).values(status="scoring")
    )
    await db.commit()
    logger.info("[Crawler] Done. %d pages crawled for audit %d", crawled, audit_id)
