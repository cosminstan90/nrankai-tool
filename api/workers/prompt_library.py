"""
Prompt Library (Prompt 21)
==========================
Manages a persistent library of fan-out prompts with performance statistics.

Seed data: 60+ prompts across 8 verticals.
Performance tiers:  high (≥60% mention rate) | medium (≥30%) | low (<30%) | untested
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import FanoutPromptLibrary

logger = logging.getLogger("prompt_library")


# ============================================================================
# SEED DATA  (60+ prompts across 8 verticals)
# ============================================================================

SEED_PROMPTS: list[dict] = [
    # ── SEO Agency (12) ──────────────────────────────────────────────────────
    {"vertical": "seo_agency",   "cluster": "best_of",     "prompt_text": "best SEO agency {city}",                    "is_template": True,  "template_vars": ["city"]},
    {"vertical": "seo_agency",   "cluster": "best_of",     "prompt_text": "top SEO companies {city}",                  "is_template": True,  "template_vars": ["city"]},
    {"vertical": "seo_agency",   "cluster": "best_of",     "prompt_text": "best digital marketing agency {city}",      "is_template": True,  "template_vars": ["city"]},
    {"vertical": "seo_agency",   "cluster": "best_of",     "prompt_text": "recommended SEO agency for small business",  "is_template": False, "template_vars": []},
    {"vertical": "seo_agency",   "cluster": "pricing",     "prompt_text": "SEO agency pricing {city}",                 "is_template": True,  "template_vars": ["city"]},
    {"vertical": "seo_agency",   "cluster": "pricing",     "prompt_text": "how much does SEO cost per month",          "is_template": False, "template_vars": []},
    {"vertical": "seo_agency",   "cluster": "comparison",  "prompt_text": "{brand} vs {competitor} SEO agency",        "is_template": True,  "template_vars": ["brand", "competitor"]},
    {"vertical": "seo_agency",   "cluster": "comparison",  "prompt_text": "SEO agency vs in-house SEO team",           "is_template": False, "template_vars": []},
    {"vertical": "seo_agency",   "cluster": "local",       "prompt_text": "local SEO agency near me",                  "is_template": False, "template_vars": []},
    {"vertical": "seo_agency",   "cluster": "local",       "prompt_text": "SEO services {city} Romania",               "is_template": True,  "template_vars": ["city"]},
    {"vertical": "seo_agency",   "cluster": "problem",     "prompt_text": "why is my website not ranking on Google",   "is_template": False, "template_vars": []},
    {"vertical": "seo_agency",   "cluster": "problem",     "prompt_text": "how to increase organic traffic fast",      "is_template": False, "template_vars": []},

    # ── Beauty Clinic (12) ────────────────────────────────────────────────────
    {"vertical": "beauty_clinic", "cluster": "best_of",    "prompt_text": "best beauty clinic {city}",                 "is_template": True,  "template_vars": ["city"]},
    {"vertical": "beauty_clinic", "cluster": "best_of",    "prompt_text": "top aesthetic clinic {city}",               "is_template": True,  "template_vars": ["city"]},
    {"vertical": "beauty_clinic", "cluster": "best_of",    "prompt_text": "best med spa near me",                      "is_template": False, "template_vars": []},
    {"vertical": "beauty_clinic", "cluster": "pricing",    "prompt_text": "botox price {city}",                        "is_template": True,  "template_vars": ["city"]},
    {"vertical": "beauty_clinic", "cluster": "pricing",    "prompt_text": "lip filler cost {city}",                    "is_template": True,  "template_vars": ["city"]},
    {"vertical": "beauty_clinic", "cluster": "pricing",    "prompt_text": "laser hair removal price",                  "is_template": False, "template_vars": []},
    {"vertical": "beauty_clinic", "cluster": "local",      "prompt_text": "beauty clinic {city} reviews",              "is_template": True,  "template_vars": ["city"]},
    {"vertical": "beauty_clinic", "cluster": "local",      "prompt_text": "aesthetic doctor {city}",                   "is_template": True,  "template_vars": ["city"]},
    {"vertical": "beauty_clinic", "cluster": "problem",    "prompt_text": "how to reduce wrinkles without surgery",    "is_template": False, "template_vars": []},
    {"vertical": "beauty_clinic", "cluster": "problem",    "prompt_text": "best treatment for under eye circles",      "is_template": False, "template_vars": []},
    {"vertical": "beauty_clinic", "cluster": "comparison", "prompt_text": "botox vs filler which is better",           "is_template": False, "template_vars": []},
    {"vertical": "beauty_clinic", "cluster": "branded",    "prompt_text": "{brand} clinic reviews {city}",             "is_template": True,  "template_vars": ["brand", "city"]},

    # ── Dental Clinic (6) ─────────────────────────────────────────────────────
    {"vertical": "dental_clinic", "cluster": "best_of",    "prompt_text": "best dentist {city}",                       "is_template": True,  "template_vars": ["city"]},
    {"vertical": "dental_clinic", "cluster": "best_of",    "prompt_text": "top dental clinic {city} reviews",          "is_template": True,  "template_vars": ["city"]},
    {"vertical": "dental_clinic", "cluster": "pricing",    "prompt_text": "dental implant cost {city}",                "is_template": True,  "template_vars": ["city"]},
    {"vertical": "dental_clinic", "cluster": "pricing",    "prompt_text": "teeth whitening price {city}",              "is_template": True,  "template_vars": ["city"]},
    {"vertical": "dental_clinic", "cluster": "problem",    "prompt_text": "emergency dentist {city}",                  "is_template": True,  "template_vars": ["city"]},
    {"vertical": "dental_clinic", "cluster": "comparison", "prompt_text": "dental veneers vs crowns which is better",  "is_template": False, "template_vars": []},

    # ── Restaurant (6) ────────────────────────────────────────────────────────
    {"vertical": "restaurant",    "cluster": "best_of",    "prompt_text": "best restaurant {city} {year}",             "is_template": True,  "template_vars": ["city", "year"]},
    {"vertical": "restaurant",    "cluster": "best_of",    "prompt_text": "top fine dining {city}",                    "is_template": True,  "template_vars": ["city"]},
    {"vertical": "restaurant",    "cluster": "local",      "prompt_text": "romantic restaurant {city} reservation",    "is_template": True,  "template_vars": ["city"]},
    {"vertical": "restaurant",    "cluster": "local",      "prompt_text": "vegan restaurant {city}",                   "is_template": True,  "template_vars": ["city"]},
    {"vertical": "restaurant",    "cluster": "branded",    "prompt_text": "{brand} restaurant menu and prices",        "is_template": True,  "template_vars": ["brand"]},
    {"vertical": "restaurant",    "cluster": "problem",    "prompt_text": "best food delivery app {city}",             "is_template": True,  "template_vars": ["city"]},

    # ── SaaS (8) ──────────────────────────────────────────────────────────────
    {"vertical": "saas",          "cluster": "best_of",    "prompt_text": "best project management software {year}",   "is_template": True,  "template_vars": ["year"]},
    {"vertical": "saas",          "cluster": "best_of",    "prompt_text": "top CRM software for small business",       "is_template": False, "template_vars": []},
    {"vertical": "saas",          "cluster": "comparison", "prompt_text": "{brand} vs {competitor} which is better",   "is_template": True,  "template_vars": ["brand", "competitor"]},
    {"vertical": "saas",          "cluster": "pricing",    "prompt_text": "{brand} pricing plans",                     "is_template": True,  "template_vars": ["brand"]},
    {"vertical": "saas",          "cluster": "pricing",    "prompt_text": "affordable SEO tools for agencies",         "is_template": False, "template_vars": []},
    {"vertical": "saas",          "cluster": "problem",    "prompt_text": "how to improve team collaboration remotely","is_template": False, "template_vars": []},
    {"vertical": "saas",          "cluster": "problem",    "prompt_text": "automate invoice processing small business", "is_template": False, "template_vars": []},
    {"vertical": "saas",          "cluster": "best_of",    "prompt_text": "best AI writing tools {year}",              "is_template": True,  "template_vars": ["year"]},

    # ── Real Estate (6) ───────────────────────────────────────────────────────
    {"vertical": "real_estate",   "cluster": "best_of",    "prompt_text": "best real estate agent {city}",             "is_template": True,  "template_vars": ["city"]},
    {"vertical": "real_estate",   "cluster": "pricing",    "prompt_text": "apartment prices {city} {year}",            "is_template": True,  "template_vars": ["city", "year"]},
    {"vertical": "real_estate",   "cluster": "local",      "prompt_text": "neighborhoods to live in {city}",           "is_template": True,  "template_vars": ["city"]},
    {"vertical": "real_estate",   "cluster": "problem",    "prompt_text": "how to buy a house first time buyer guide", "is_template": False, "template_vars": []},
    {"vertical": "real_estate",   "cluster": "problem",    "prompt_text": "mortgage calculator {city}",                "is_template": True,  "template_vars": ["city"]},
    {"vertical": "real_estate",   "cluster": "comparison", "prompt_text": "rent vs buy apartment {city}",              "is_template": True,  "template_vars": ["city"]},

    # ── Law Firm (5) ─────────────────────────────────────────────────────────
    {"vertical": "law_firm",      "cluster": "best_of",    "prompt_text": "best employment lawyer {city}",             "is_template": True,  "template_vars": ["city"]},
    {"vertical": "law_firm",      "cluster": "best_of",    "prompt_text": "top immigration attorney {city}",           "is_template": True,  "template_vars": ["city"]},
    {"vertical": "law_firm",      "cluster": "pricing",    "prompt_text": "divorce lawyer cost {city}",                "is_template": True,  "template_vars": ["city"]},
    {"vertical": "law_firm",      "cluster": "problem",    "prompt_text": "what to do after a car accident {city}",    "is_template": True,  "template_vars": ["city"]},
    {"vertical": "law_firm",      "cluster": "problem",    "prompt_text": "how to file for bankruptcy step by step",   "is_template": False, "template_vars": []},

    # ── Generic (10+) ─────────────────────────────────────────────────────────
    {"vertical": "generic",       "cluster": "branded",    "prompt_text": "what is {brand}",                           "is_template": True,  "template_vars": ["brand"]},
    {"vertical": "generic",       "cluster": "branded",    "prompt_text": "{brand} reviews {year}",                    "is_template": True,  "template_vars": ["brand", "year"]},
    {"vertical": "generic",       "cluster": "branded",    "prompt_text": "is {brand} legit",                          "is_template": True,  "template_vars": ["brand"]},
    {"vertical": "generic",       "cluster": "best_of",    "prompt_text": "best {service} near me",                    "is_template": True,  "template_vars": ["service"]},
    {"vertical": "generic",       "cluster": "pricing",    "prompt_text": "{brand} pricing {year}",                    "is_template": True,  "template_vars": ["brand", "year"]},
    {"vertical": "generic",       "cluster": "comparison", "prompt_text": "{brand} alternatives {year}",               "is_template": True,  "template_vars": ["brand", "year"]},
    {"vertical": "generic",       "cluster": "problem",    "prompt_text": "how does {brand} work",                     "is_template": True,  "template_vars": ["brand"]},
    {"vertical": "generic",       "cluster": "problem",    "prompt_text": "{brand} vs {competitor} comparison",        "is_template": True,  "template_vars": ["brand", "competitor"]},
    {"vertical": "generic",       "cluster": "best_of",    "prompt_text": "top {service} companies {city} {year}",     "is_template": True,  "template_vars": ["service", "city", "year"]},
    {"vertical": "generic",       "cluster": "local",      "prompt_text": "{service} {city} contact",                  "is_template": True,  "template_vars": ["service", "city"]},
    {"vertical": "generic",       "cluster": "branded",    "prompt_text": "{brand} contact {city}",                    "is_template": True,  "template_vars": ["brand", "city"]},
]


def _prompt_hash(text: str) -> str:
    return hashlib.sha256(text.lower().strip().encode()).hexdigest()


def _render_template(text: str, brand: Optional[str] = None, city: Optional[str] = None,
                     year: Optional[str] = None, service: Optional[str] = None,
                     competitor: Optional[str] = None) -> str:
    """Replace {placeholder} tokens with provided values."""
    now_year = str(datetime.now(timezone.utc).year)
    replacements = {
        "{brand}":      brand      or "your brand",
        "{city}":       city       or "your city",
        "{year}":       year       or now_year,
        "{service}":    service    or "service",
        "{competitor}": competitor or "competitor",
    }
    result = text
    for k, v in replacements.items():
        result = result.replace(k, v)
    return result


# ============================================================================
# PromptLibrary class
# ============================================================================

class PromptLibrary:

    @classmethod
    async def seed(cls, db: AsyncSession) -> int:
        """Seed the library if it is empty. Returns number of prompts inserted."""
        count = (await db.execute(select(func.count(FanoutPromptLibrary.id)))).scalar_one()
        if count > 0:
            return 0

        added = 0
        for p in SEED_PROMPTS:
            ph = _prompt_hash(p["prompt_text"])
            existing = (await db.execute(
                select(FanoutPromptLibrary).where(FanoutPromptLibrary.prompt_hash == ph)
            )).scalar_one_or_none()
            if not existing:
                db.add(FanoutPromptLibrary(
                    prompt_text   = p["prompt_text"],
                    prompt_hash   = ph,
                    vertical      = p.get("vertical", "generic"),
                    cluster       = p.get("cluster"),
                    is_template   = p.get("is_template", False),
                    template_vars = p.get("template_vars"),
                ))
                added += 1

        await db.commit()
        logger.info("Seeded %d prompts into prompt library", added)
        return added

    @classmethod
    async def get_for_discovery(
        cls,
        db: AsyncSession,
        vertical: Optional[str] = None,
        brand: Optional[str]    = None,
        city: Optional[str]     = None,
        year: Optional[str]     = None,
        count: int = 20,
    ) -> List[str]:
        """
        Return rendered prompt strings for use by PromptDiscovery.
        Priority: high > medium > untested > low.
        """
        stmt = select(FanoutPromptLibrary)
        if vertical:
            stmt = stmt.where(FanoutPromptLibrary.vertical == vertical)

        # Order: high first, then medium, untested, low
        tier_order = {"high": 0, "medium": 1, "untested": 2, "low": 3}
        rows = (await db.execute(stmt)).scalars().all()
        rows.sort(key=lambda r: (tier_order.get(r.performance_tier, 2), -(r.times_used or 0)))

        now_year = year or str(datetime.now(timezone.utc).year)
        results: List[str] = []
        for r in rows[:count]:
            if r.is_template:
                rendered = _render_template(r.prompt_text, brand=brand, city=city, year=now_year)
            else:
                rendered = r.prompt_text
            results.append(rendered)
        return results

    @classmethod
    async def record_usage(
        cls,
        db: AsyncSession,
        prompt_text: str,
        mention_rate: Optional[float] = None,
        fanout_query_count: Optional[int] = None,
        source_count: Optional[int] = None,
    ) -> None:
        """Update running averages and performance_tier for *prompt_text*."""
        ph  = _prompt_hash(prompt_text)
        row = (await db.execute(
            select(FanoutPromptLibrary).where(FanoutPromptLibrary.prompt_hash == ph)
        )).scalar_one_or_none()

        if row is None:
            return

        n = (row.times_used or 0) + 1
        row.times_used   = n
        row.last_used_at = datetime.now(timezone.utc)

        if mention_rate is not None:
            prev = row.avg_mention_rate or 0.0
            row.avg_mention_rate = (prev * (n - 1) + mention_rate) / n
            # Update performance tier
            if row.avg_mention_rate >= 0.60:
                row.performance_tier = "high"
            elif row.avg_mention_rate >= 0.30:
                row.performance_tier = "medium"
            else:
                row.performance_tier = "low"

        if fanout_query_count is not None:
            prev = row.avg_fanout_queries or 0.0
            row.avg_fanout_queries = (prev * (n - 1) + fanout_query_count) / n

        if source_count is not None:
            prev = row.avg_source_count or 0.0
            row.avg_source_count = (prev * (n - 1) + source_count) / n

        try:
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning("record_usage failed: %s", exc)

    @classmethod
    async def suggest_gaps(
        cls,
        db: AsyncSession,
        vertical: str,
        existing_prompts: List[str],
    ) -> List[str]:
        """Return library prompts not present in *existing_prompts*."""
        existing_hashes = {_prompt_hash(p) for p in existing_prompts}
        stmt = (
            select(FanoutPromptLibrary)
            .where(FanoutPromptLibrary.vertical == vertical)
            .where(FanoutPromptLibrary.is_template == False)  # noqa: E712
        )
        rows = (await db.execute(stmt)).scalars().all()
        return [r.prompt_text for r in rows if r.prompt_hash not in existing_hashes]

    @classmethod
    async def add_from_discovery(
        cls,
        db: AsyncSession,
        discovery_result: dict,
        vertical: str,
    ) -> int:
        """Add prompts from a discovery result that have mention_rate > 0."""
        added = 0
        for item in discovery_result.get("mentioned_in", []):
            text = item.get("prompt", "")
            if not text:
                continue
            ph = _prompt_hash(text)
            existing = (await db.execute(
                select(FanoutPromptLibrary).where(FanoutPromptLibrary.prompt_hash == ph)
            )).scalar_one_or_none()
            if not existing:
                db.add(FanoutPromptLibrary(
                    prompt_text      = text,
                    prompt_hash      = ph,
                    vertical         = vertical,
                    performance_tier = "untested",
                ))
                added += 1
        if added:
            await db.commit()
        return added

    @classmethod
    async def add_prompt(
        cls,
        db: AsyncSession,
        prompt_text: str,
        vertical: str = "generic",
        cluster: Optional[str] = None,
        language: str = "en",
        locale: str = "en-US",
        tags: Optional[List[str]] = None,
        is_template: bool = False,
        template_vars: Optional[List[str]] = None,
    ) -> dict:
        ph = _prompt_hash(prompt_text)
        existing = (await db.execute(
            select(FanoutPromptLibrary).where(FanoutPromptLibrary.prompt_hash == ph)
        )).scalar_one_or_none()
        if existing:
            return existing.to_dict()

        row = FanoutPromptLibrary(
            prompt_text   = prompt_text,
            prompt_hash   = ph,
            vertical      = vertical,
            cluster       = cluster,
            language      = language,
            locale        = locale,
            tags          = tags,
            is_template   = is_template,
            template_vars = template_vars,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.to_dict()

    @classmethod
    async def get_for_display(
        cls,
        db: AsyncSession,
        vertical: Optional[str]    = None,
        cluster: Optional[str]     = None,
        performance: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        stmt = select(FanoutPromptLibrary)
        if vertical:
            stmt = stmt.where(FanoutPromptLibrary.vertical == vertical)
        if cluster:
            stmt = stmt.where(FanoutPromptLibrary.cluster == cluster)
        if performance:
            stmt = stmt.where(FanoutPromptLibrary.performance_tier == performance)
        stmt = stmt.order_by(
            desc(FanoutPromptLibrary.times_used),
            FanoutPromptLibrary.id,
        ).limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
        return [r.to_dict() for r in rows]

    @classmethod
    async def get_stats(cls, db: AsyncSession) -> dict:
        total = (await db.execute(select(func.count(FanoutPromptLibrary.id)))).scalar_one()

        tier_rows = (await db.execute(
            select(FanoutPromptLibrary.performance_tier, func.count(FanoutPromptLibrary.id))
            .group_by(FanoutPromptLibrary.performance_tier)
        )).all()
        by_tier = {row[0]: row[1] for row in tier_rows}

        vert_rows = (await db.execute(
            select(FanoutPromptLibrary.vertical, func.count(FanoutPromptLibrary.id))
            .group_by(FanoutPromptLibrary.vertical)
        )).all()
        by_vertical = {row[0]: row[1] for row in vert_rows}

        avg_mention = (await db.execute(select(func.avg(FanoutPromptLibrary.avg_mention_rate)))).scalar_one()

        return {
            "total":           total,
            "by_tier":         by_tier,
            "by_vertical":     by_vertical,
            "avg_mention_rate": round(float(avg_mention or 0), 3),
        }
