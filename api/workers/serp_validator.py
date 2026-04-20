"""
SERP Validator (Prompt 19)
==========================
Validates fan-out queries against real Google SERP results via Serper.dev API.
Rate limit: max 5 requests/second → 0.2s delay between calls.

Usage:
    validator = SERPValidator(api_key=os.getenv("SERPER_API_KEY"))
    results   = await validator.validate_batch(queries, "example.com", gl="us", hl="en")
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("serp_validator")

SERPER_ENDPOINT   = "https://google.serper.dev/search"
SERP_COST_PER_QUERY = 0.001   # USD per Serper.dev API call


@dataclass
class SERPResult:
    query:                   str
    target_found:            bool            = False
    target_position:         Optional[int]   = None     # 1-based
    featured_snippet_domain: Optional[str]  = None
    people_also_ask:         List[str]       = field(default_factory=list)
    top_10_domains:          List[str]       = field(default_factory=list)
    error:                   Optional[str]   = None


def _domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc.lstrip("www.")
    except Exception:
        return url


class SERPValidator:

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def check_ranking(
        self,
        query: str,
        target_domain: str,
        gl: str = "us",
        hl: str = "en",
        num: int = 10,
    ) -> SERPResult:
        """POST to Serper.dev and extract SERP data for *query*."""
        try:
            import httpx
        except ImportError:
            return SERPResult(query=query, error="httpx not installed")

        body = {
            "q":           query,
            "gl":          gl,
            "hl":          hl,
            "num":         num,
            "autocorrect": False,
        }
        headers = {
            "X-API-KEY":    self.api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(SERPER_ENDPOINT, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Serper.dev error for query %r: %s", query, exc)
            return SERPResult(query=query, error=str(exc))

        result = SERPResult(query=query)

        # Organic results
        organic = data.get("organic", [])
        top_domains: list[str] = []
        tgt_clean = target_domain.lower().replace("www.", "").strip("/")
        for pos, item in enumerate(organic[:10], start=1):
            link   = item.get("link", "")
            domain = _domain_from_url(link)
            top_domains.append(domain)
            if tgt_clean in domain or domain in tgt_clean:
                result.target_found    = True
                result.target_position = pos
        result.top_10_domains = top_domains

        # Featured snippet
        snip = data.get("answerBox") or data.get("knowledgeGraph")
        if snip and snip.get("link"):
            result.featured_snippet_domain = _domain_from_url(snip["link"])

        # People Also Ask
        result.people_also_ask = [
            item.get("question", "") for item in data.get("peopleAlsoAsk", [])
            if item.get("question")
        ]

        return result

    async def validate_batch(
        self,
        queries: List[str],
        target_domain: str,
        gl: str = "us",
        hl: str = "en",
        max_queries: int = 20,
    ) -> List[SERPResult]:
        """
        Validate up to *max_queries* fan-out queries with a 0.2s delay between calls.
        Returns one SERPResult per query (in the same order).
        """
        results: List[SERPResult] = []
        for q in queries[:max_queries]:
            sr = await self.check_ranking(q, target_domain, gl=gl, hl=hl)
            results.append(sr)
            await asyncio.sleep(0.2)   # stay under Serper's 5 req/s limit
        return results
