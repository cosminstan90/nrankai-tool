"""
Passage chunking -- Pasul 17 of docs/superpowers/plans/2026-09-30-next-steps.md.

Splits a page's HTML into ~150-300 word passages for embedding-based
similarity matching (Fan-Out sub-query coverage; reusable anywhere else a
page needs to be compared section-by-section rather than as one blob).
Breaks at heading boundaries where the markup has them, so a passage
roughly corresponds to one section instead of an arbitrary word-count
slice; falls back to plain word-count chunking for a page with no heading
structure at all.

Reuses core/html2llm_converter.py's extract_content for the actual text --
found live, against a real ing.ro page, that walking specific tags
(<p>/<li>) directly misses real content on sites that lay out paragraphs in
plain <div>s instead of semantic <p> tags (common on JS-framework-built
pages): a real page with 7 <p> tags and 6 headings produced a single
11-word "passage", because none of the real body text was actually inside
a <p>. extract_content()'s own whole-body get_text() already handles
whatever tag convention a site uses; this only overlays heading-boundary
detection on top of it, by matching each extracted line's text against
what the page's own heading tags contain.
"""
from typing import List

from bs4 import BeautifulSoup

from core.html2llm_converter import extract_content

MIN_PASSAGE_WORDS = 150
MAX_PASSAGE_WORDS = 300
_HEADING_TAGS = ("h1", "h2", "h3", "h4")


def chunk_html_into_passages(html: str) -> List[str]:
    """
    Returns passage texts, each within [MIN_PASSAGE_WORDS, MAX_PASSAGE_WORDS]
    where the page has enough content -- the last passage of a page may be
    shorter. A heading only starts a fresh passage once the current one
    already has at least half of MIN_PASSAGE_WORDS, so a heading
    immediately followed by another heading (no real content yet) doesn't
    fragment things into tiny, useless passages.
    """
    soup = BeautifulSoup(html, "html.parser")
    heading_texts = set()
    for tag in soup.find_all(_HEADING_TAGS):
        text = tag.get_text(separator=" ", strip=True)
        if text:
            heading_texts.add(text)

    full_text = extract_content(html)
    lines = [line.strip() for line in full_text.split("\n") if line.strip()]
    if not lines:
        return []

    passages: List[str] = []
    current: List[str] = []

    def _flush():
        if current:
            passages.append(" ".join(current))

    for line in lines:
        is_heading = line in heading_texts
        if is_heading and len(current) >= MIN_PASSAGE_WORDS // 2:
            _flush()
            current.clear()
        current.extend(line.split())
        while len(current) >= MAX_PASSAGE_WORDS:
            passages.append(" ".join(current[:MAX_PASSAGE_WORDS]))
            current[:] = current[MAX_PASSAGE_WORDS:]
    _flush()

    return passages
