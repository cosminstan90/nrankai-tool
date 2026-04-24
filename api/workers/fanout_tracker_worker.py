"""
Fan-Out Historical Tracking Worker.

Executes scheduled tracking configs: runs fan-out analysis for each configured
prompt set, stores aggregate stats and per-prompt details, handles retry logic
and dead-letter marking.

Called from the main scheduler loop every 15 minutes.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import (
    AsyncSessionLocal,
    FanoutTrackingConfig, FanoutTrackingRun, FanoutTrackingDetail,
)
from api.workers.fanout_analyzer import analyze_prompt, PROVIDER_DEFAULTS
from api.workers.prompt_discovery import classify_prompt_cluster

logger = logging.getLogger("fanout_tracker_worker")

# ── Retry policy (delegates to api/utils/retry_policy.py) ───────────────────

try:
    from api.utils.retry_policy import is_retryable as _is_retryable_fn, next_retry_delay as _next_retry_delay_fn
    def _is_retryable(error_msg: str) -> bool:
        return _is_retryable_fn(error_msg)
    def _next_retry_delay(retry_count: int) -> int:
        return _next_retry_delay_fn(retry_count)
except ImportError:
    # Fallback inline implementation if retry_policy not available
    _RETRYABLE_ERRORS = ("rate_limit", "timeout", "connection", "502", "503", "504")
    _RETRY_DELAYS = [30, 120, 480]
    def _is_retryable(error_msg: str) -> bool:
        low = error_msg.lower()
        return any(k in low for k in _RETRYABLE_ERRORS)
    def _next_retry_delay(retry_count: int) -> int:
        base = _RETRY_DELAYS[min(retry_count, len(_RETRY_DELAYS) - 1)]
        return base + random.randint(-5, 5)


def _next_run_at(schedule: str, from_dt: Optional[datetime] = None) -> datetime:
    """Compute next scheduled run datetime from a schedule string."""
    now = from_dt or datetime.now(timezone.utc)
    if schedule == "daily":
        return now + timedelta(days=1)
    elif schedule == "monthly":
        return now + timedelta(days=30)
    else:  # weekly (default)
        return now + timedelta(days=7)


# ── Domain helper ─────────────────────────────────────────────────────────────

def _find_target_position(sources, target_domain: str) -> int:
    """Return 1-based position of target domain in sources list, 0 if absent."""
    target = target_domain.lower().lstrip("www.")
    for i, src in enumerate(sources, 1):
        url = src.url if hasattr(src, "url") else ""
        try:
            netloc = urlparse(url).netloc.lower().lstrip("www.")
        except Exception:
            continue
        if netloc == target or netloc.endswith("." + target):
            return i
    return 0


# ── Core run logic ────────────────────────────────────────────────────────────

async def run_tracking(config_id: str) -> None:
    """
    Execute one tracking run for the given config.

    Creates a FanoutTrackingRun row, runs fan-out analysis for every
    (prompt × engine) combination, stores details, then updates aggregate stats.
    """
    async with AsyncSessionLocal() as db:
        config = await db.get(FanoutTrackingConfig, config_id)
        if not config:
            logger.error("TrackingConfig %s not found", config_id)
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Check if a run for today already exists and is completed
        existing = await db.execute(
            select(FanoutTrackingRun).where(
                and_(
                    FanoutTrackingRun.config_id == config_id,
                    FanoutTrackingRun.run_date == today,
                    FanoutTrackingRun.status == "completed",
                )
            )
        )
        if existing.scalar_one_or_none():
            logger.info("Config %s already has a completed run for %s — skipping", config_id, today)
            return

        # Create the run row
        run = FanoutTrackingRun(
            config_id=config_id,
            run_date=today,
            status="running",
            total_prompts=len(config.prompts or []),
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)

        prompts = config.prompts or []
        engines = config.engines or ["openai"]
        target_domain = (config.target_domain or "").lower().lstrip("www.")

        detail_rows = []
        mention_count = 0
        positions = []
        all_sources_seen = set()
        competitor_counts: dict = {}
        total_cost = 0.0

        try:
            for prompt in prompts:
                cluster = classify_prompt_cluster(prompt)

                for engine in engines:
                    logger.debug("Tracking %s | engine=%s | prompt=%.50s", config_id, engine, prompt)
                    try:
                        result = await analyze_prompt(prompt, provider=engine)
                        await asyncio.sleep(1.0)  # rate-limit buffer
                    except Exception as exc:
                        logger.warning("Engine %s failed for prompt %r: %s", engine, prompt[:50], exc)
                        detail_rows.append(FanoutTrackingDetail(
                            run_id=run.id,
                            prompt=prompt,
                            prompt_cluster=cluster,
                            engine=engine,
                            target_found=False,
                            source_count=0,
                            fanout_query_count=0,
                        ))
                        continue

                    pos = _find_target_position(result.sources, target_domain) if target_domain else 0
                    found = pos > 0
                    if found:
                        mention_count += 1
                        positions.append(pos)

                    # Track competitor domains
                    for src in result.sources:
                        url = src.url if hasattr(src, "url") else ""
                        try:
                            domain = urlparse(url).netloc.lower().lstrip("www.")
                        except Exception:
                            domain = ""
                        if domain:
                            all_sources_seen.add(domain)
                            if domain != target_domain:
                                competitor_counts[domain] = competitor_counts.get(domain, 0) + 1

                    detail_rows.append(FanoutTrackingDetail(
                        run_id=run.id,
                        prompt=prompt,
                        prompt_cluster=cluster,
                        engine=engine,
                        target_found=found,
                        source_position=pos if found else None,
                        fanout_query_count=result.total_fanout_queries,
                        source_count=result.total_sources,
                    ))

            # Aggregate stats
            total_combos = len(prompts) * len(engines)
            mention_rate = mention_count / total_combos if total_combos else 0.0
            avg_pos = sum(positions) / len(positions) if positions else None
            top_competitors = [
                {"domain": d, "appearances": c}
                for d, c in sorted(competitor_counts.items(), key=lambda x: -x[1])[:10]
            ]

            # Determine model version string (first engine default model)
            model_version = PROVIDER_DEFAULTS.get(engines[0], "unknown") if engines else "unknown"

            # Persist detail rows
            db.add_all(detail_rows)

            # Update run to completed
            run.status = "completed"
            run.mention_rate = round(mention_rate, 4)
            run.avg_source_position = round(avg_pos, 2) if avg_pos else None
            run.total_unique_sources = len(all_sources_seen)
            run.top_competitors = top_competitors
            run.model_version = model_version
            run.cost_usd = round(total_cost, 4)

            # Update config timestamps
            config.last_run_at = datetime.now(timezone.utc)
            config.next_run_at = _next_run_at(config.schedule)

            await db.commit()
            logger.info(
                "Tracking run complete: config=%s date=%s mention_rate=%.2f%%",
                config_id, today, mention_rate * 100,
            )

        except Exception as exc:
            error_msg = str(exc)
            logger.error("Tracking run failed for config %s: %s", config_id, error_msg)

            run.status = "failed"
            run.error_message = error_msg
            run.failure_reason = error_msg[:500]

            if _is_retryable(error_msg) and run.retry_count < run.max_retries:
                delay_min = _next_retry_delay(run.retry_count)
                run.retry_count += 1
                run.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=delay_min)
                logger.info(
                    "Will retry config %s in %d minutes (attempt %d/%d)",
                    config_id, delay_min, run.retry_count, run.max_retries,
                )
            else:
                run.is_dead_letter = True
                logger.warning("Config %s marked as dead letter after %d retries", config_id, run.retry_count)

            await db.commit()


# ── Scheduler entry point ─────────────────────────────────────────────────────

async def check_and_run_due_trackings() -> None:
    """
    Check for due tracking configs and retry-eligible failed runs.
    Called by the main scheduler loop every 15 minutes.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        # Normal scheduled runs
        due = await db.execute(
            select(FanoutTrackingConfig).where(
                and_(
                    FanoutTrackingConfig.is_active == True,
                    FanoutTrackingConfig.next_run_at <= now,
                )
            )
        )
        configs = due.scalars().all()

        # Retry-eligible failed runs
        retry_due = await db.execute(
            select(FanoutTrackingRun).where(
                and_(
                    FanoutTrackingRun.status == "failed",
                    FanoutTrackingRun.is_dead_letter == False,
                    FanoutTrackingRun.next_retry_at <= now,
                    FanoutTrackingRun.retry_count < FanoutTrackingRun.max_retries,
                )
            )
        )
        retry_runs = retry_due.scalars().all()

    config_ids = [c.id for c in configs]
    retry_config_ids = list({r.config_id for r in retry_runs})

    all_ids = list(set(config_ids + retry_config_ids))
    if not all_ids:
        return

    logger.info(
        "Tracking scheduler: %d due, %d retries → %d total",
        len(config_ids), len(retry_config_ids), len(all_ids),
    )

    for config_id in all_ids:
        try:
            await run_tracking(config_id)
        except Exception as exc:
            logger.error("Unexpected error running tracking %s: %s", config_id, exc)
