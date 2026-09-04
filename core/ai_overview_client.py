"""
Google AI Overview presence/citation check, via DataForSEO's SERP API.

Etapa 4 of docs/IMPROVEMENTS_PLAN.md. api/routes/visibility.py already
measures real citations/mentions by asking ChatGPT/Claude/Perplexity a
question and text-searching the response -- but Google's AI Overviews are
the largest AI search surface by volume and were never measured at all, only
ever given advice by prompts/ai_overview_optimization.yaml. AI Overviews
don't show up by asking an LLM a question; they show up in Google's own
search results, which is what this actually queries.

Schema verified against one real, live DataForSEO call (2026-09-04, keyword
"how does photosynthesis work", ~$0.003) rather than from memory of their
docs -- see the ai_overview item's real shape below. `load_async_ai_overview`
is passed so DataForSEO waits for the overview's content in the same
response when possible; `asynchronous_ai_overview: true` on the returned item
means Google rendered it asynchronously and DataForSEO did not have content
for it in this call. There is no verified follow-up-call mechanism for that
case in this codebase, so it degrades to "AI Overview is present but this
tool has no content for it" rather than guessing at unverified endpoint
mechanics -- a known, stated gap, not a bug.
"""

import base64
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_SERP_URL = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"


def dfs_configured() -> bool:
    return bool(os.getenv("DATAFORSEO_LOGIN") and os.getenv("DATAFORSEO_PASSWORD"))


def _dfs_auth() -> str:
    login = os.getenv("DATAFORSEO_LOGIN", "")
    pw = os.getenv("DATAFORSEO_PASSWORD", "")
    return "Basic " + base64.b64encode(f"{login}:{pw}".encode()).decode()


async def fetch_ai_overview(
    keyword: str, location_code: int = 2840, language_code: str = "en", device: str = "desktop",
) -> Optional[dict]:
    """
    Returns None for three genuinely different situations, all "nothing to
    report" rather than a caller-visible error: DataForSEO isn't configured,
    the HTTP call itself failed, or (by far the most common case) Google
    simply did not show an AI Overview for this keyword at all -- most
    keywords don't trigger one, and that absence is real information, not a
    failure to fetch it.

    On success: {"asynchronous": bool, "markdown": str, "references": [{domain,
    url, title, source}], "raw": <the original ai_overview item, for
    reprocessing without a re-fetch>}. `references` is the deduplicated,
    overview-wide citation list (DataForSEO's top-level `references` field on
    the ai_overview item) rather than the per-paragraph ones nested under
    `items[].references` -- the top-level list is what answers "which domains
    are cited anywhere in this overview", which is what Etapa 4 asks for.
    """
    if not dfs_configured():
        return None

    payload = [{
        "keyword": keyword,
        "location_code": location_code,
        "language_code": language_code,
        "device": device,
        "load_async_ai_overview": True,
    }]

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                _SERP_URL,
                headers={"Authorization": _dfs_auth(), "Content-Type": "application/json"},
                json=payload,
            )
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("AI Overview SERP request failed for %r: %s", keyword, exc)
        return None

    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        logger.warning("AI Overview SERP task error for %r: %s", keyword, task.get("status_message"))
        return None

    result = (task.get("result") or [{}])[0]
    items = result.get("items") or []
    aio = next((it for it in items if it.get("type") == "ai_overview"), None)
    if not aio:
        return None

    references = [
        {"domain": r.get("domain"), "url": r.get("url"), "title": r.get("title"), "source": r.get("source")}
        for r in (aio.get("references") or [])
    ]

    return {
        "asynchronous": bool(aio.get("asynchronous_ai_overview")),
        "markdown": aio.get("markdown") or "",
        "references": references,
        "raw": aio,
    }
