"""
Deterministic page features for citation comparison -- Pasul 16 of
docs/superpowers/plans/2026-09-30-next-steps.md.

"Optimize for AI" is generic advice. The pages an AI Overview or an LLM
actually cited for a query can be measured on the same, deterministic axes
as the tracked site's own page -- this module extracts those axes from raw
HTML; core/citation_comparison.py turns several pages' features into a
report.

Every feature is a fact extracted from markup, never a judgment -- no LLM
call happens in this module. A feature that couldn't be determined is None,
not a false-feeling default (e.g. a missing date is None, not "old").
"""
import re
from datetime import datetime, timezone
from typing import List, Optional

from bs4 import BeautifulSoup

from core.html2llm_converter import extract_content
from core.technical_facts import extract_structured_data_types

_NUMERIC_TOKEN_RE = re.compile(
    r"\d[\d.,]*\s?%|[\$€]\s?\d[\d.,]*|\d[\d.,]*\s?(?:lei|ron|eur|usd)\b",
    re.IGNORECASE,
)
_DATE_META_NAMES = {
    "published": ["article:published_time", "og:article:published_time", "date", "datePublished"],
    "modified": ["article:modified_time", "og:article:modified_time", "last-modified", "dateModified"],
}


def _numeric_density_per_100_words(text: str) -> float:
    """Concrete figures (prices, percentages, sums) per 100 words of body text."""
    words = text.split()
    if not words:
        return 0.0
    matches = len(_NUMERIC_TOKEN_RE.findall(text))
    return round(matches / len(words) * 100, 2)


def _looks_like_qa_content(text: str) -> bool:
    """
    3+ sentences ending in '?' -- a cheap, not perfect, proxy for visible
    Q&A content (FAQ blocks, People Also Ask style sections). Splits on
    sentence-ending punctuation (. ? ! or a newline) with a lookbehind, so
    three questions run together in one paragraph (one text node once HTML
    is extracted -- no newlines between them) are still counted separately,
    not merged into one long non-matching chunk.
    """
    sentences = re.split(r"(?<=[.?!])\s+|\n", text)
    question_sentences = [s for s in sentences if s.strip().endswith("?")]
    return len(question_sentences) >= 3


def _heading_structure(soup: BeautifulSoup) -> dict:
    return {level: len(soup.find_all(level)) for level in ("h1", "h2", "h3", "h4")}


def _has_author(soup: BeautifulSoup) -> bool:
    """A visible byline signal: a meta author tag, or a Person in JSON-LD."""
    if soup.find("meta", attrs={"name": "author"}):
        return True
    if soup.find(attrs={"rel": "author"}):
        return True
    import json as _json
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads((script.string or "").strip())
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            author = item.get("author")
            if author:
                return True
    return False


def _extract_meta_date(soup: BeautifulSoup, names: List[str]) -> Optional[str]:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"]
    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        return time_tag["datetime"]
    return None


def _extract_json_ld_date(soup: BeautifulSoup, key: str) -> Optional[str]:
    import json as _json
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads((script.string or "").strip())
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get(key):
                return item[key]
    return None


def _parse_date_loosely(value: Optional[str]) -> Optional[datetime]:
    """
    Found live on a real ing.ro page: `<meta property="article:modified_time"
    content=2025-04-30T10:38:46.750+03:00/>` -- an unquoted attribute value,
    so BeautifulSoup's parser has no delimiter to stop at and folds the
    tag's own self-closing "/" into the attribute value. Not a made-up edge
    case; stripping a trailing "/" (and surrounding whitespace) before
    parsing handles it without needing to special-case every markup quirk a
    real page might have.
    """
    if not value:
        return None
    value = value.strip().rstrip("/").strip()
    try:
        # Most published/modified dates are ISO-8601 already; fromisoformat
        # handles the common "Z" suffix in 3.11+ but not always -- normalise it.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_since(value: Optional[str]) -> Optional[int]:
    parsed = _parse_date_loosely(value)
    if not parsed:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).days


def _direct_answer_in_first_words(text: str, query: str, window_words: int = 100) -> bool:
    """
    Whether the query's own significant words (>=4 characters, to skip
    stopword-sized tokens like "de", "cu", "din") appear in the first
    `window_words` words of the main content -- a cheap proxy for "answers
    the question immediately" rather than burying it.
    """
    if not query:
        return False
    first_words = " ".join(text.split()[:window_words]).lower()
    query_terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 4]
    if not query_terms:
        return False
    hits = sum(1 for t in query_terms if t in first_words)
    return hits / len(query_terms) >= 0.5


def extract_features(html: str, query: Optional[str] = None) -> dict:
    """
    All deterministic features for one page. `query` is optional -- only
    used for direct_answer_in_first_words, which needs it; every other
    feature applies to any page regardless of which query it's being
    compared for.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = extract_content(html)

    published = _extract_meta_date(soup, _DATE_META_NAMES["published"]) or _extract_json_ld_date(soup, "datePublished")
    modified = _extract_meta_date(soup, _DATE_META_NAMES["modified"]) or _extract_json_ld_date(soup, "dateModified")

    features = {
        "word_count": len(text.split()),
        "has_tables": bool(soup.find("table")),
        "has_lists": bool(soup.find(["ul", "ol"])),
        "has_qa_content": _looks_like_qa_content(text),
        "numeric_density_per_100_words": _numeric_density_per_100_words(text),
        "json_ld_types": extract_structured_data_types(html),
        "has_author": _has_author(soup),
        "heading_structure": _heading_structure(soup),
        "published_date": published,
        "updated_date": modified,
        "days_since_updated": _days_since(modified or published),
    }
    if query is not None:
        features["direct_answer_in_first_words"] = _direct_answer_in_first_words(text, query)
    return features
