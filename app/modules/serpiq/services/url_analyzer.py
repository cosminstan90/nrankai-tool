"""
SerpIQ — URL Analyzer
======================
Fetches a URL asynchronously and extracts the on-page signals needed for
the SERP analysis: title, meta description, H1, word count, schema types,
canonical, meta robots, and Open Graph tags.

Public interface
----------------
  URLAnalysis           — Pydantic model returned by analyze_url()
  URLAnalyzer           — async service class
    analyze_url(url)    — fetch + extract; never raises; sets fetch_failed=True
    find_url_in_serp    — locate an input URL in a SERPItem list by domain

Design constraints
------------------
- Hard timeout: 8 s (configurable). If the page doesn't respond in time,
  a partial URLAnalysis is returned with fetch_failed=True.
- No exceptions propagate to callers — every failure path is logged and
  reflected in the URLAnalysis fields.
- BeautifulSoup (html.parser built-in) is used; lxml is tried first if
  available for speed.
- JSON-LD blocks are parsed with json.loads; malformed blocks are skipped.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel

from api.utils.domain import strip_www

logger = logging.getLogger("serpiq.url_analyzer")

# ---------------------------------------------------------------------------
# User-agent (same style as ContentIQ)
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "Mozilla/5.0 (compatible; nrankai-serpiq/1.0; +https://nrankai.com/bot)"
)

# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class URLAnalysis(BaseModel):
    """On-page signals extracted from a live URL fetch."""

    url:              str

    # HTTP / crawlability
    status_code:      Optional[int]  = None
    fetch_failed:     bool           = False    # True when request timed out / errored
    redirect_url:     Optional[str]  = None     # final URL after redirects

    # On-page content
    title:            Optional[str]  = None
    meta_description: Optional[str]  = None
    h1:               Optional[str]  = None
    word_count:       Optional[int]  = None
    canonical_url:    Optional[str]  = None
    is_indexable:     Optional[bool] = None     # False when noindex is set

    # Schema / structured data
    has_schema:       bool           = False
    schema_types:     list[str]      = []

    # Open Graph
    og_title:         Optional[str]  = None
    og_description:   Optional[str]  = None

    # Ahrefs authority (not populated here — filled by caller if available)
    domain_rating:    Optional[int]  = None
    backlinks:        Optional[int]  = None
    referring_domains: Optional[int] = None

    model_config = {"from_attributes": True}

    def to_dict(self) -> dict:
        return self.model_dump()


# ---------------------------------------------------------------------------
# URLAnalyzer
# ---------------------------------------------------------------------------

class URLAnalyzer:
    """
    Fetches and analyses a single URL.  All methods are async-safe.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze_url(self, url: str, timeout: int = 8) -> URLAnalysis:
        """
        Fetch *url* and return a URLAnalysis.

        On any error (network timeout, DNS failure, HTTP 4xx/5xx, parse
        error) the method returns a partial URLAnalysis with
        ``fetch_failed=True`` rather than raising.

        Parameters
        ----------
        url:     The absolute URL to fetch (must begin with http/https).
        timeout: Max seconds to wait for the server to respond.
        """
        import httpx
        from bs4 import BeautifulSoup

        result = URLAnalysis(url=url)

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ro,en;q=0.8",
                },
                max_redirects=5,
            ) as client:
                resp = await client.get(url)

            result.status_code = resp.status_code

            # Track final URL after redirects
            if str(resp.url) != url:
                result.redirect_url = str(resp.url)

            if resp.status_code >= 400:
                # Non-OK status — return what we have; not a fetch_failed
                result.fetch_failed = False
                return result

            # Only parse HTML-ish responses
            ct = resp.headers.get("content-type", "")
            if "html" not in ct.lower():
                logger.debug("[URLAnalyzer] non-HTML content-type %r for %s", ct, url)
                return result

            parser = "lxml" if _lxml_available() else "html.parser"
            soup   = BeautifulSoup(resp.text, parser)

            result.title            = _extract_title(soup)
            result.meta_description = _extract_meta(soup, "description")
            result.h1               = _extract_h1(soup)
            result.word_count       = _extract_word_count(soup)
            result.canonical_url    = _extract_canonical(soup, str(resp.url))
            result.is_indexable     = _check_indexable(soup)
            result.has_schema, result.schema_types = _extract_schema(soup)
            result.og_title         = _extract_meta(soup, "og:title",       prop=True)
            result.og_description   = _extract_meta(soup, "og:description", prop=True)

        except Exception as exc:
            logger.warning("[URLAnalyzer] fetch failed for %s: %s", url, exc)
            result.fetch_failed = True

        return result

    # ------------------------------------------------------------------
    # SERP matching
    # ------------------------------------------------------------------

    @staticmethod
    def find_url_in_serp(
        url:        str,
        serp_items: list,
    ) -> int | None:
        """
        Return the 1-based position of *url* in *serp_items*, or None.

        Matching is domain-level (both with and without ``www.`` are
        considered equal).  This is intentional: for competitive analysis
        the presence of the same *domain* at a position matters more than
        the exact path match.

        Parameters
        ----------
        url:        The URL to look up (may be a full URL or just a domain).
        serp_items: List of SERPItem-like objects (dataclass or dict).
                    Each item must expose ``position`` and either ``domain``
                    or ``url``.
        """
        target_domain = _normalise_domain(url)
        if not target_domain:
            return None

        for item in serp_items:
            # Support both dataclass (attribute access) and dict access
            item_domain = (
                item.domain if hasattr(item, "domain")
                else item.get("domain") if isinstance(item, dict)
                else None
            )
            if not item_domain:
                # Fall back to parsing the item's url field
                item_url = (
                    item.url if hasattr(item, "url")
                    else item.get("url") if isinstance(item, dict)
                    else None
                )
                item_domain = _normalise_domain(item_url or "")

            if item_domain and _domains_match(target_domain, item_domain):
                pos = (
                    item.position if hasattr(item, "position")
                    else item.get("position") if isinstance(item, dict)
                    else None
                )
                return int(pos) if pos is not None else None

        return None


