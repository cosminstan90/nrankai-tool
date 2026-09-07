"""
Parses Screaming Frog CSV exports into plain dicts (Etapa 5 of
docs/IMPROVEMENTS_PLAN.md).

Every column name here was read off a real export, not guessed. All SF CSVs
are UTF-8 WITH a BOM, so they must be opened with encoding="utf-8-sig" -- with
plain "utf-8" the first header parses as "﻿Address", every row's url comes
back None, and the whole export silently yields nothing instead of erroring.

Pure functions over file paths: no database, no network, so the whole module is
testable against the committed fixtures in tests/fixtures/sf.
"""

import csv
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ENCODING = "utf-8-sig"

CONTENT_POSITION = "Content"


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return None


def parse_internal_html(path) -> List[dict]:
    """
    One dict per crawled HTML page: url, status, depth and link counts.

    Returns [] when the file is absent -- SF is invoked with --skip-empty, so
    a missing export legitimately means "nothing matched", not a failure.
    """
    path = Path(path)
    if not path.is_file():
        logger.warning("internal_html export missing at %s", path)
        return []

    pages: List[dict] = []
    with open(path, encoding=_ENCODING, newline="") as fh:
        for row in csv.DictReader(fh):
            url = (row.get("Address") or "").strip()
            if not url:
                continue
            pages.append({
                "url": url,
                "status_code": _int(row.get("Status Code")),
                "indexability": (row.get("Indexability") or "").strip() or None,
                "crawl_depth": _int(row.get("Crawl Depth")),
                "inlinks_total": _int(row.get("Inlinks")) or 0,
                "unique_inlinks": _int(row.get("Unique Inlinks")) or 0,
                "outlinks_total": _int(row.get("Outlinks")) or 0,
                "unique_outlinks": _int(row.get("Unique Outlinks")) or 0,
            })
    return pages


def _edge_reason(link_position: str, dest_status: Optional[int]) -> Optional[str]:
    """
    Decide whether an edge is worth a database row. None means "count it, do
    not store it".

    Measured on a real crawl: of 174,244 hyperlink edges, 152,277 were
    Navigation and only 157 were Content. Persisting navigation edges would
    store one copy of the site's menu per page for no analytical gain -- and
    prompts/internal_linking.yaml explicitly discounts them ("Navigation and
    footer links do NOT count as quality internal links"). They survive as
    aggregate counts instead.

    Broken and redirecting destinations are kept whatever their position: a 404
    linked only from the navigation is still a real bug, and it is a small set.
    """
    if dest_status is not None and dest_status >= 400:
        return "error"
    if dest_status is not None and 300 <= dest_status < 400:
        return "redirect"
    if link_position == CONTENT_POSITION:
        return "content"
    return None


def parse_inlinks(path) -> Tuple[List[dict], Dict[str, dict]]:
    """
    Returns (edges_to_store, per_destination_counts).

    edges_to_store holds only content, error and redirect hyperlinks.
    per_destination_counts holds {url: {"content": n, "non_content": n}} for
    every hyperlink seen, so navigation volume is still countable after the
    rows themselves are discarded.
    """
    path = Path(path)
    if not path.is_file():
        logger.warning("all_inlinks export missing at %s", path)
        return [], {}

    edges: List[dict] = []
    counts: Dict[str, dict] = defaultdict(lambda: {"content": 0, "non_content": 0})

    with open(path, encoding=_ENCODING, newline="") as fh:
        for row in csv.DictReader(fh):
            # Only real anchor links. SF also reports JavaScript, CSS, Image
            # and Iframe relationships in this export; none of them are
            # internal links in the sense the prompt scores on.
            if (row.get("Type") or "").strip() != "Hyperlink":
                continue

            source = (row.get("Source") or "").strip()
            dest = (row.get("Destination") or "").strip()
            if not source or not dest:
                continue

            position = (row.get("Link Position") or "").strip()
            dest_status = _int(row.get("Status Code"))

            if position == CONTENT_POSITION:
                counts[dest]["content"] += 1
            else:
                counts[dest]["non_content"] += 1

            reason = _edge_reason(position, dest_status)
            if reason is None:
                continue

            edges.append({
                "source_url": source,
                "dest_url": dest,
                "anchor": (row.get("Anchor") or "").strip() or None,
                "link_position": position or None,
                "follow": (row.get("Follow") or "").strip().lower() == "true",
                "dest_status_code": dest_status,
                "reason": reason,
            })

    return edges, dict(counts)
