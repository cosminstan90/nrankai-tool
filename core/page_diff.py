"""
Compares two page snapshots and reports what changed, in SEO terms
(Etapa 6 of docs/IMPROVEMENTS_PLAN.md).

The plan is explicit that this must not be a raw text diff. The question it
answers is the one that actually comes up on client work -- "they changed the
page, what did that break?" -- so the output is a list of findings, each with a
severity, rather than a wall of markup.

The one rule that shapes everything: None means "not captured", never "absent".
The scraper stores document.body only, so title/meta/canonical are None on
every page today. Treating None as "removed" or "added" would flag every page
in the site at once and bury the real changes -- so any comparison involving
None on either side is skipped.
"""

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# A content or link loss is a real regression; gaining a heading rarely is.
# Severity exists so a person can triage a hundred changed pages, so it has to
# actually separate them rather than label everything "medium".
_WORD_COUNT_DROP_THRESHOLD = 0.10   # 10% of the previous length


def _changed(before, after) -> bool:
    """True only when both sides are known and differ -- see the module docstring."""
    if before is None or after is None:
        return False
    return before != after


def _change(kind: str, severity: str, before, after, note: str = "") -> dict:
    out = {"kind": kind, "severity": severity, "before": before, "after": after}
    if note:
        out["note"] = note
    return out


def diff_snapshots(before: dict, after: dict) -> List[dict]:
    """
    Returns the SEO-meaningful differences, most severe first.

    Both arguments are dicts as produced by core.page_snapshot.extract_page_fields.
    """
    changes: List[dict] = []

    # ── head metadata (present only once head capture exists) ────────────────
    for field, kind in (("title", "title_changed"),
                        ("meta_description", "meta_description_changed"),
                        ("canonical", "canonical_changed")):
        if _changed(before.get(field), after.get(field)):
            changes.append(_change(kind, "high", before[field], after[field]))

    # ── headings ─────────────────────────────────────────────────────────────
    if _changed(before.get("h1"), after.get("h1")):
        changes.append(_change("h1_changed", "high", before["h1"], after["h1"],
                               "the H1 is the page's strongest on-page signal"))

    for level in ("h2", "h3"):
        old = set(before.get(level) or [])
        new = set(after.get(level) or [])
        removed, added = sorted(old - new), sorted(new - old)
        if removed:
            changes.append(_change(f"{level}_removed", "medium", removed, [],
                                   "sections that disappeared can no longer be surfaced as passages"))
        if added:
            changes.append(_change(f"{level}_added", "low", [], added))

    # ── internal links ───────────────────────────────────────────────────────
    old_links = set(before.get("internal_links") or [])
    new_links = set(after.get("internal_links") or [])
    removed_links, added_links = sorted(old_links - new_links), sorted(new_links - old_links)
    if removed_links:
        changes.append(_change("internal_links_removed", "high", removed_links, [],
                               "removed internal links stop passing link equity to those pages"))
    if added_links:
        changes.append(_change("internal_links_added", "low", [], added_links))

    # ── content volume ───────────────────────────────────────────────────────
    old_words = before.get("word_count")
    new_words = after.get("word_count")
    if old_words and new_words is not None:
        delta = old_words - new_words
        if delta > 0 and delta >= old_words * _WORD_COUNT_DROP_THRESHOLD:
            changes.append(_change("word_count_dropped", "high", old_words, new_words,
                                   f"{delta} words removed ({delta / old_words:.0%} of the page)"))
        elif new_words > old_words and (new_words - old_words) >= old_words * _WORD_COUNT_DROP_THRESHOLD:
            changes.append(_change("word_count_grew", "low", old_words, new_words))

    # ── images ───────────────────────────────────────────────────────────────
    old_missing = before.get("images_without_alt")
    new_missing = after.get("images_without_alt")
    if old_missing is not None and new_missing is not None and new_missing > old_missing:
        changes.append(_change("images_without_alt_increased", "medium", old_missing, new_missing,
                               "images lost their alt text"))

    # ── structured data ──────────────────────────────────────────────────────
    old_schema = set(before.get("schema_types") or [])
    new_schema = set(after.get("schema_types") or [])
    lost, gained = sorted(old_schema - new_schema), sorted(new_schema - old_schema)
    if lost:
        changes.append(_change("schema_removed", "high", lost, [],
                               "losing schema can lose rich results"))
    if gained:
        changes.append(_change("schema_added", "low", [], gained))

    order = {"high": 0, "medium": 1, "low": 2}
    changes.sort(key=lambda c: order[c["severity"]])
    return changes


def summarize(changes: List[dict]) -> dict:
    """Counts by severity, for listing many pages without rendering every change."""
    return {
        "total": len(changes),
        "high": sum(1 for c in changes if c["severity"] == "high"),
        "medium": sum(1 for c in changes if c["severity"] == "medium"),
        "low": sum(1 for c in changes if c["severity"] == "low"),
    }
