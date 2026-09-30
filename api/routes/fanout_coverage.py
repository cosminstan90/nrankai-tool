"""
GET /api/fanout/sessions/{session_id}/coverage-gaps -- Pasul 17 of
docs/superpowers/plans/2026-09-30-next-steps.md.

Distinct from the existing GET /api/fanout/sessions/{id}/coverage
(api/routes/fanout.py): that reports what % of SOURCES are the target
domain. This reports which of Fan-Out's own generated SUB-QUERIES have no
good answer anywhere on the site at all -- a different question the
existing endpoint never answered. Backend only this session, matching the
Pasul 9/10/12/13/14/15/16 split.
"""
import os
from typing import List, Optional

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from api.models.database import AsyncSessionLocal, FanoutQuery, FanoutSession
from api.utils.errors import raise_not_found
from api.utils.url_validator import sanitize_website_for_path
from api.workers.prompt_discovery import classify_prompt_cluster
from core.embeddings import embed_text
from core.fanout_coverage import build_coverage_report
from core.passages import chunk_html_into_passages

router = APIRouter(prefix="/api/fanout", tags=["fanout"])

# Real, paid OpenAI calls (cache-aware after the first run per exact text) --
# a cap keeps one request from silently embedding an entire large site.
DEFAULT_MAX_PAGES_TO_EMBED = 20
DEFAULT_MAX_PASSAGES_TO_EMBED = 100


async def _load_site_passages(website: str, max_pages: int, max_passages: int) -> List[dict]:
    """
    Chunks every stored, already-scraped HTML page for `website` into
    passages and embeds each once (cache-aware). Reads from the scraper's
    own input_html directory -- the same convention
    api/routes/internal_links.py's _page_text_from_html_file uses -- since
    Fan-Out sessions aren't tied to a specific crawl or audit, just a
    target_url.
    """
    try:
        website_dir = sanitize_website_for_path(website)
    except ValueError:
        return []
    html_dir = os.path.join(website_dir, "input_html")
    if not os.path.isdir(html_dir):
        return []

    passages: List[dict] = []
    files = sorted(f for f in os.listdir(html_dir) if f.endswith(".html"))[:max_pages]
    for filename in files:
        if len(passages) >= max_passages:
            break
        path = os.path.join(html_dir, filename)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                html = fh.read()
        except OSError:
            continue
        for text in chunk_html_into_passages(html):
            if len(passages) >= max_passages:
                break
            vector = await embed_text(text, source="fanout_coverage")
            passages.append({"url": filename, "text": text, "vector": vector})

    return passages


@router.get("/sessions/{session_id}/coverage-gaps")
async def get_coverage_gaps(
    session_id: str,
    max_pages_to_embed: int = Query(DEFAULT_MAX_PAGES_TO_EMBED, ge=1, le=200),
):
    """
    Which of this session's own generated sub-queries have no good answer
    anywhere on the target site. Requires the session to have a target_url
    and that site to have stored, scraped HTML (core/web_scraper.py) --
    without either, reports why rather than a confident-looking empty result.
    """
    async with AsyncSessionLocal() as db:
        session = (await db.execute(
            select(FanoutSession).where(FanoutSession.id == session_id)
            .options(selectinload(FanoutSession.queries))
        )).scalar_one_or_none()
        if not session:
            raise_not_found("Fanout session", session_id)

        if not session.target_url:
            return {"session_id": session_id, "note": "session has no target_url set",
                   "queries_checked": 0, "report": None}

        sub_query_texts = [q.query_text for q in (session.queries or [])]

    if not sub_query_texts:
        return {"session_id": session_id, "target_url": session.target_url,
               "note": "session has no sub-queries recorded", "queries_checked": 0, "report": None}

    passages = await _load_site_passages(session.target_url, max_pages_to_embed, DEFAULT_MAX_PASSAGES_TO_EMBED)
    if not passages:
        return {"session_id": session_id, "target_url": session.target_url,
               "note": "no stored scraped HTML found for this site -- run an audit with a sitemap first",
               "queries_checked": len(sub_query_texts), "report": None}

    sub_queries = []
    for text in sub_query_texts:
        vector = await embed_text(text, source="fanout_coverage")
        sub_queries.append({"query": text, "vector": vector, "cluster": classify_prompt_cluster(text)})

    report = build_coverage_report(sub_queries, passages)

    return {
        "session_id": session_id, "target_url": session.target_url,
        "queries_checked": len(sub_queries), "pages_embedded": len({p["url"] for p in passages}),
        "passages_embedded": len(passages), "report": report,
    }
