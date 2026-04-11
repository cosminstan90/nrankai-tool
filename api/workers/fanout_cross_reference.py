"""
Fan-Out Cross-Reference Analysis.

Cross-references fan-out analysis results with other GEO Tool modules:
  - Citation Tracker  — overlap between fan-out sources and citation-tracked URLs
  - GEO Monitor       — overlap between fan-out queries and monitoring queries
  - Content Gaps      — queries where the target site has no content

Produces three analysis blocks:
  1. citations_overlap   : sources that appear in both fan-out AND citation tracking
  2. content_gaps        : fan-out queries the target site likely lacks content for
  3. retrieval_coverage  : how much of the AI retrieval surface the target domain covers
"""

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.models.database import (
    FanoutSession, FanoutQuery, FanoutSource,
    CitationTracker, CitationScan,
    GeoMonitorProject, GeoMonitorScan,
    ContentGap,
)

logger = logging.getLogger("fanout.cross_reference")


# ============================================================================
# Helpers
# ============================================================================

def _domain_of(url: str) -> str:
    """Extract bare domain (no www.) from URL string."""
    try:
        return urlparse(url if "://" in url else f"https://{url}").netloc.lstrip("www.")
    except Exception:
        return url.lower().replace("www.", "")


def _domains_match(a: str, b: str) -> bool:
    """Loose domain comparison: 'ing.ro' matches 'www.ing.ro' or 'ing.ro/path'."""
    a = _domain_of(a)
    b = _domain_of(b)
    return a == b or a.endswith(f".{b}") or b.endswith(f".{a}")


# ============================================================================
# 1. Citations overlap
# ============================================================================

