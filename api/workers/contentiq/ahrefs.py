"""
ContentIQ Ahrefs v3/v4 API client (Prompt 03)
===============================================
AhrefsClient — get_url_metrics, get_top_pages, get_keywords_for_url, batch_url_metrics
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, List

logger = logging.getLogger("contentiq.ahrefs")

_BASE_URL = "https://api.ahrefs.com/v4"
_EMPTY_METRICS = {"traffic": 0, "keywords": 0, "backlinks": 0, "dr": 0}


class RateLimitError(Exception):
    pass


class AhrefsClient:
    def __init__(self, api_key: str):
        self._api_key = api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def get_url_metrics(self, url: str) -> dict:
        """Fetch organic traffic, keywords, backlinks, DR for a single URL."""
        import httpx
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30, base_url=_BASE_URL) as client:
                    r = await client.get(
                        "/site-explorer/metrics",
                        headers=self._headers(),
                        params={
                            "select": "org_traffic,org_keywords,backlinks,domain_rating",
                            "target": url,
                            "mode":   "exact",
                        },
                    )
                    if r.status_code == 429:
                        raise RateLimitError("Rate limited by Ahrefs")
                    r.raise_for_status()
                    data = r.json()
                    m = data.get("metrics", data)
                    return {
                        "traffic":  int(m.get("org_traffic",    m.get("traffic",  0)) or 0),
                        "keywords": int(m.get("org_keywords",   m.get("keywords", 0)) or 0),
                        "backlinks":int(m.get("backlinks",      0) or 0),
                        "dr":       int(m.get("domain_rating",  m.get("dr", 0))   or 0),
                    }
            except RateLimitError:
                logger.warning("Ahrefs rate limit — sleeping 10s (attempt %d/3)", attempt + 1)
                await asyncio.sleep(10)
            except Exception as exc:
                logger.warning("Ahrefs get_url_metrics error for %s: %s", url, exc)
                return dict(_EMPTY_METRICS)
        return dict(_EMPTY_METRICS)

    async def get_top_pages(self, domain: str, limit: int = 200) -> List[dict]:
        """Return top pages by traffic for a domain."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, base_url=_BASE_URL) as client:
                r = await client.get(
                    "/site-explorer/top-pages-by-traffic",
                    headers=self._headers(),
                    params={
                        "select": "url,traffic,keywords",
                        "target": domain,
                        "mode":   "domain",
                        "limit":  limit,
                    },
                )
                r.raise_for_status()
                data = r.json()
                pages = data.get("pages", data.get("top_pages", []))
                return [
                    {
                        "url":      p.get("url", ""),
                        "traffic":  int(p.get("traffic", 0) or 0),
                        "keywords": int(p.get("keywords", 0) or 0),
                    }
                    for p in pages
                ]
        except Exception as exc:
            logger.warning("Ahrefs get_top_pages error for %s: %s", domain, exc)
            return []

    async def get_keywords_for_url(self, url: str, limit: int = 10) -> List[dict]:
        """Return top organic keywords for a URL."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, base_url=_BASE_URL) as client:
                r = await client.get(
                    "/site-explorer/organic-keywords",
                    headers=self._headers(),
                    params={
                        "select": "keyword,position,traffic,volume",
                        "target": url,
                        "mode":   "exact",
                        "limit":  limit,
                    },
                )
                r.raise_for_status()
                data = r.json()
                kws = data.get("keywords", data.get("organic_keywords", []))
                return [
                    {
                        "keyword":  k.get("keyword", ""),
                        "position": int(k.get("position", 99) or 99),
                        "traffic":  int(k.get("traffic", 0) or 0),
                        "volume":   int(k.get("volume", 0) or 0),
                    }
                    for k in kws
                ]
        except Exception as exc:
            logger.warning("Ahrefs get_keywords_for_url error for %s: %s", url, exc)
            return []

    async def batch_url_metrics(self, urls: List[str], concurrency: int = 3) -> Dict[str, dict]:
        """Fetch metrics for multiple URLs with concurrency control."""
        sem     = asyncio.Semaphore(concurrency)
        results = {}

        async def _fetch(u: str):
            async with sem:
                results[u] = await self.get_url_metrics(u)

        await asyncio.gather(*[_fetch(u) for u in urls])
        return results


def get_client() -> AhrefsClient:
    """Return a configured AhrefsClient (or raise if no key)."""
    key = os.getenv("AHREFS_API_KEY", "")
    if not key:
        raise RuntimeError("AHREFS_API_KEY not configured")
    return AhrefsClient(key)
