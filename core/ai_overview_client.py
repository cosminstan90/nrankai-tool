"""
Google AI Overview presence/citation check, via DataForSEO's SERP API.

Etapa 4 of docs/IMPROVEMENTS_PLAN.md. api/routes/visibility.py already
measures real citations/mentions by asking ChatGPT/Claude/Perplexity a
question and text-searching the response -- but Google's AI Overviews are
the largest AI search surface by volume and were never measured at all, only
ever given advice by prompts/ai_overview_optimization.yaml. AI Overviews
don't show up by asking an LLM a question; they show up in Google's own
search results, which is what this actually queries.

This used to make its own HTTP call. Since Etapa 8 it is a thin wrapper over
core/serp_client.py, the single client for this endpoint -- the original
version paid for the whole SERP and kept only the ai_overview block, while
SerpIQ called the same endpoint separately.

`asynchronous_ai_overview: true` on the returned item means Google rendered
the overview asynchronously and DataForSEO had no content for it in this call.
There is no verified follow-up mechanism for that case in this codebase, so it
degrades to "present, no content" rather than guessing at unverified endpoint
behaviour -- a known, stated gap.

The default location is Romania (2642), from core/dataforseo_locations. It
used to default to 2840 (United States), which would have checked Romanian
queries for a .ro site against Google US.
"""

import logging
from typing import Optional

from core import serp_client
from core.dataforseo_locations import get as get_location

logger = logging.getLogger(__name__)

_DEFAULT = get_location("RO")


def dfs_configured() -> bool:
    return serp_client.dfs_configured()


async def fetch_ai_overview(
    keyword: str,
    location_code: int = _DEFAULT.location_code,
    language_code: str = _DEFAULT.language_code,
    device: str = "desktop",
) -> Optional[dict]:
    """
    Returns None for three genuinely different situations, all "nothing to
    report" rather than a caller-visible error: DataForSEO isn't configured,
    the HTTP call itself failed, or (by far the most common case) Google
    simply did not show an AI Overview for this keyword at all -- most
    keywords don't trigger one, and that absence is real information, not a
    failure to fetch it.

    On success: {"asynchronous": bool, "markdown": str, "references": [{domain,
    url, title, source}], "raw": <the original ai_overview item>}.
    """
    serp = await serp_client.fetch_serp(keyword, location_code, language_code, device=device)
    return serp.ai_overview if serp else None
