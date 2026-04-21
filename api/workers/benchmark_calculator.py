"""
GEO Benchmark Calculator (Prompt 24)
Uses stdlib `statistics` only — no pandas/numpy.
Minimum 3 projects per (vertical, locale) bucket for anonymisation.
"""

import logging
import statistics
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import FanoutProject, FanoutTrackingRun, FanoutTrackingConfig, GeoBenchmark

logger = logging.getLogger("benchmark_calculator")
MIN_SAMPLE = 3


def _percentile(data: list[float], pct: float) -> float:
    """Simple percentile without numpy: linear interpolation."""
    if not data:
        return 0.0
    data = sorted(data)
    k = (len(data) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (k - lo)


async def calculate_geo_benchmarks(db: AsyncSession, period_month: Optional[str] = None) -> dict:
    """
    Aggregate latest tracking run per project into geo_benchmarks.
    period_month: "YYYY-MM", defaults to current month.
    Returns {"processed": int, "skipped_buckets": int, "period_month": str}
    """
    if not period_month:
        period_month = datetime.now(timezone.utc).strftime("%Y-%m")

    # Load all active projects
    projects = (
        await db.execute(select(FanoutProject).where(FanoutProject.is_active == True))
    ).scalars().all()

    # Build bucket: (vertical, locale) -> list[mention_rate, composite_score]
    buckets: dict[tuple, list[dict]] = {}
    for proj in projects:
        # Get latest completed run for this project's tracking configs
        configs = (
            await db.execute(
                select(FanoutTrackingConfig).where(
                    FanoutTrackingConfig.project_id == proj.id,
                    FanoutTrackingConfig.is_active == True,
                )
            )
        ).scalars().all()

        for cfg in configs:
            latest_run = (
                await db.execute(
                    select(FanoutTrackingRun)
                    .where(
                        FanoutTrackingRun.config_id == cfg.id,
                        FanoutTrackingRun.status == "completed",
                    )
                    .order_by(FanoutTrackingRun.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if latest_run and latest_run.mention_rate is not None:
                key = (proj.vertical or "generic", proj.locale or "en-US")
                buckets.setdefault(key, []).append(
                    {
                        "mention_rate": latest_run.mention_rate,
                        "composite_score": latest_run.composite_score,
                    }
                )
                break  # one run per project

    processed = 0
    skipped = 0
    for (vertical, locale), items in buckets.items():
        if len(items) < MIN_SAMPLE:
            skipped += 1
            continue

        rates = [x["mention_rate"] for x in items]
        scores = [x["composite_score"] for x in items if x["composite_score"] is not None]

        existing = (
            await db.execute(
                select(GeoBenchmark).where(
                    GeoBenchmark.vertical == vertical,
                    GeoBenchmark.locale == locale,
                    GeoBenchmark.period_month == period_month,
                )
            )
        ).scalar_one_or_none()

        vals = dict(
            sample_size=len(items),
            avg_mention_rate=statistics.mean(rates),
            median_mention_rate=statistics.median(rates),
            p25_mention_rate=_percentile(rates, 25),
            p75_mention_rate=_percentile(rates, 75),
            avg_composite_score=statistics.mean(scores) if scores else None,
            calculated_at=datetime.now(timezone.utc),
        )

        if existing:
            for k, v in vals.items():
                setattr(existing, k, v)
        else:
            db.add(
                GeoBenchmark(
                    vertical=vertical,
                    locale=locale,
                    period_month=period_month,
                    **vals,
                )
            )

        processed += 1

    await db.commit()
    logger.info(
        "Benchmarks: processed %d buckets, skipped %d (< %d samples)",
        processed,
        skipped,
        MIN_SAMPLE,
    )
    return {"processed": processed, "skipped_buckets": skipped, "period_month": period_month}
