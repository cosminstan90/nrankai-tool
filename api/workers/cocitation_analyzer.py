"""
Co-citation Map Analyzer (Prompt 34)
======================================
Identifies which domains appear alongside the target in AI engine responses.
$0 cost — analyzes existing session data from DB.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("cocitation_analyzer")

DIRECTORIES   = {"yelp.com", "tripadvisor.com", "angi.com", "thumbtack.com", "yellowpages.com", "houzz.com"}
REVIEW_SITES  = {"g2.com", "capterra.com", "trustpilot.com", "clutch.co", "sitejabber.com", "glassdoor.com"}


def _root_domain(url: str) -> str:
    """Extract root domain from URL."""
    try:
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        parts = parsed.netloc.lower().split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return parsed.netloc.lower()
    except Exception:
        return url.lower()


def classify_relationship(domain: str) -> str:
    d = _root_domain(domain)
    if d in DIRECTORIES:
        return "directory"
    if d in REVIEW_SITES:
        return "review_site"
    return "competitor"


async def build_cocitation_map(
    target_brand: str,
    target_domain: str,
    fanout_session_ids: Optional[List[str]],
    db,
    period_days: int = 30,
) -> dict:
    """
    Build a co-citation map for target_domain across fan-out sessions.
    If fanout_session_ids is None, fetches recent sessions for the brand.
    """
    from sqlalchemy import select
    from datetime import datetime, timezone, timedelta
    from api.models.database import FanoutSession, FanoutSource

    target_root = _root_domain(target_domain)

    # Load sessions
    if fanout_session_ids:
        stmt = select(FanoutSession).where(FanoutSession.id.in_(fanout_session_ids))
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=period_days)
        stmt = (
            select(FanoutSession)
            .where(FanoutSession.target_url.ilike(f"%{target_root}%"))
            .where(FanoutSession.created_at >= cutoff)
        )
    sessions = (await db.execute(stmt)).scalars().all()

    total_sessions = len(sessions)
    sessions_with_target = 0
    co_occurrences: dict[str, int] = defaultdict(int)
    co_contexts: dict[str, list] = defaultdict(list)

    for session in sessions:
        # Get sources for this session
        sources = (await db.execute(
            select(FanoutSource).where(FanoutSource.session_id == session.id)
        )).scalars().all()

        source_domains = {_root_domain(s.source_url) for s in sources if s.source_url}
        target_present = target_root in source_domains

        if target_present:
            sessions_with_target += 1
            others = source_domains - {target_root}
            for dom in others:
                co_occurrences[dom] += 1
                if len(co_contexts[dom]) < 3:
                    co_contexts[dom].append(session.prompt)

    # Build frequent co-citations (top 20)
    session_base = max(sessions_with_target, 1)
    frequent = []
    for dom, cnt in sorted(co_occurrences.items(), key=lambda x: -x[1])[:20]:
        rate = round(cnt / session_base, 3)
        frequent.append({
            "domain":            dom,
            "co_occurrences":    cnt,
            "co_occurrence_rate": rate,
            "contexts":          co_contexts[dom],
            "relationship":      classify_relationship(dom),
        })

    # Association gaps: directories/review sites that appear frequently but target doesn't
    missing_associations = []
    all_domains_seen = set(co_occurrences.keys())
    for dom in (DIRECTORIES | REVIEW_SITES):
        if dom not in all_domains_seen:
            missing_associations.append(dom)

    # Association gaps vs top co-cited domains
    # Compare rate of top competitor domains appearing without target
    association_gaps = []
    all_sources_any_session: dict[str, int] = defaultdict(int)
    for session in sessions:
        sources = (await db.execute(
            select(FanoutSource).where(FanoutSource.session_id == session.id)
        )).scalars().all()
        for s in sources:
            all_sources_any_session[_root_domain(s.source_url)] += 1

    for dom, total_appearances in sorted(all_sources_any_session.items(), key=lambda x: -x[1])[:10]:
        if dom == target_root:
            continue
        competitor_rate = round(total_appearances / max(total_sessions, 1), 3)
        your_rate       = round(co_occurrences.get(dom, 0) / max(sessions_with_target, 1), 3)
        if competitor_rate > 0.1 and your_rate < competitor_rate * 0.5:
            association_gaps.append({
                "domain":          dom,
                "competitor_rate": competitor_rate,
                "your_rate":       your_rate,
                "recommendation":  f"Increase visibility alongside {dom} — appears in {round(competitor_rate*100)}% of AI sessions",
                "relationship":    classify_relationship(dom),
            })

    # Insights
    insights = []
    if sessions_with_target == 0:
        insights.append(f"{target_brand} was not found in any AI response sources across {total_sessions} sessions — GEO visibility is zero")
    else:
        coverage = round(sessions_with_target / max(total_sessions, 1) * 100)
        insights.append(f"{target_brand} appears in {coverage}% of sessions ({sessions_with_target}/{total_sessions})")
    if frequent:
        top = frequent[0]
        insights.append(f"Most frequent co-citation: {top['domain']} ({top['co_occurrences']} times, {classify_relationship(top['domain'])})")
    if association_gaps:
        insights.append(f"{len(association_gaps)} domain(s) outrank {target_brand} in AI co-citations — association gaps detected")

    return {
        "total_sessions_analyzed":  total_sessions,
        "sessions_with_target":     sessions_with_target,
        "frequent_co_citations":    frequent,
        "missing_associations":     missing_associations,
        "association_gaps":         association_gaps,
        "insights":                 insights,
    }
