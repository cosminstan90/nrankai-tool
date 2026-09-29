"""
The single client for DataForSEO's Google organic SERP endpoint.

Etapa 8 of docs/IMPROVEMENTS_PLAN.md. Before this module the endpoint had two
independent callers: SerpIQ's SERPFetcher, and core/ai_overview_client.py,
written in Etapa 4 without noticing the first one existed. The AI Overview
check paid for the full SERP -- organic rankings included -- and kept only the
ai_overview block. The plan warned about exactly that: "don't do these in two
passes, you pay twice".

One call now yields both the AI Overview and the organic rankings.

Measured on a real Romanian SERP ("cea mai buna banca din romania", location
2642) before this was written:

  * rank_group and rank_absolute diverge. The first organic result was
    rank_group 1 but rank_absolute 2, because an AI Overview sat above it; by
    the end of the page rank_group 17 was rank_absolute 21. rank_group is what
    "we rank #3" means; rank_absolute is how far down the page that actually
    is. For a GEO tool the gap between them is the story, so both are kept.
  * The same site appears more than once (www.1asig.ro at 1, 1asig.ro at 13).
    A site's rank is its best position, with www normalised away.
  * A site absent from the results has no rank. It is reported as "not found
    within the first N", never as 0 or as a made-up sentinel.
"""

import base64
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

SERP_URL = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"

_ORGANIC_TYPES = {"organic", "featured_snippet"}

# From DataForSEO's own error list (/v3/appendix/errors, fetched 2026-09-29).
# These stop every paid call until someone acts, so they are raised, not
# treated as "nothing to report". 50001 ("Error While Checking the Balance")
# is deliberately absent: it is a transient server-side failure, not a lack of
# funds, and alarming on it would be crying wolf.
BILLING_CODES = {
    40200: "Payment Required",
    40203: "cost limit exceeded (adjustable at https://app.dataforseo.com/api-settings)",
    40210: "insufficient funds -- the account balance is too low",
}


class DataForSEOBillingError(RuntimeError):
    """
    DataForSEO refused the request for billing reasons.

    Raised instead of returning None because the two look identical otherwise:
    an exhausted balance and "Google showed no AI Overview" both came back as
    an empty result, so running out of credit was indistinguishable from
    genuine absence. The account had $0.89 left when this was added.
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"DataForSEO {code}: {message}")


def raise_for_billing(data: dict) -> None:
    """Raise DataForSEOBillingError if the response, or any task in it, is a billing refusal."""
    if not isinstance(data, dict):
        return
    candidates = [(data.get("status_code"), data.get("status_message"))]
    candidates += [(t.get("status_code"), t.get("status_message"))
                   for t in (data.get("tasks") or []) if isinstance(t, dict)]
    for code, message in candidates:
        if code in BILLING_CODES:
            raise DataForSEOBillingError(code, message or BILLING_CODES[code])


def dfs_configured() -> bool:
    return bool(os.getenv("DATAFORSEO_LOGIN") and os.getenv("DATAFORSEO_PASSWORD"))


def _dfs_auth() -> str:
    login = os.getenv("DATAFORSEO_LOGIN", "")
    pw = os.getenv("DATAFORSEO_PASSWORD", "")
    return "Basic " + base64.b64encode(f"{login}:{pw}".encode()).decode()


def normalize_host(value: Optional[str]) -> str:
    """Lowercase host with a leading www. removed; accepts bare domains or URLs."""
    if not value:
        return ""
    host = urlparse(value if "://" in value else f"https://{value}").hostname or ""
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def host_matches_site(host: str, site: str) -> bool:
    """
    True for the site itself and any of its subdomains.

    business.ing.ro ranking is still ING ranking. The ranking URL is stored
    alongside every observation, so which host actually ranked stays visible.
    """
    host, site = normalize_host(host), normalize_host(site)
    return bool(host and site) and (host == site or host.endswith("." + site))


@dataclass
class OrganicResult:
    rank_group: int
    rank_absolute: int
    url: Optional[str]
    domain: str
    title: Optional[str] = None
    is_featured_snippet: bool = False


@dataclass
class SerpResult:
    keyword: str
    location_code: int
    language_code: str
    depth: int
    organic: List[OrganicResult] = field(default_factory=list)
    ai_overview: Optional[dict] = None
    features: List[str] = field(default_factory=list)   # item types present, in order, deduplicated

    def site_rank(self, site: str) -> Optional[OrganicResult]:
        """The site's best organic result, or None if it is not in these results."""
        matches = [r for r in self.organic if host_matches_site(r.domain, site)]
        return min(matches, key=lambda r: r.rank_group) if matches else None

    def ai_overview_cites(self, site: str) -> bool:
        if not self.ai_overview:
            return False
        return any(host_matches_site(ref.get("domain") or ref.get("url") or "", site)
                   for ref in self.ai_overview.get("references") or [])


