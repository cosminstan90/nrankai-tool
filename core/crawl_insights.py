"""
Derives the findings prompts/internal_linking.yaml asks about from stored
crawl rows: anchor text quality, internal 404s with the pages that link to
them, and crawl depth distribution.

Etapa 5 of docs/IMPROVEMENTS_PLAN.md.

Pure functions over dicts so they are unit-testable without a database, and so
the same code works on freshly parsed rows or rows read back out of SQLite.
"""

from collections import Counter, defaultdict
from typing import Dict, List

# 401/403: the page exists, it is just protected. Reporting an auth-gated page
# as a broken link would be a false finding -- seen on the first real crawl,
# where the footer link to app.nrankai.com returned 401.
AUTH_STATUSES = {401, 403}

# prompts/internal_linking.yaml names these explicitly: "Generic anchors
# ('click here', 'here', 'read more', 'learn more') are flagged as MAJOR
# issues." Matched as whole normalised strings, never as substrings --
# "Here is our pricing guide" is descriptive, and reporting a real anchor as a
# major issue is a worse failure than missing a generic one.
GENERIC_ANCHORS = {
    "click here", "here", "read more", "learn more", "more",
    "this", "link", "this link", "see more", "details", "click",
    "continue", "go", "view", "view more",
}


def anchor_distribution(edges: List[dict]) -> Dict[str, object]:
    """
    Anchor quality across body-content edges only.

    Broken and redirecting edges are excluded: their anchor text is a separate
    concern from "are our internal links descriptive", and mixing them would
    inflate the generic count with links that are already reported elsewhere.
    """
    content = [e for e in edges if e.get("reason") == "content"]

    generic = empty = descriptive = 0
    texts: Counter = Counter()

    for edge in content:
        anchor = (edge.get("anchor") or "").strip()
        if not anchor:
            empty += 1
            continue
        texts[anchor] += 1
        if anchor.casefold() in GENERIC_ANCHORS:
            generic += 1
        else:
            descriptive += 1

    return {
        "total": len(content),
        "generic": generic,
        "empty": empty,
        "descriptive": descriptive,
        "most_common": texts.most_common(10),
    }


def broken_internal_links(edges: List[dict]) -> List[dict]:
    """
    Broken destinations, each with the pages that link to it.

    The source list is the part that makes this actionable -- SF's 4xx tab
    export reports only a count of inlinks, so the pages to fix come from the
    stored error edges. Redirects are excluded: a 301 is a finding, but not
    this finding. So are 401/403, which mean the page exists but is protected
    -- reporting an auth-gated page as a broken link is a false finding.
    """
    sources = defaultdict(list)
    statuses: Dict[str, int] = {}

    for edge in edges:
        status = edge.get("dest_status_code")
        if status is None or status < 400 or status in AUTH_STATUSES:
            continue
        dest = edge["dest_url"]
        sources[dest].append(edge["source_url"])
        statuses[dest] = status

    return [
        {"dest_url": dest, "status_code": statuses[dest], "linked_from": srcs}
        for dest, srcs in sources.items()
    ]


def depth_histogram(pages: List[dict]) -> Dict[int, int]:
    """
    How many pages sit at each click depth from the home page.

    Pages with an unknown depth are skipped rather than bucketed as 0 --
    counting them as depth 0 would report uncrawled pages as if they were the
    home page.
    """
    histogram: Counter = Counter()
    for page in pages:
        depth = page.get("crawl_depth")
        if depth is not None:
            histogram[depth] += 1
    return dict(histogram)
