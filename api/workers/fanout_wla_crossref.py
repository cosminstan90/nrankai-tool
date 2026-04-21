"""
WLA Fan-Out Cross-Reference Analyzer (Prompt 18)
=================================================
Compares fan-out queries from a fan-out session against the crawled pages
of a WLA site audit to identify coverage gaps and quick-win opportunities.

Algorithm:
  - Extract fan-out queries for the session.
  - Load audit result pages (page_url, title, meta_description, H1 from result_json).
  - Tokenise both; compute overlap score.
  - Classify each query:
      COVERED  : overlap ≥ 0.6 with title / H1 / URL
      PARTIAL  : overlap ≥ 0.3 with any field
      GAP      : overlap < 0.3
  - Build CrossRefResult with coverage_score, action_cards, quick_wins.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.audit import AuditResult
from api.models.database import FanoutSession, FanoutQuery

logger = logging.getLogger("fanout_wla_crossref")


# ── Stopwords ─────────────────────────────────────────────────────────────────

STOP_WORDS: set[str] = {
    "the", "a", "an", "and", "or", "for", "in", "on", "at", "to", "is", "are",
    "de", "la", "în", "și", "sau", "pe", "cu", "că", "o", "un", "cel", "cea",
    "was", "be", "with", "from", "this", "that", "it", "its", "by", "of",
    "how", "what", "which", "who", "where", "when", "best", "top", "good",
}


def _tokenize(text: str) -> set[str]:
    """Lower-case, strip punctuation, remove stop-words."""
    tokens = re.findall(r"[a-zA-ZÀ-ÿ0-9]+", text.lower())
    return {t for t in tokens if t not in STOP_WORDS and len(t) > 2}


def _overlap(query_tokens: set[str], text: str) -> float:
    if not query_tokens or not text:
        return 0.0
    page_tokens = _tokenize(text)
    if not page_tokens:
        return 0.0
    return len(query_tokens & page_tokens) / len(query_tokens)


def _extract_page_fields(result_json_str: Optional[str]) -> dict:
    """Pull title, meta_description, h1 from the audit result JSON blob."""
    out = {"title": "", "meta_description": "", "h1": ""}
    if not result_json_str:
        return out
    try:
        data = json.loads(result_json_str)
    except Exception:
        return out
    # Support both flat and nested result formats
    out["title"]            = data.get("title", "") or data.get("page_title", "") or ""
    out["meta_description"] = data.get("meta_description", "") or data.get("description", "") or ""
    h1s = data.get("h1", []) or data.get("headings", {}).get("h1", [])
    if isinstance(h1s, list):
        out["h1"] = " ".join(h1s)
    elif isinstance(h1s, str):
        out["h1"] = h1s
    return out


# ── Priority scoring ──────────────────────────────────────────────────────────

def _priority(query: str, session_count: int, competitor: Optional[str]) -> str:
    if session_count > 3 or competitor:
        return "high"
    if session_count == 2:
        return "medium"
    return "low"


def _suggested_content_type(query: str) -> str:
    q = query.lower()
    if any(w in q for w in ["price", "cost", "tarif", "pret", "pricing"]):
        return "pricing_page"
    if any(w in q for w in ["vs", "versus", "compare", "alternative"]):
        return "comparison_page"
    if any(w in q for w in ["how", "what", "why", "guide", "tutorial", "cum"]):
        return "blog_post"
    if any(w in q for w in ["near", "local", "city", "oras", "bucuresti", "cluj"]):
        return "location_page"
    return "service_page"


# ── Main analyze function ─────────────────────────────────────────────────────

async def analyze(
    fanout_session_id: str,
    wla_audit_id: str,
    target_domain: str,
    db: AsyncSession,
) -> dict:
    """
    Cross-reference fan-out queries with WLA audit pages.

    Returns a CrossRefResult dict:
    {
      coverage_score, retrieval_readiness_score,
      covered_queries, partial_queries,
      gap_queries: [{query, status, suggested_content_type, priority, competitor_who_covers}],
      quick_wins,
      action_cards: [{type, priority, title, rationale, queries_covered, estimated_impact}]
    }
    """
    # 1. Fan-out queries
    q_stmt = select(FanoutQuery).where(FanoutQuery.session_id == fanout_session_id)
    queries_rows = (await db.execute(q_stmt)).scalars().all()
    queries = [q.query_text for q in queries_rows]

    if not queries:
        return {
            "coverage_score": 0.0, "retrieval_readiness_score": 0.0,
            "covered_queries": [], "partial_queries": [], "gap_queries": [],
            "quick_wins": [], "action_cards": [],
            "error": "No fan-out queries found for session",
        }

    # 2. Audit pages
    ar_stmt = select(AuditResult).where(AuditResult.audit_id == wla_audit_id)
    audit_pages = (await db.execute(ar_stmt)).scalars().all()

    pages = []
    for ap in audit_pages:
        fields = _extract_page_fields(ap.result_json)
        pages.append({
            "url":              ap.page_url,
            "title":            fields["title"],
            "meta_description": fields["meta_description"],
            "h1":               fields["h1"],
        })

    # 3. Classify each query
    covered_queries: list  = []
    partial_queries: list  = []
    gap_queries: list      = []

    for query in queries:
        q_tokens = _tokenize(query)
        best_status = "gap"
        best_page   = None
        best_overlap = 0.0

        for page in pages:
            # Check title+H1+URL for covered threshold
            ov_title = _overlap(q_tokens, page["title"])
            ov_h1    = _overlap(q_tokens, page["h1"])
            ov_url   = _overlap(q_tokens, page["url"])
            ov_meta  = _overlap(q_tokens, page["meta_description"])

            high_ov = max(ov_title, ov_h1, ov_url)
            any_ov  = max(high_ov, ov_meta)

            if high_ov >= 0.6:
                if any_ov > best_overlap:
                    best_status, best_page, best_overlap = "covered", page, any_ov
                break
            elif any_ov >= 0.3 and best_status != "covered":
                if any_ov > best_overlap:
                    best_status, best_page, best_overlap = "partial", page, any_ov

        if best_status == "covered":
            covered_queries.append({"query": query, "page_url": best_page["url"] if best_page else None})
        elif best_status == "partial":
            partial_queries.append({"query": query, "page_url": best_page["url"] if best_page else None, "overlap": round(best_overlap, 2)})
        else:
            gap_queries.append({
                "query":                query,
                "status":              "gap",
                "suggested_content_type": _suggested_content_type(query),
                "priority":            _priority(query, 1, None),
                "competitor_who_covers": None,
            })

    total = len(queries)
    coverage_score            = len(covered_queries) / total if total else 0.0
    retrieval_readiness_score = (len(covered_queries) + 0.5 * len(partial_queries)) / total if total else 0.0

    # 4. Quick wins: partial queries with high overlap (close to covered)
    quick_wins = [p["query"] for p in partial_queries if p.get("overlap", 0) >= 0.45]

    # 5. Action cards
    action_cards = []

    if gap_queries:
        high_gaps   = [g for g in gap_queries if g["priority"] == "high"]
        create_type = "create_content"
        action_cards.append({
            "type":             create_type,
            "priority":         "high" if high_gaps else "medium",
            "title":            f"Create content for {len(gap_queries)} uncovered queries",
            "rationale":        "These queries are fired by AI engines but no matching page exists on your site.",
            "queries_covered":  [g["query"] for g in gap_queries[:5]],
            "estimated_impact": "high" if len(gap_queries) > 3 else "medium",
        })

    if partial_queries:
        action_cards.append({
            "type":             "optimize_content",
            "priority":         "medium",
            "title":            f"Optimize {len(partial_queries)} partially-matching pages for AI retrieval",
            "rationale":        "These pages partially match fan-out queries. Adding the query terms to H1 / meta can elevate them to full coverage.",
            "queries_covered":  [p["query"] for p in partial_queries[:5]],
            "estimated_impact": "medium",
        })

    if quick_wins:
        action_cards.append({
            "type":             "quick_win",
            "priority":         "high",
            "title":            f"{len(quick_wins)} quick-win optimizations (close to covered)",
            "rationale":        "These pages already have strong token overlap. Minor on-page edits could push them to full coverage.",
            "queries_covered":  quick_wins[:5],
            "estimated_impact": "high",
        })

    return {
        "coverage_score":            round(coverage_score, 3),
        "retrieval_readiness_score": round(retrieval_readiness_score, 3),
        "total_queries":             total,
        "total_pages_checked":       len(pages),
        "covered_queries":           covered_queries,
        "partial_queries":           partial_queries,
        "gap_queries":               gap_queries,
        "quick_wins":                quick_wins,
        "action_cards":              action_cards,
    }