# ---------------------------------------------------------------------------
# BeautifulSoup extraction helpers (module-level, reusable)
# ---------------------------------------------------------------------------

def _extract_title(soup) -> str | None:
    tag = soup.find("title")
    return tag.get_text(strip=True) or None if tag else None


def _extract_h1(soup) -> str | None:
    tag = soup.find("h1")
    return tag.get_text(strip=True) or None if tag else None


def _extract_meta(soup, name: str, prop: bool = False) -> str | None:
    """
    Extract <meta name=... content=...> or <meta property=... content=...>.
    ``prop=True`` switches to the ``property`` attribute (Open Graph).
    """
    attr = "property" if prop else "name"
    tag  = soup.find("meta", attrs={attr: re.compile(re.escape(name), re.I)})
    if tag:
        val = tag.get("content", "").strip()
        return val or None
    return None


def _extract_canonical(soup, base_url: str) -> str | None:
    tag = soup.find("link", rel="canonical")
    if tag:
        href = tag.get("href", "").strip()
        if href:
            # Resolve relative canonicals
            if href.startswith("http"):
                return href
            return urljoin(base_url, href)
    return None


def _check_indexable(soup) -> bool:
    """Return False only when an explicit ``noindex`` directive is found."""
    # <meta name="robots" content="noindex">
    robots_tag = soup.find(
        "meta",
        attrs={"name": re.compile(r"^robots$", re.I)},
    )
    if robots_tag:
        content = (robots_tag.get("content") or "").lower()
        if "noindex" in content:
            return False
    # <meta name="googlebot" content="noindex">
    googlebot = soup.find(
        "meta",
        attrs={"name": re.compile(r"^googlebot$", re.I)},
    )
    if googlebot:
        content = (googlebot.get("content") or "").lower()
        if "noindex" in content:
            return False
    return True


def _extract_word_count(soup) -> int:
    """
    Count words in the visible body text.

    Removes script, style, nav, header, footer, and aside elements
    before counting to get a cleaner content word count.
    """
    # Work on a copy so we don't mutate the caller's soup
    import copy
    body = copy.copy(soup.find("body") or soup)

    for noise in body(["script", "style", "nav", "header", "footer",
                        "aside", "noscript", "template"]):
        noise.decompose()

    # Prefer <main> or <article> content when available
    content_el = body.find("main") or body.find("article") or body
    text = content_el.get_text(" ", strip=True)
    return len(text.split())


def _extract_schema(soup) -> tuple[bool, list[str]]:
    """
    Parse all <script type="application/ld+json"> blocks and collect
    unique @type values.

    Returns (has_schema, sorted_type_list).
    """
    types: list[str] = []

    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # data may be a single object or a @graph array
        objects = []
        if isinstance(data, list):
            objects = data
        elif isinstance(data, dict):
            graph = data.get("@graph")
            objects = graph if isinstance(graph, list) else [data]

        for obj in objects:
            if not isinstance(obj, dict):
                continue
            raw_type = obj.get("@type")
            if isinstance(raw_type, str):
                if raw_type not in types:
                    types.append(raw_type)
            elif isinstance(raw_type, list):
                for t in raw_type:
                    if t and t not in types:
                        types.append(t)

    return bool(types), sorted(types)


# ---------------------------------------------------------------------------
# Domain normalisation helpers
# ---------------------------------------------------------------------------

def _normalise_domain(value: str) -> str | None:
    """
    Extract a lower-cased domain string from a URL or bare hostname,
    stripping the ``www.`` prefix and trailing slashes.

    Returns None for empty/unparseable input.
    """
    if not value:
        return None
    value = value.strip()
    # Add scheme if missing so urlparse works correctly
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    try:
        hostname = urlparse(value).hostname or ""
    except Exception:
        return None
    return strip_www(hostname.lower()) or None


def _domains_match(a: str, b: str) -> bool:
    """
    True when two normalised domain strings refer to the same site.

    Handles cases where one includes a subdomain and the other doesn't,
    e.g. ``shop.example.ro`` matches ``example.ro``.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    # One is a subdomain of the other
    return a.endswith("." + b) or b.endswith("." + a)


def _lxml_available() -> bool:
    """Silently return True if lxml is importable (faster than html.parser)."""
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False
