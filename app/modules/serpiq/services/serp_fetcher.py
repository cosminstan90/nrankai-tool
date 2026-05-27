"""
SerpIQ — SERP Fetcher
======================
Pulls live organic SERP results from DataForSEO and maps them to the
SERPItem data class used throughout the SerpIQ module.

Public interface
----------------
  SERPItem              — dataclass for one ranked result
  SERPFetcher           — async service; call fetch_serp() for a keyword
  extract_keyword_from_url(url) — derive a search keyword from a URL path

DataForSEO endpoint
-------------------
  POST /v3/serp/google/organic/live/advanced
  Auth : Basic (DATAFORSEO_LOGIN : DATAFORSEO_PASSWORD)
  Docs : https://docs.dataforseo.com/v3/serp/google/organic/live/advanced/
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("serpiq.fetcher")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DFS_ENDPOINT  = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
_USER_AGENT    = "nrankai-serpiq/1.0"

# Rough word-count estimator: description chars × factor ≈ page word count.
# Rationale: a 160-char snippet ≈ 30 words; typical pages are ~25× longer.
_DESC_WORD_FACTOR = 25

# DataForSEO item types that map to special result kinds
_TYPE_FEATURED_SNIPPET = "featured_snippet"
_TYPE_LOCAL_PACK       = "local_pack"
_TYPE_VIDEO            = "video"
_ORGANIC_TYPES         = {"organic", "featured_snippet"}

# Path extensions to strip when deriving a keyword from a URL
_STRIP_EXTENSIONS = re.compile(
    r"\.(html?|php|aspx?|cfm|jsp|xml|json|txt|pdf)$", re.I
)
# Characters that act as word separators in URL path segments
_SLUG_SEPARATORS = re.compile(r"[-_+]+")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SERPItem:
    """One ranked position in a SERP result set."""

    position:             int
    url:                  Optional[str]       = None
    domain:               Optional[str]       = None
    title:                Optional[str]       = None
    description:          Optional[str]       = None
    word_count_estimated: Optional[int]       = None
    has_schema:           bool                = False
    schema_types:         list[str]           = field(default_factory=list)
    is_featured_snippet:  bool                = False
    is_local_pack:        bool                = False
    is_video:             bool                = False

    def to_dict(self) -> dict:
        return {
            "position":             self.position,
            "url":                  self.url,
            "domain":               self.domain,
            "title":                self.title,
            "description":          self.description,
            "word_count_estimated": self.word_count_estimated,
            "has_schema":           self.has_schema,
            "schema_types":         self.schema_types,
            "is_featured_snippet":  self.is_featured_snippet,
            "is_local_pack":        self.is_local_pack,
            "is_video":             self.is_video,
        }


# ---------------------------------------------------------------------------
# SERPFetcher
# ---------------------------------------------------------------------------

class SERPFetcher:
    """
    Thin async client around the DataForSEO live/advanced SERP endpoint.

    Instantiation raises ValueError if credentials are missing from env.
    """

    def __init__(
        self,
        api_login:    str | None = None,
        api_password: str | None = None,
    ) -> None:
        login = api_login    or os.getenv("DATAFORSEO_LOGIN",    "")
        pw    = api_password or os.getenv("DATAFORSEO_PASSWORD", "")
        if not (login and pw):
            raise ValueError(
                "DataForSEO credentials missing — set DATAFORSEO_LOGIN and "
                "DATAFORSEO_PASSWORD in .env"
            )
        self._auth = "Basic " + base64.b64encode(f"{login}:{pw}".encode()).decode()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_serp(
        self,
        keyword:       str,
        location_code: int = 2040,
        language_code: str = "ro",
        depth:         int = 20,
    ) -> list[SERPItem]:
        """
        Fetch live SERP results for *keyword* and return up to *depth* items.

        The list is ordered by position (1-based).  Special result types
        (featured snippet, local pack, video) are flagged on the SERPItem
        but always consume one position slot.

        Returns an empty list on any non-recoverable error (logged at WARNING).
        """
        import httpx

        payload = [{
            "keyword":       keyword,
            "location_code": location_code,
            "language_code": language_code,
            "device":        "desktop",
            "os":            "windows",
            "depth":         depth,
            "calculate_rectangles": False,
        }]
        headers = {
            "Authorization": self._auth,
            "Content-Type":  "application/json",
            "User-Agent":    _USER_AGENT,
        }

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(_DFS_ENDPOINT, json=payload, headers=headers)
            data = resp.json()
        except Exception as exc:
            logger.warning("[SERPFetcher] network error for %r: %s", keyword, exc)
            return []

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        items      = self._parse_response(data, depth)

        logger.info(
            "[SERPFetcher] keyword=%r location=%d → %d items in %d ms",
            keyword, location_code, len(items), elapsed_ms,
        )
        return items

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, data: dict, depth: int) -> list[SERPItem]:
        """
        Walk the DataForSEO task/result tree and build SERPItem list.
        Organic items and featured snippets both count as ranked positions.
        Local pack and video items are flagged but do *not* claim an organic
        position number — they use their own position field if available.
        """
        items: list[SERPItem] = []

        for task in data.get("tasks", []):
            if task.get("status_code") != 20000:
                logger.warning(
                    "[SERPFetcher] task error %s: %s",
                    task.get("status_code"),
                    task.get("status_message", ""),
                )
                return []

            for result in task.get("result") or []:
                for raw in result.get("items") or []:
                    item = self._parse_item(raw)
                    if item is not None:
                        items.append(item)
                        if len(items) >= depth:
                            return items

        return items

    def _parse_item(self, raw: dict) -> SERPItem | None:
        """
        Map one DataForSEO item dict → SERPItem.
        Returns None for item types we don't want to surface (ads, etc.).
        """
        item_type = (raw.get("type") or "").lower()

        # Skip paid / non-organic result types
        if item_type in ("paid", "shopping", "app", "map"):
            return None

        position = raw.get("rank_absolute") or raw.get("position") or 0

        # Featured snippet
        is_featured = item_type == _TYPE_FEATURED_SNIPPET
        is_local    = item_type == _TYPE_LOCAL_PACK
        is_video    = item_type == _TYPE_VIDEO

        url    = raw.get("url") or raw.get("domain") or None
        domain = self._extract_domain(raw)
        title  = (raw.get("title") or "").strip() or None
        desc   = (raw.get("description") or "").strip() or None

        # Schema / rich snippet detection
        schema_types = self._extract_schema_types(raw)
        has_schema   = bool(schema_types)

        # Word count estimate from description
        word_count = self._estimate_word_count(desc)

        return SERPItem(
            position             = position,
            url                  = url,
            domain               = domain,
            title                = title,
            description          = desc,
            word_count_estimated = word_count,
            has_schema           = has_schema,
            schema_types         = schema_types,
            is_featured_snippet  = is_featured,
            is_local_pack        = is_local,
            is_video             = is_video,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_domain(raw: dict) -> str | None:
        """Prefer raw.domain field; fall back to parsing raw.url."""
        domain = raw.get("domain") or None
        if domain:
            return domain.lower().lstrip("www.").rstrip("/")
        url = raw.get("url") or ""
        if url:
            try:
                parsed = urlparse(url if "://" in url else "https://" + url)
                return parsed.hostname.lstrip("www.") if parsed.hostname else None
            except Exception:
                pass
        return None

    @staticmethod
    def _extract_schema_types(raw: dict) -> list[str]:
        """
        Pull schema.org @type values out of the rich_snippet or
        extended_snippet field DataForSEO may include on organic results.
        """
        types: list[str] = []

        rich = raw.get("rich_snippet") or {}
        # DataForSEO wraps schema data under rich_snippet.top/bottom -> items
        for section in ("top", "bottom"):
            snippet_section = rich.get(section) or {}
            for item in snippet_section.get("items") or []:
                t = item.get("type") or item.get("@type") or ""
                if t and t not in types:
                    types.append(t)

        # Some endpoints put it directly under rich_snippet.schema_type
        st = rich.get("schema_type")
        if st and isinstance(st, str) and st not in types:
            types.append(st)
        elif st and isinstance(st, list):
            for t in st:
                if t and t not in types:
                    types.append(t)

        return types

    @staticmethod
    def _estimate_word_count(description: str | None) -> int | None:
        """
        Return a rough page word count estimate based on the SERP description.

        Heuristic: description ~30 words → page ~750 words (factor 25).
        The estimate is only indicative; use URLAnalyzer for real counts.
        """
        if not description:
            return None
        desc_words = len(description.split())
        return max(50, desc_words * _DESC_WORD_FACTOR)


# ---------------------------------------------------------------------------
# Keyword extractor from URL
# ---------------------------------------------------------------------------

def extract_keyword_from_url(url: str) -> str:
    """
    Derive the primary search keyword from a URL path.

    Algorithm
    ---------
    1. Parse the URL, take the path component.
    2. Split on '/' and discard empty segments.
    3. Strip common file extensions (.html, .php, …).
    4. For each segment: replace hyphens / underscores with spaces, lowercase.
    5. Pick the *last* non-trivial segment (slug) as the main keyword,
       but if the path has only one segment use that directly.
    6. Strip generic stop-path parts (page, category, tag, p, id, etc.)
       so "/blog/credit-ipotecar-prima-casa/" → "credit ipotecar prima casa".

    Returns the clean keyword string; falls back to the raw hostname on
    failure (so the caller always gets a non-empty value).

    Examples
    --------
    >>> extract_keyword_from_url("https://example.ro/credit-ipotecar-prima-casa/")
    'credit ipotecar prima casa'
    >>> extract_keyword_from_url("https://example.ro/blog/2024/01/cel-mai-bun-cont-curent")
    'cel mai bun cont curent'
    >>> extract_keyword_from_url("https://example.ro/")
    'example.ro'
    """
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
    except Exception:
        return url.strip()

    path = parsed.path or ""

    # Split on slashes, strip blanks
    segments = [s for s in path.split("/") if s.strip()]

    # Remove common structural path segments
    _STOP_SEGMENTS = frozenset({
        "blog", "news", "articles", "articole", "stiri", "tag", "tags",
        "category", "categorie", "categorii", "page", "p", "post", "posts",
        "en", "ro", "de", "fr", "es", "index", "home",
        "archive", "archives", "author", "authors",
    })

    # Strip extensions from each segment
    cleaned: list[str] = []
    for seg in segments:
        seg = _STRIP_EXTENSIONS.sub("", seg)
        if seg and seg.lower() not in _STOP_SEGMENTS:
            # Replace separators with spaces
            keyword_candidate = _SLUG_SEPARATORS.sub(" ", seg).strip().lower()
            # Drop purely numeric or very short tokens (IDs)
            if keyword_candidate and not re.fullmatch(r"\d+", keyword_candidate):
                cleaned.append(keyword_candidate)

    if cleaned:
        # Prefer the last meaningful segment (usually the most specific)
        return cleaned[-1]

    # Fallback: return hostname without www
    host = (parsed.hostname or "").lstrip("www.")
    return host or url.strip()