async def cross_reference_with_citations(
    session_id: str,
    db: AsyncSession,
) -> Optional[Dict[str, Any]]:
    """
    Compare fan-out sources against Citation Tracker data.

    Looks for a CitationTracker whose website domain matches the
    fan-out session's target_url, then compares:
      - cited_and_in_fanout : URLs in both citation tracking AND fan-out sources
      - in_fanout_not_cited : fan-out sources the target doesn't appear alongside
      - cited_not_in_fanout : citation-tracked URLs absent from fan-out sources
      - overlap_score       : Jaccard-like overlap percentage
    """
    # Load fan-out session with sources
    stmt = (
        select(FanoutSession)
        .where(FanoutSession.id == session_id)
        .options(selectinload(FanoutSession.sources))
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session or not session.target_url:
        return None

    target_domain = _domain_of(session.target_url)

    # Find a citation tracker for the same domain
    trackers = (
        await db.execute(select(CitationTracker))
    ).scalars().all()

    matched_tracker = None
    for t in trackers:
        if _domains_match(t.website, target_domain):
            matched_tracker = t
            break

    if not matched_tracker:
        logger.info("No citation tracker found for domain %s", target_domain)
        return None

    # Get the latest completed scan
    scan_stmt = (
        select(CitationScan)
        .where(
            CitationScan.tracker_id == matched_tracker.id,
            CitationScan.status == "completed",
        )
        .order_by(desc(CitationScan.created_at))
        .limit(1)
    )
    scan = (await db.execute(scan_stmt)).scalar_one_or_none()
    if not scan or not scan.top_cited_urls:
        return None

    # Parse citation URLs from scan
    try:
        cited_urls_raw = json.loads(scan.top_cited_urls)
    except (json.JSONDecodeError, TypeError):
        cited_urls_raw = []

    cited_domains = set()
    for entry in cited_urls_raw:
        url = entry.get("url", "") if isinstance(entry, dict) else str(entry)
        d = _domain_of(url)
        if d:
            cited_domains.add(d)

    # Fan-out source domains
    fanout_domains = {s.domain for s in session.sources if s.domain}

    # Compute sets
    overlap    = cited_domains & fanout_domains
    only_fanout = fanout_domains - cited_domains
    only_cited  = cited_domains - fanout_domains
    union      = cited_domains | fanout_domains

    overlap_score = round(len(overlap) / max(len(union), 1) * 100, 1)

    return {
        "citation_tracker_id":   matched_tracker.id,
        "citation_tracker_name": matched_tracker.name,
        "cited_and_in_fanout":   sorted(overlap),
        "in_fanout_not_cited":   sorted(only_fanout),
        "cited_not_in_fanout":   sorted(only_cited),
        "overlap_score":         overlap_score,
        "total_cited_domains":   len(cited_domains),
        "total_fanout_domains":  len(fanout_domains),
    }


# ============================================================================
# 2. Content gaps from fan-out
# ============================================================================

def _classify_query_type(query: str) -> str:
    """Heuristic classification of content type from a search query."""
    q = query.lower()
    if any(w in q for w in ["best", "top", "cele mai bune", "ranking", "compare"]):
        return "listicle"
    if any(w in q for w in ["how to", "cum sa", "ghid", "guide", "tutorial"]):
        return "how-to guide"
    if any(w in q for w in ["review", "recenzie", "pareri", "opinie"]):
        return "review"
    if any(w in q for w in ["vs", "versus", "comparatie", "comparison"]):
        return "comparison"
    if any(w in q for w in ["price", "cost", "pret", "tarif"]):
        return "pricing page"
    if any(w in q for w in ["alternative", "alternativa"]):
        return "alternatives page"
    return "informational"


async def generate_content_gaps_from_fanout(
    session_id: str,
    db: AsyncSession,
) -> Optional[List[Dict[str, Any]]]:
    """
    Identify content gaps based on fan-out queries.

    For each fan-out query, checks whether the target domain appears in
    the session's sources. Queries where the target is absent are
    classified as content gaps with priority based on query position
    (earlier = higher priority, since LLMs tend to search most important
    queries first).

    Also looks for matching ContentGap records to avoid re-flagging
    topics already in the content pipeline.
    """
    stmt = (
        select(FanoutSession)
        .where(FanoutSession.id == session_id)
        .options(
            selectinload(FanoutSession.queries),
            selectinload(FanoutSession.sources),
        )
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session or not session.target_url:
        return None

    target_domain = _domain_of(session.target_url)
    target_source_domains = {
        s.domain for s in session.sources
        if s.domain and _domains_match(s.domain, target_domain)
    }

    # Domains that ARE in sources (competitors)
    all_source_domains = {}
    for s in session.sources:
        if s.domain and s.domain != target_domain:
            all_source_domains[s.domain] = all_source_domains.get(s.domain, 0) + 1

    # Load existing content gaps for this website to check for duplicates
    existing_gaps_stmt = (
        select(ContentGap)
        .where(ContentGap.website.ilike(f"%{target_domain}%"))
    )
    existing_gaps = (await db.execute(existing_gaps_stmt)).scalars().all()
    existing_topics = {g.topic.lower() for g in existing_gaps}

    # Build gap list
    gaps = []
    total_queries = len(session.queries or [])

    for q in (session.queries or []):
        position = q.query_position or 999

        # Check if target appears in sources for this query
        # (Since we don't have per-query source mapping, we check if target
        # appears in ANY source. A more precise version would need per-query
        # source tracking.)
        has_content = bool(target_source_domains)

        # Priority based on position: top 5 = high, 6-15 = medium, rest = low
        if position <= 5:
            priority = "high"
        elif position <= 15:
            priority = "medium"
        else:
            priority = "low"

        # Skip if target IS found in sources (optimistic: they have coverage)
        if has_content:
            continue

        # Check if already in content pipeline
        already_tracked = any(
            topic in q.query_text.lower()
            for topic in existing_topics
        )

        # Top competitors for this query's topic
        competing_domains = sorted(
            all_source_domains.items(), key=lambda x: x[1], reverse=True
        )[:3]

        gaps.append({
            "fanout_query":         q.query_text,
            "query_position":       position,
            "has_content":          False,
            "already_in_pipeline":  already_tracked,
            "suggested_content_type": _classify_query_type(q.query_text),
            "priority":             priority,
            "competing_domains":    [
                {"domain": d, "appearances": c} for d, c in competing_domains
            ],
        })

    return gaps


# ============================================================================
# 3. Retrieval coverage score
# ============================================================================

async def calculate_retrieval_coverage(
    session_id: str,
    db: AsyncSession,
) -> Optional[Dict[str, Any]]:
    """
    Calculate a Retrieval Coverage Score for the target domain.

    The score measures how visible the target domain is in the AI's
    "retrieval surface" — the set of sources the LLM cites.

    Returns:
        domain, coverage_pct, competing domains ranked by appearance count,
        and an improvement_potential classification.
    """
    stmt = (
        select(FanoutSession)
        .where(FanoutSession.id == session_id)
        .options(
            selectinload(FanoutSession.queries),
            selectinload(FanoutSession.sources),
        )
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session or not session.target_url:
        return None

    target_domain = _domain_of(session.target_url)
    sources = session.sources or []
    queries = session.queries or []

    # Count appearances per domain
    domain_counts: Dict[str, int] = {}
    target_appearances = 0
    for s in sources:
        d = s.domain or ""
        if not d:
            continue
        domain_counts[d] = domain_counts.get(d, 0) + 1
        if _domains_match(d, target_domain):
            target_appearances += 1

    total_sources = len(sources)
    coverage_pct = round(target_appearances / max(total_sources, 1) * 100, 1)

    # Improvement classification
    if coverage_pct < 5:
        improvement = "critical"
    elif coverage_pct < 20:
        improvement = "high"
    elif coverage_pct < 50:
        improvement = "medium"
    else:
        improvement = "low"

    # Top competing domains (exclude target)
    competitors = sorted(
        [
            {"domain": d, "appearances": c,
             "coverage_pct": round(c / max(total_sources, 1) * 100, 1)}
            for d, c in domain_counts.items()
            if not _domains_match(d, target_domain)
        ],
        key=lambda x: x["appearances"],
        reverse=True,
    )[:10]

    # Cross-reference with GEO Monitor if available
    geo_monitor_data = await _get_geo_monitor_overlap(target_domain, queries, db)

    return {
        "domain":                target_domain,
        "total_fanout_queries":  len(queries),
        "total_sources":         total_sources,
        "target_appearances":    target_appearances,
        "retrieval_coverage_pct": coverage_pct,
        "improvement_potential":  improvement,
        "top_competing_domains":  competitors,
        "geo_monitor_overlap":    geo_monitor_data,
    }


async def _get_geo_monitor_overlap(
    target_domain: str,
    fanout_queries: list,
    db: AsyncSession,
) -> Optional[Dict[str, Any]]:
    """
    Check if any GEO Monitor project tracks the same domain and compute
    query overlap between the monitor's tracking queries and the fan-out
    queries.
    """
    projects = (await db.execute(select(GeoMonitorProject))).scalars().all()
    matched_project = None
    for p in projects:
        if _domains_match(p.website, target_domain):
            matched_project = p
            break

    if not matched_project:
        return None

    try:
        monitor_queries = set(
            q.lower() for q in json.loads(matched_project.target_queries)
        )
    except (json.JSONDecodeError, TypeError):
        return None

    fanout_query_texts = set(q.query_text.lower() for q in fanout_queries)

    # Exact match overlap
    exact_overlap = monitor_queries & fanout_query_texts

    # Fuzzy overlap: check if a fanout query CONTAINS a monitor query or vice versa
    fuzzy_only = set()
    for fq in fanout_query_texts - exact_overlap:
        for mq in monitor_queries:
            if mq in fq or fq in mq:
                fuzzy_only.add(fq)
                break

    # Fan-out queries NOT in monitor = potential new tracking queries
    new_candidates = fanout_query_texts - exact_overlap - fuzzy_only

    return {
        "project_id":        matched_project.id,
        "project_name":      matched_project.name,
        "monitor_queries":   len(monitor_queries),
        "exact_overlap":     len(exact_overlap),
        "fuzzy_overlap":     len(fuzzy_only),
        "new_query_candidates": sorted(new_candidates)[:20],  # top 20 suggestions
    }


# ============================================================================
# Action Cards generator
# ============================================================================

import uuid as _uuid


def generate_fanout_action_cards(
    retrieval_coverage: Optional[Dict[str, Any]],
    content_gaps: Optional[List[Dict[str, Any]]],
    citations_overlap: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Generate prioritised action cards from cross-reference analysis results.

    Rules (applied in priority order):
      A) coverage < 5%  → "Low Retrieval Coverage"  — critical
      B) coverage 5-20% → "Low Retrieval Coverage"  — high
      C) top competitor ≥ 50% of sources → "Competitor Dominance" — high
      D) target in sources but coverage < 50% → "Quick Win Queries" — medium
      E) high-priority content gaps exist → "Content Gaps"  — medium
      F) citations_overlap: cited URLs absent from fan-out → "Citation Gap" — low

    All rules are evaluated independently so multiple cards can fire at once.
    """
    cards: List[Dict[str, Any]] = []

    # ── A/B: Low Retrieval Coverage ──────────────────────────────────────
    if retrieval_coverage is not None:
        cov_pct    = retrieval_coverage.get("retrieval_coverage_pct", 0.0)
        domain     = retrieval_coverage.get("domain", "your site")
        total_q    = retrieval_coverage.get("total_fanout_queries", 0)
        competitors = retrieval_coverage.get("top_competing_domains", [])

        if cov_pct < 5:
            priority = "critical"
            title    = "Critical: Nearly Invisible to AI"
            desc     = (
                f"{domain} appears in {cov_pct}% of AI-cited sources. "
                f"AI engines are almost never citing your site when users ask about this topic."
            )
        elif cov_pct < 20:
            priority = "high"
            title    = "Low AI Retrieval Coverage"
            desc     = (
                f"{domain} appears in only {cov_pct}% of AI-cited sources across "
                f"{total_q} fan-out queries. Significant visibility gap."
            )
        else:
            priority = None

        if priority:
            cards.append({
                "id":       str(_uuid.uuid4()),
                "type":     "fanout_coverage",
                "priority": priority,
                "title":    title,
                "description": desc,
                "action_items": [
                    f"Create authoritative content targeting the top {min(5, total_q)} fan-out queries",
                    "Build citations from domains that DO appear in AI sources",
                    "Add structured data (FAQ, HowTo) to help AI engines extract your content",
                ],
                "data": {
                    "coverage_pct":     cov_pct,
                    "total_queries":    total_q,
                    "top_competitors":  competitors[:3],
                },
            })

        # ── C: Competitor Dominance ───────────────────────────────────────
        if competitors:
            top = competitors[0]
            if top.get("coverage_pct", 0) >= 50:
                cards.append({
                    "id":       str(_uuid.uuid4()),
                    "type":     "competitor_dominance",
                    "priority": "high",
                    "title":    f"Competitor Dominance: {top['domain']}",
                    "description": (
                        f"{top['domain']} appears in {top['coverage_pct']}% of AI-cited sources "
                        f"({top['appearances']} appearances). They dominate the AI retrieval surface "
                        f"for this topic."
                    ),
                    "action_items": [
                        f"Analyse {top['domain']}'s content strategy for this topic",
                        "Create more comprehensive content than the competitor",
                        "Target the same fan-out queries with better depth and structure",
                    ],
                    "data": {
                        "dominant_domain":     top["domain"],
                        "dominant_coverage":   top["coverage_pct"],
                        "dominant_appearances": top["appearances"],
                        "all_competitors":     competitors,
                    },
                })

        # ── D: Quick Win Queries ──────────────────────────────────────────
        target_appearances = retrieval_coverage.get("target_appearances", 0)
        if 0 < target_appearances and cov_pct < 50:
            cards.append({
                "id":       str(_uuid.uuid4()),
                "type":     "quick_win",
                "priority": "medium",
                "title":    "Quick Win: Improve Existing Rankings",
                "description": (
                    f"{domain} already appears in {target_appearances} AI source(s) "
                    f"({cov_pct}% coverage). With targeted improvements you can increase "
                    f"how often AI cites you."
                ),
                "action_items": [
                    "Strengthen existing pages that are already being cited",
                    "Add more data, statistics, and expert quotes to cited content",
                    "Improve internal linking from cited pages to related topics",
                ],
                "data": {
                    "target_appearances": target_appearances,
                    "coverage_pct":       cov_pct,
                },
            })

    # ── E: High-priority content gaps ────────────────────────────────────
    if content_gaps:
        high_gaps  = [g for g in content_gaps if g.get("priority") == "high"]
        by_type: Dict[str, List[str]] = {}
        for g in high_gaps:
            ctype = g.get("suggested_content_type", "informational")
            by_type.setdefault(ctype, []).append(g["fanout_query"])

        if high_gaps:
            action_items = []
            for ctype, queries in list(by_type.items())[:4]:
                q_examples = ", ".join(f'"{q}"' for q in queries[:2])
                action_items.append(f"Create {ctype} content for: {q_examples}")

            cards.append({
                "id":       str(_uuid.uuid4()),
                "type":     "content_gap",
                "priority": "medium",
                "title":    f"{len(high_gaps)} High-Priority Content Gaps",
                "description": (
                    f"AI engines search for {len(high_gaps)} topics where your site has no "
                    f"matching content. These are the first queries AI fires — highest impact."
                ),
                "action_items": action_items or [
                    "Create content targeting the top fan-out queries",
                ],
                "data": {
                    "high_gap_count":  len(high_gaps),
                    "total_gap_count": len(content_gaps),
                    "top_gaps":        high_gaps[:5],
                    "by_content_type": {k: len(v) for k, v in by_type.items()},
                },
            })

    # ── F: Citation gap (cited URLs not in fan-out sources) ──────────────
    if citations_overlap:
        cited_not_fanout = citations_overlap.get("cited_not_in_fanout", [])
        overlap_score    = citations_overlap.get("overlap_score", 0)
        if cited_not_fanout and overlap_score < 40:
            cards.append({
                "id":       str(_uuid.uuid4()),
                "type":     "citation_gap",
                "priority": "low",
                "title":    "Citation Gap: Sources Not in AI Retrieval",
                "description": (
                    f"{len(cited_not_fanout)} domain(s) from your Citation Tracker appear in "
                    f"AI responses but NOT in the fan-out retrieval surface. These sites get "
                    f"cited by AI but aren't in the source pool for this topic."
                ),
                "action_items": [
                    f"Investigate why {cited_not_fanout[0]} is cited but not retrieved",
                    "Create content that targets the same queries as these sites",
                    "Consider link building from these citation-rich domains",
                ],
                "data": {
                    "overlap_score":     overlap_score,
                    "missed_domains":    cited_not_fanout[:10],
                },
            })

    # Sort: critical → high → medium → low
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    cards.sort(key=lambda c: priority_order.get(c["priority"], 9))

    return cards


# ============================================================================
# Combined analysis
# ============================================================================

async def full_cross_reference(
    session_id: str,
    db: AsyncSession,
) -> Dict[str, Any]:
    """
    Run all three cross-reference analyses and return a combined result.

    Gracefully returns null for any section whose prerequisites aren't met
    (e.g. no Citation Tracker configured for the target domain).
    """
    citations_overlap = None
    content_gaps = None
    retrieval_coverage = None

    try:
        citations_overlap = await cross_reference_with_citations(session_id, db)
    except Exception as exc:
        logger.warning("Citations cross-ref failed for %s: %s", session_id, exc)

    try:
        content_gaps = await generate_content_gaps_from_fanout(session_id, db)
    except Exception as exc:
        logger.warning("Content gaps failed for %s: %s", session_id, exc)

    try:
        retrieval_coverage = await calculate_retrieval_coverage(session_id, db)
    except Exception as exc:
        logger.warning("Retrieval coverage failed for %s: %s", session_id, exc)

    action_cards = generate_fanout_action_cards(
        retrieval_coverage=retrieval_coverage,
        content_gaps=content_gaps,
        citations_overlap=citations_overlap,
    )

    return {
        "session_id":          session_id,
        "citations_overlap":   citations_overlap,
        "content_gaps":        content_gaps,
        "retrieval_coverage":  retrieval_coverage,
        "action_cards":        action_cards,
    }
