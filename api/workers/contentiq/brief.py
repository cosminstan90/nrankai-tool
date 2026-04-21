"""
ContentIQ Claude Brief Generator (Prompt 08)
=============================================
Generates structured 8-section content briefs for UPDATE/CONSOLIDATE pages.
Uses claude-sonnet-4-20250514 (~$0.015/brief).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import List

logger = logging.getLogger("contentiq.brief")

_MODEL = "claude-sonnet-4-20250514"

_SYSTEM = (
    "You are a senior SEO and GEO content strategist at nrankai.com. "
    "You produce precise, actionable content briefs for Romanian and international websites. "
    "Your briefs are structured, specific, and optimized for both traditional search and AI answer engines (GEO). "
    "Respond in the same language as the page title/URL — Romanian if the site is Romanian, English otherwise. "
    "Never include generic advice. Every recommendation must be specific to the given page."
)


def _build_user_prompt(page: dict, audit_domain: str) -> str:
    verdict        = page.get("verdict", "UPDATE")
    url            = page.get("url", "")
    title          = page.get("title") or "N/A"
    h1             = page.get("h1") or "N/A"
    word_count     = page.get("word_count") or 0
    last_modified  = page.get("last_modified") or "unknown"
    meta_desc      = page.get("meta_description") or "N/A"
    sf             = page.get("score_freshness") or 0
    sg             = page.get("score_geo") or 0
    se             = page.get("score_eeat") or 0
    ssh            = page.get("score_seo_health") or 0
    st             = page.get("score_total") or 0
    vr             = page.get("verdict_reason") or ""
    gsc_clicks     = page.get("gsc_clicks") or 0
    gsc_impr       = page.get("gsc_impressions") or 0
    gsc_pos        = page.get("gsc_position") or "N/A"
    ahrefs_tr      = page.get("ahrefs_traffic") or 0
    ahrefs_kw      = page.get("ahrefs_keywords") or 0
    ahrefs_bl      = page.get("ahrefs_backlinks") or 0

    return f"""Generate a content brief for this page that needs a [{verdict}] action.

URL: {url}
Title: {title}
H1: {h1}
Word Count: {word_count}
Last Modified: {last_modified}
Meta Description: {meta_desc}

Performance scores (0-100):
- Freshness: {sf} — {vr}
- GEO Visibility: {sg}
- E-E-A-T: {se}
- SEO Health: {ssh}
- Total: {st}

Traffic signals (last 90 days):
- GSC Clicks: {gsc_clicks} | Impressions: {gsc_impr} | Avg Position: {gsc_pos}
- Ahrefs Organic Traffic: {ahrefs_tr} | Keywords: {ahrefs_kw} | Backlinks: {ahrefs_bl}

Verdict: {verdict} — {vr}

Produce a brief with these sections:
1. OBJECTIVE — Why this action, what outcome to achieve (2-3 sentences)
2. TARGET KEYWORDS — 3-5 primary keywords to target or preserve, with intent
3. CONTENT STRUCTURE — Recommended H1, H2 headings (as bullet list)
4. CONTENT REQUIREMENTS — Word count target, key topics to cover, data/stats to include
5. GEO OPTIMIZATION — How to structure content for AI answer engines (FAQ, definitions, direct answers)
6. E-E-A-T SIGNALS — Specific additions: author bio, citations, trust elements
7. INTERNAL LINKING — 2-3 specific internal link opportunities on {audit_domain}
8. PRIORITY — High/Medium/Low with 1-line justification"""


async def generate_brief(page: dict, audit_domain: str) -> str:
    """Generate a content brief for a single page. Returns brief text."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    import anthropic
    client = anthropic.AsyncAnthropic(api_key=api_key)

    try:
        response = await client.messages.create(
            model      = _MODEL,
            max_tokens = 1500,
            temperature= 0.3,
            system     = _SYSTEM,
            messages   = [{"role": "user", "content": _build_user_prompt(page, audit_domain)}],
        )
        return response.content[0].text if response.content else ""
    except Exception as exc:
        logger.error("Brief generation failed for %s: %s", page.get("url"), exc)
        raise


async def batch_generate_briefs(
    pages: List[dict],
    audit_domain: str,
    audit_id: int,
    db,
    concurrency: int = 3,
) -> int:
    """
    Generate briefs for all UPDATE/CONSOLIDATE pages that don't have one yet.
    Saves to DB. Returns count of briefs generated.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select, update
    from api.models.contentiq import CiqPage

    targets = [
        p for p in pages
        if p.get("verdict") in ("UPDATE", "CONSOLIDATE") and not p.get("brief_generated")
    ]
    if not targets:
        return 0

    sem   = asyncio.Semaphore(concurrency)
    count = 0

    async def _gen(page: dict):
        nonlocal count
        async with sem:
            try:
                brief = await generate_brief(page, audit_domain)
                # Update DB
                await db.execute(
                    update(CiqPage)
                    .where(CiqPage.id == page["id"])
                    .values(
                        brief_content   = brief,
                        brief_generated = True,
                        scored_at       = datetime.now(timezone.utc),
                    )
                )
                await db.commit()
                count += 1
                logger.info("[Brief] %d briefs generated so far", count)
            except Exception as exc:
                logger.error("Brief failed for page %d: %s", page.get("id"), exc)

    await asyncio.gather(*[_gen(p) for p in targets])
    return count