def parse_ai_overview(items: List[dict]) -> Optional[dict]:
    """
    The ai_overview block, or None when Google showed none.

    `references` is DataForSEO's overview-wide, deduplicated citation list --
    the one that answers "which domains are cited anywhere in this overview".
    """
    aio = next((it for it in items if it.get("type") == "ai_overview"), None)
    if not aio:
        return None
    return {
        "asynchronous": bool(aio.get("asynchronous_ai_overview")),
        "markdown": aio.get("markdown") or "",
        "references": [
            {"domain": r.get("domain"), "url": r.get("url"),
             "title": r.get("title"), "source": r.get("source")}
            for r in (aio.get("references") or [])
        ],
        "raw": aio,
    }


def parse_organic(items: List[dict]) -> List[OrganicResult]:
    """Organic results (featured snippet included), in page order."""
    out: List[OrganicResult] = []
    for it in items:
        if it.get("type") not in _ORGANIC_TYPES:
            continue
        rank_group = it.get("rank_group")
        rank_absolute = it.get("rank_absolute")
        if not isinstance(rank_group, int) or not isinstance(rank_absolute, int):
            continue
        out.append(OrganicResult(
            rank_group=rank_group,
            rank_absolute=rank_absolute,
            url=it.get("url"),
            domain=normalize_host(it.get("domain") or it.get("url")),
            title=(it.get("title") or "").strip() or None,
            is_featured_snippet=it.get("type") == "featured_snippet",
        ))
    return out


def parse_serp_response(data: dict, keyword: str, location_code: int,
                        language_code: str, depth: int) -> Optional[SerpResult]:
    """Turn a raw DataForSEO response into a SerpResult; None on a task error."""
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        logger.warning("SERP task error for %r: %s", keyword, task.get("status_message"))
        return None
    result = (task.get("result") or [{}])[0] or {}
    items = result.get("items") or []

    features: List[str] = []
    for it in items:
        t = it.get("type")
        if t and t not in features:
            features.append(t)

    return SerpResult(
        keyword=keyword,
        location_code=location_code,
        language_code=language_code,
        depth=depth,
        organic=parse_organic(items),
        ai_overview=parse_ai_overview(items),
        features=features,
    )


async def fetch_raw(keyword: str, location_code: int, language_code: str,
                    depth: int = 20, device: str = "desktop",
                    load_ai_overview: bool = True, extra: Optional[dict] = None) -> Optional[dict]:
    """
    One HTTP call to the endpoint. Returns the raw response, or None when
    DataForSEO is not configured or the request itself fails.

    Raises DataForSEOBillingError when DataForSEO refuses the request for
    billing reasons -- see BILLING_CODES.

    Exposed separately so SerpIQ can keep its own richer item parsing while
    sharing the transport, credentials and error handling.
    """
    if not dfs_configured():
        return None

    task = {
        "keyword": keyword,
        "location_code": location_code,
        "language_code": language_code,
        "device": device,
        "depth": depth,
    }
    if load_ai_overview:
        task["load_async_ai_overview"] = True
    if extra:
        task.update(extra)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                SERP_URL,
                headers={"Authorization": _dfs_auth(), "Content-Type": "application/json"},
                json=[task],
            )
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("SERP request failed for %r: %s", keyword, exc)
        return None

    # Outside the try on purpose: a billing refusal must reach the caller.
    raise_for_billing(data)
    return data


async def fetch_serp(keyword: str, location_code: int, language_code: str,
                     depth: int = 20, device: str = "desktop") -> Optional[SerpResult]:
    """Fetch and parse one SERP: organic rankings and AI Overview together."""
    data = await fetch_raw(keyword, location_code, language_code, depth=depth, device=device)
    if data is None:
        return None
    return parse_serp_response(data, keyword, location_code, language_code, depth)
