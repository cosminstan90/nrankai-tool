"""
Shared classification for "does this AI fan-out query already have real-world
search presence for the target domain" -- used by both the GSC crossref
(historical Search Console data, api/workers/gsc_fanout_crossref.py) and the
Serper.dev SERP-validate endpoint (live rank check, api/routes/fanout.py).

Both call sites used to independently implement a 4-way ai_found/serp_found
matrix (synced/ai_gap/ai_only/double_gap), with ai_found hardcoded to True
"by definition -- these are fan-out queries". That made ai_gap and
double_gap structurally unreachable in both places: no per-query "the AI
actually cited my brand for this query" signal exists anywhere in the data
model (FanoutSource, the citation table, is session-level, not linked to
individual FanoutQuery rows). This reduces the classification to what the
data actually supports: does the target domain rank for this query in real
search right now, or not.
"""
from typing import List


def classify_serp_presence(entries: List[dict]) -> dict:
    """
    entries: list of dicts, each already containing at least
        {"query": str, "serp_found": bool, ...any caller-specific fields}

    Returns {"total_queries", "synced", "gap", "summary": {"synced_count", "gap_count"}}
    """
    synced = [e for e in entries if e.get("serp_found")]
    gap    = [e for e in entries if not e.get("serp_found")]
    return {
        "total_queries": len(entries),
        "synced":        synced,
        "gap":           gap,
        "summary": {
            "synced_count": len(synced),
            "gap_count":    len(gap),
        },
    }
