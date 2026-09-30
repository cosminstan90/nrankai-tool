"""
Fetch and cache citing pages for comparison -- Pasul 16 of
docs/superpowers/plans/2026-09-30-next-steps.md.

Downloads a page actually cited (by an AI Overview or an LLM) so
core/citation_features.py can measure it, respecting robots.txt and caching
on disk so the same URL is never fetched twice.

Missing/unreadable robots.txt defaults to ALLOWED, matching Python's own
urllib.robotparser default (an empty rule set permits everything) and the
standard web convention: no robots.txt means no stated restrictions, not
"assume blocked".
"""
import hashlib
import logging
import os
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)

USER_AGENT = "nrankai-tool/1.0 (+https://app.nrankai.com; citation-comparison research)"
# api/data/, not a project-root data/ -- the same directory api/models/_base.py's
# DATABASE_DIR uses, already gitignored (.gitignore:79) and the project's one
# established place for local, ungit-tracked runtime data.
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api", "data", "citation_cache")
FETCH_TIMEOUT_S = 15


def cache_path(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{digest}.html")


def _robots_allows(url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
    except Exception as exc:
        logger.warning("Could not read robots.txt at %s: %s -- treating as allowed", robots_url, exc)
        return True
    return parser.can_fetch(USER_AGENT, url)


async def fetch_and_cache(url: str) -> Optional[str]:
    """
    Cached HTML for `url`, fetching it (respecting robots.txt) if this is
    the first time. None when robots.txt disallows it or the fetch fails --
    never raises, since a single uncooperative citing page must not fail
    the whole comparison.
    """
    import asyncio

    path = cache_path(url)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    allowed = await asyncio.to_thread(_robots_allows, url)
    if not allowed:
        logger.info("robots.txt disallows fetching %s -- skipped", url)
        return None

    import httpx

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Could not fetch %s: %s", url, exc)
        return None

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(resp.text)
    return resp.text
