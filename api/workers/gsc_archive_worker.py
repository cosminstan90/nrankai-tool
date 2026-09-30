"""
GSC history archive worker — Pasul 2 of docs/superpowers/plans/2026-09-30-next-steps.md.

Google's Search Analytics API serves only the trailing 16 months. The tables
that accumulate history (gsc_page_history / gsc_query_history, added in
migration 0012) had 0 rows in the real database on 2026-09-30 -- the sync
endpoint upserts them for whatever range a user manually triggers, but
nothing was filling them on its own, so a month of data goes missing for
good every month nobody manually syncs.

This worker backfills every "api"-synced GscProperty once a day, resuming
from GscProperty.history_synced_through (see migration 0020 for why that
cursor exists instead of inferring gaps from what's already stored: GSC
returns no rows at all for a day with zero impressions, so an unfetched day
and a fetched-but-silent day look identical in the data).

Disabled with GSC_ARCHIVE_ENABLED=0. On by default.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import DATABASE_PATH, GscProperty
from api.routes.gsc._shared import fetch_daily_gsc_rows, _get_gsc_credentials
from api.routes.gsc.oauth_sync import upsert_gsc_history

logger = logging.getLogger(__name__)

HISTORY_WINDOW_DAYS = 16 * 30   # ~480 days; GSC's own stated retention is 16 months
FRESHNESS_DELAY_DAYS = 3        # GSC's own data lags 2-3 days behind real time
CHUNK_DAYS = 30                 # write in ~monthly chunks: a crash mid-backfill
                                # re-fetches one chunk, not the whole 16 months
POLL_INTERVAL_S = 24 * 3600     # once a day
STARTUP_DELAY_S = 60            # let the app finish starting before the first run

# Read by GET /api/gsc/oauth/status so a stuck or failing worker is visible
# instead of silently doing nothing -- see docs/superpowers/plans/2026-09-30-next-steps.md
# Pasul 7 for the fuller health panel this is a first step toward.
LAST_RUN_STATUS = {"ran_at": None, "properties": {}, "error": None}


async def _archive_chunk(creds, property_id: str, site_url: str, start_str: str, end_str: str) -> int:
    """Fetch one date range, upsert it, advance the property's cursor. Returns rows written."""
    loop = asyncio.get_event_loop()
    (query_rows, _), (page_rows, _) = await asyncio.gather(
        loop.run_in_executor(None, fetch_daily_gsc_rows, creds, site_url, "query", start_str, end_str),
        loop.run_in_executor(None, fetch_daily_gsc_rows, creds, site_url, "page", start_str, end_str),
    )
    await loop.run_in_executor(None, upsert_gsc_history, DATABASE_PATH, property_id, query_rows, page_rows)

    # Short session, own commit -- never held open across the network calls above.
    async with AsyncSessionLocal() as db:
        prop = await db.get(GscProperty, property_id)
        if prop:
            prop.history_synced_through = end_str
            await db.commit()

    return len(query_rows) + len(page_rows)


async def _archive_property(creds, property_id: str, site_url: str,
                            synced_through: str, window_start, fetch_until) -> dict:
    """Backfill one property in monthly chunks. Returns a status dict for LAST_RUN_STATUS."""
    start = (
        datetime.strptime(synced_through, "%Y-%m-%d").date() + timedelta(days=1)
        if synced_through else window_start
    )
    start = max(start, window_start)

    if start > fetch_until:
        return {"ok": True, "rows_written": 0, "note": "up to date"}

    total_rows = 0
    chunk_start = start
    try:
        while chunk_start <= fetch_until:
            chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), fetch_until)
            total_rows += await _archive_chunk(
                creds, property_id, site_url, chunk_start.isoformat(), chunk_end.isoformat())
            chunk_start = chunk_end + timedelta(days=1)
    except Exception as exc:
        logger.exception("GSC archive: property %s failed at chunk starting %s", property_id, chunk_start)
        return {"ok": False, "rows_written": total_rows, "error": str(exc)}

    return {"ok": True, "rows_written": total_rows}


async def run_archive_once() -> None:
    """One archive pass over every api-synced GscProperty. Never raises."""
    from api.workers.worker_run import record_worker_run

    started_at = datetime.now(timezone.utc)
    result = await _run_archive_once_impl()
    LAST_RUN_STATUS.update(result)

    failed_props = [pid for pid, p in result["properties"].items() if not p.get("ok", True)]
    ok = result["error"] is None and not failed_props
    if result["error"]:
        detail = result["error"]
    elif failed_props:
        detail = f"failed properties: {failed_props}"
    else:
        total_rows = sum(p.get("rows_written", 0) for p in result["properties"].values())
        detail = f"{len(result['properties'])} properties, {total_rows} rows written"
    await record_worker_run("gsc_archive", started_at, ok, detail)


async def _run_archive_once_impl() -> dict:
    """The actual archive pass. Returns a status dict; never mutates LAST_RUN_STATUS itself."""
    async with AsyncSessionLocal() as db:
        properties = (await db.execute(
            select(GscProperty).where(GscProperty.sync_type == "api")
        )).scalars().all()
        props = [(p.id, p.site_url, p.history_synced_through) for p in properties]

    result = {"ran_at": datetime.now(timezone.utc).isoformat(), "properties": {}, "error": None}

    if not props:
        return result

    try:
        creds = await _get_gsc_credentials()
    except Exception as exc:
        logger.warning("GSC archive: could not load credentials: %s", exc)
        result["error"] = f"credentials: {exc}"
        return result

    if not creds:
        logger.info("GSC archive: no Google account connected, skipping this run")
        result["error"] = "not connected"
        return result

    today = datetime.now(timezone.utc).date()
    fetch_until = today - timedelta(days=FRESHNESS_DELAY_DAYS)
    window_start = today - timedelta(days=HISTORY_WINDOW_DAYS)

    for property_id, site_url, synced_through in props:
        result["properties"][property_id] = await _archive_property(
            creds, property_id, site_url, synced_through, window_start, fetch_until)

    return result


async def gsc_archive_worker_loop():
    """Background loop: archives GSC history once a day for every api-synced property."""
    if os.getenv("GSC_ARCHIVE_ENABLED", "1") == "0":
        logger.info("GSC_ARCHIVE_ENABLED=0 — GSC archive worker disabled")
        return

    logger.info(
        "GSC archive worker started (daily, %d-day window, %d-day freshness delay)",
        HISTORY_WINDOW_DAYS, FRESHNESS_DELAY_DAYS,
    )
    await asyncio.sleep(STARTUP_DELAY_S)

    while True:
        try:
            await run_archive_once()
        except Exception:
            logger.exception("GSC archive worker: unhandled error in run")
        await asyncio.sleep(POLL_INTERVAL_S)
