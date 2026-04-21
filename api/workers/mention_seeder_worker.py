"""
Mention Seeding Worker (Prompt 32)
===================================
Scans Reddit, Quora, G2, Capterra, Trustpilot, and press for brand mentions
using Serper.dev search API. Stores results in mention_seeding_results table.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, date
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger("mention_seeder")

PLATFORMS = {
    "reddit":     lambda brand, domain: f'site:reddit.com "{brand}"',
    "quora":      lambda brand, domain: f'site:quora.com "{brand}"',
    "g2":         lambda brand, domain: f'site:g2.com "{brand}"',
    "capterra":   lambda brand, domain: f'site:capterra.com "{brand}"',
    "trustpilot": lambda brand, domain: f'site:trustpilot.com "{brand}"',
    "press":      lambda brand, domain: f'"{brand}" -site:{domain} -site:youtube.com -site:facebook.com -site:twitter.com -site:instagram.com -site:linkedin.com -site:amazon.com',
}

PLATFORM_LIMITS = {
    "reddit": 10, "quora": 10, "g2": 5,
    "capterra": 5, "trustpilot": 5, "press": 15,
}

@dataclass
class MentionResult:
    platform: str
    url: str
    title: str
    context: str  # snippet
    sentiment: str = "neutral"  # positive/neutral/negative (simple heuristic)
    is_new: bool = True

@dataclass
class MentionSeedingReport:
    config_id: int
    total_mentions: int = 0
    new_this_run: int = 0
    by_platform: dict = field(default_factory=dict)
    sentiment_breakdown: dict = field(default_factory=dict)
    coverage_score: float = 0.0
    missing_platforms: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)
    mentions: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "config_id": self.config_id,
            "total_mentions": self.total_mentions,
            "new_this_run": self.new_this_run,
            "by_platform": self.by_platform,
            "sentiment_breakdown": self.sentiment_breakdown,
            "coverage_score": self.coverage_score,
            "missing_platforms": self.missing_platforms,
            "recommendations": self.recommendations,
        }


def _simple_sentiment(text: str) -> str:
    """Very rough sentiment from snippet keywords."""
    text = text.lower()
    positive = ["best", "great", "excellent", "amazing", "love", "recommend", "top", "good", "helpful"]
    negative = ["worst", "bad", "terrible", "scam", "avoid", "hate", "poor", "awful", "horrible"]
    pos = sum(1 for w in positive if w in text)
    neg = sum(1 for w in negative if w in text)
    if neg > pos:
        return "negative"
    if pos > neg:
        return "positive"
    return "neutral"


async def _serper_search(query: str, num: int, api_key: str) -> list[dict]:
    """Call Serper.dev and return list of {url, title, snippet}."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": num},
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("organic", []):
                results.append({
                    "url":     item.get("link", ""),
                    "title":   item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                })
            return results
    except Exception as exc:
        logger.warning("Serper search failed for %r: %s", query, exc)
        return []


