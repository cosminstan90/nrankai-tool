"""
Extracts the SEO-relevant shape of a page so two versions can be compared
(Etapa 6 of docs/IMPROVEMENTS_PLAN.md).

The tool had no memory of page content: core/scrape_state.py hashes pages, so
it knows *that* a page changed, but the previous HTML is overwritten on the
next scrape, so nothing could say *what* changed. A snapshot is the missing
half -- small enough to keep every time (measured on a real page: 4.3 KB of
extracted fields against 67.7 KB of HTML).

Scope note, measured rather than assumed: the scraper stores document.body
only (core/web_scraper.py's DEEP_HTML_SCRIPT ends `getDeepHTML(document.body)`),
so <title>, meta description and rel=canonical are not in the stored data at
all -- present in 0 of 40 real stored pages, though present on the live pages.
Those fields are reported as None here rather than "", because a diff would
otherwise announce "title removed" on every page it ever looked at.
"""

import hashlib
import json
import logging
import re
from typing import List, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Reused rather than reimplemented: same pattern core/technical_facts.py uses
# to find JSON-LD blocks.
_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _jsonld_types(html: str) -> List[str]:
    types: List[str] = []
    for match in _JSONLD_RE.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            for node in (graph if isinstance(graph, list) else [item]):
                if not isinstance(node, dict):
                    continue
                t = node.get("@type")
                if isinstance(t, list):
                    types.extend(str(x) for x in t)
                elif t:
                    types.append(str(t))

    seen, out = set(), []
    for t in types:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _is_internal(href: str, site_host: str) -> bool:
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return False
    host = urlparse(href).netloc.lower()
    return host == "" or host == site_host


def extract_page_fields(html: str, page_url: str) -> dict:
    """
    The SEO-relevant shape of one page, as a plain dict ready to store.

    page_url decides which links count as internal; only its host is used.
    """
    soup = BeautifulSoup(html, "html.parser")
    site_host = urlparse(page_url).netloc.lower()

    # Present only if the stored HTML happens to include them. It normally does
    # not -- see the module docstring -- so None means "not captured", which the
    # diff must not read as "removed".
    title_tag = soup.find("title")
    meta_desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    canonical = soup.find("link", attrs={"rel": re.compile(r"^canonical$", re.I)})

    internal, external = [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        (internal if _is_internal(href, site_host) else external).append(href)

    images = soup.find_all("img")
    without_alt = [i for i in images if not (i.get("alt") or "").strip()]

    text = soup.get_text(" ", strip=True)

    return {
        "url": page_url,
        "title": title_tag.get_text(strip=True) if title_tag else None,
        "meta_description": (meta_desc.get("content") or "").strip() if meta_desc else None,
        "canonical": (canonical.get("href") or "").strip() if canonical else None,
        "h1": [h.get_text(strip=True) for h in soup.find_all("h1")],
        "h2": [h.get_text(strip=True) for h in soup.find_all("h2")],
        "h3": [h.get_text(strip=True) for h in soup.find_all("h3")],
        "word_count": len(text.split()),
        "internal_links": sorted(set(internal)),
        "external_link_count": len(set(external)),
        "images_total": len(images),
        "images_without_alt": len(without_alt),
        "schema_types": _jsonld_types(html),
        # Hashes the extracted shape, not the raw HTML: a changed build id or
        # cache-busting query in an asset URL should not read as a content
        # change, while a reworded heading should.
        "content_hash": _shape_hash(text, soup),
    }


def _shape_hash(text: str, soup: BeautifulSoup) -> str:
    payload = json.dumps({
        "text": text,
        "h": [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])],
    }, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
