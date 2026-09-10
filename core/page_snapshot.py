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
so <title>, meta description and rel=canonical were missing from every stored
page -- 0 of 40 sampled -- though present on the live pages. They now arrive
via the head sidecar the scraper writes alongside the HTML. Pages scraped
before that existed have no sidecar, so those fields stay None, meaning "not
captured" rather than "", which a diff would announce as "title removed" on
every page it ever looked at.
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


def _merge_schema_types(body_types: List[str], head_blocks) -> List[str]:
    """
    Schema types from both sources, deduplicated and order-stable.

    A block genuinely appears in both when the browser relocates it: the raw
    HTTP response puts ld+json before </head>, the parser moves it into body,
    and the scraper stores the rendered body. Counting it twice would inflate
    the page's schema list for no reason.
    """
    merged = list(body_types)
    for raw in (head_blocks or []):
        for t in _jsonld_types(f'<script type="application/ld+json">{raw}</script>'):
            if t not in merged:
                merged.append(t)
    return merged


def _is_internal(href: str, site_host: str) -> bool:
    href = (href or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return False
    host = urlparse(href).netloc.lower()
    return host == "" or host == site_host


def extract_page_fields(html: str, page_url: str, head_meta: Optional[dict] = None) -> dict:
    """
    The SEO-relevant shape of one page, as a plain dict ready to store.

    page_url decides which links count as internal; only its host is used.

    head_meta is the sidecar core/web_scraper.py writes next to the HTML
    (see load_head_meta). None means the page was scraped before head capture
    existed, so title/meta/canonical stay None -- "not captured", which
    core/page_diff.py refuses to read as "removed".
    """
    head_meta = head_meta or {}
    soup = BeautifulSoup(html, "html.parser")
    site_host = urlparse(page_url).netloc.lower()

    # Present only if the stored HTML happens to include them. It normally does
    # not -- see the module docstring -- so None means "not captured", which the
    # diff must not read as "removed".
    title_tag = soup.find("title")
    meta_desc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    canonical = soup.find("link", attrs={"rel": re.compile(r"^canonical$", re.I)})
    robots_tag = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})

    internal, external = [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        (internal if _is_internal(href, site_host) else external).append(href)

    images = soup.find_all("img")
    without_alt = [i for i in images if not (i.get("alt") or "").strip()]

    text = soup.get_text(" ", strip=True)

    fields = {
        "url": page_url,
        # Head metadata wins where present: it comes straight from the rendered
        # document, while the body copy exists only if the markup happened to
        # put these tags there.
        "title": head_meta.get("title") or (title_tag.get_text(strip=True) if title_tag else None),
        "meta_description": head_meta.get("meta_description") or (
            (meta_desc.get("content") or "").strip() if meta_desc else None),
        "canonical": head_meta.get("canonical") or (
            (canonical.get("href") or "").strip() if canonical else None),
        "meta_robots": head_meta.get("meta_robots") or (
            (robots_tag.get("content") or "").strip() if robots_tag else None),
        "h1": [h.get_text(strip=True) for h in soup.find_all("h1")],
        "h2": [h.get_text(strip=True) for h in soup.find_all("h2")],
        "h3": [h.get_text(strip=True) for h in soup.find_all("h3")],
        "word_count": len(text.split()),
        "internal_links": sorted(set(internal)),
        "external_link_count": len(set(external)),
        "images_total": len(images),
        "images_without_alt": len(without_alt),
        "schema_types": _merge_schema_types(_jsonld_types(html), head_meta.get("jsonld")),
    }

    # Hashes the extracted shape, not the raw HTML: a changed build id or
    # cache-busting query in an asset URL should not read as a content change,
    # while a reworded heading should.
    #
    # Head metadata is part of that shape. compare_runs short-circuits on this
    # hash, so leaving head out made a title rewrite or a switch to noindex
    # invisible whenever the body was untouched -- which is precisely what a
    # metadata edit in a CMS looks like. Caught in end-to-end verification,
    # where a real title change plus noindex produced an empty diff.
    fields["content_hash"] = _shape_hash(text, soup, fields)
    return fields


def _shape_hash(text: str, soup: BeautifulSoup, fields: dict) -> str:
    payload = json.dumps({
        "text": text,
        "h": [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])],
        "head": [fields.get("title"), fields.get("meta_description"),
                 fields.get("canonical"), fields.get("meta_robots")],
    }, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