async def run_mention_scan(config_id: int, db) -> MentionSeedingReport:
    """
    Run a full mention scan for a MentionSeedingConfig.
    Stores new MentionSeedingResult rows in the DB.
    Returns a MentionSeedingReport.
    """
    from sqlalchemy import select
    from api.models.database import MentionSeedingConfig, MentionSeedingResult

    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key:
        logger.error("SERPER_API_KEY not configured — mention scan skipped")
        raise RuntimeError("SERPER_API_KEY not configured")

    # Load config
    cfg = (await db.execute(
        select(MentionSeedingConfig).where(MentionSeedingConfig.id == config_id)
    )).scalar_one_or_none()
    if not cfg:
        raise ValueError(f"MentionSeedingConfig {config_id} not found")

    brand  = cfg.target_brand
    domain = cfg.target_domain or ""
    run_date = date.today().isoformat()

    # Load previous URLs to detect is_new
    prev_results = (await db.execute(
        select(MentionSeedingResult.mention_url)
        .where(MentionSeedingResult.config_id == config_id)
    )).scalars().all()
    seen_urls = set(prev_results)

    # Determine which platforms to scan
    active_platforms = {}
    if cfg.monitor_reddit:     active_platforms["reddit"]     = PLATFORMS["reddit"]
    if cfg.monitor_quora:      active_platforms["quora"]      = PLATFORMS["quora"]
    if cfg.monitor_review_sites:
        active_platforms["g2"]        = PLATFORMS["g2"]
        active_platforms["capterra"]  = PLATFORMS["capterra"]
        active_platforms["trustpilot"]= PLATFORMS["trustpilot"]
    if cfg.monitor_press:      active_platforms["press"]      = PLATFORMS["press"]

    if not active_platforms:
        active_platforms = dict(PLATFORMS)  # scan all if none configured

    # Fan-out searches concurrently
    tasks = {
        plat: _serper_search(fn(brand, domain), PLATFORM_LIMITS.get(plat, 10), api_key)
        for plat, fn in active_platforms.items()
    }
    platform_results = {}
    for plat, coro in tasks.items():
        platform_results[plat] = await coro

    # Build report + persist results
    report = MentionSeedingReport(config_id=config_id)
    all_mentions = []

    for plat, items in platform_results.items():
        count = 0
        for item in items:
            url  = item["url"]
            if not url:
                continue
            sent = _simple_sentiment(item.get("snippet", "") + " " + item.get("title", ""))
            is_new = url not in seen_urls
            mention = MentionResult(
                platform=plat,
                url=url,
                title=item.get("title", ""),
                context=item.get("snippet", ""),
                sentiment=sent,
                is_new=is_new,
            )
            all_mentions.append(mention)
            count += 1

            # Persist to DB
            db.add(MentionSeedingResult(
                config_id=config_id,
                run_date=run_date,
                platform=plat,
                mention_url=url,
                mention_title=item.get("title", ""),
                mention_context=item.get("snippet", ""),
                sentiment=sent,
                is_new=is_new,
                discovered_at=datetime.now(timezone.utc),
            ))

        report.by_platform[plat] = count

    await db.commit()

    # Aggregate
    report.total_mentions = len(all_mentions)
    report.new_this_run   = sum(1 for m in all_mentions if m.is_new)
    report.mentions       = [{"platform": m.platform, "url": m.url, "title": m.title, "context": m.context, "sentiment": m.sentiment, "is_new": m.is_new} for m in all_mentions]

    # Sentiment breakdown
    for sent in ("positive", "neutral", "negative"):
        report.sentiment_breakdown[sent] = sum(1 for m in all_mentions if m.sentiment == sent)

    # Coverage score: % of scanned platforms with at least 1 mention
    covered = sum(1 for plat, cnt in report.by_platform.items() if cnt > 0)
    report.coverage_score = round(covered / max(len(active_platforms), 1) * 100, 1)

    # Missing platforms
    report.missing_platforms = [p for p, c in report.by_platform.items() if c == 0]

    # Recommendations
    recs = []
    if report.by_platform.get("reddit", 0) == 0:
        recs.append("Create or participate in relevant subreddit discussions to build Reddit presence")
    if report.by_platform.get("g2", 0) == 0:
        recs.append("Claim and optimise your G2 profile — G2 is heavily cited by AI engines")
    if report.by_platform.get("press", 0) < 3:
        recs.append("Target press coverage — reach out to relevant publications for brand mentions")
    neg_count = report.sentiment_breakdown.get("negative", 0)
    total = max(report.total_mentions, 1)
    if neg_count / total > 0.30:
        recs.append("Address negative mentions — reputation risk (>30% negative sentiment detected)")
    report.recommendations = recs

    # Update config last_run_at
    cfg.last_run_at = datetime.now(timezone.utc)
    await db.commit()

    return report
