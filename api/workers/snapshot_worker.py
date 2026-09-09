"""
Captures page snapshots and compares two runs (Etapa 6 of
docs/IMPROVEMENTS_PLAN.md).

Reads the HTML the scraper already wrote rather than fetching anything, so a
capture costs no requests against the client's site and can be taken at any
time -- including retroactively, from whatever is on disk right now.
"""

import asyncio
import logging
import os
import uuid
import weakref
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import PageSnapshot, SnapshotRun
from core.page_diff import diff_snapshots, summarize
from core.page_snapshot import extract_page_fields
from core.web_scraper import safe_filename_stem

logger = logging.getLogger(__name__)


_BATCH_SIZE = 50

# Only one capture at a time, process-wide.
#
# api/models/_base.py builds the engine with poolclass=StaticPool, so every
# session in the process shares ONE physical SQLite connection. Two captures
# running at once over that single connection deadlocked on a real 545-page
# site: both stalled at exactly 445 rows, one run hung in "running" forever and
# the other reported "completed, 545 pages" against 445 stored rows.
#
# This lock makes the feature safe; it does not fix the underlying hazard,
# which is app-wide and lives in the engine configuration.
#
# Created per event loop rather than once at import: a module-level
# asyncio.Lock() binds to whichever loop first uses it and then raises
# "bound to a different event loop" everywhere else. Harmless under uvicorn,
# which runs a single loop, but it broke the tests immediately and would break
# any second loop in production just as quietly.
_CAPTURE_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _capture_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _CAPTURE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _CAPTURE_LOCKS[loop] = lock
    return lock


async def _flush(rows: list) -> None:
    """
    Write one batch in its own session and commit immediately.

    Short sessions are the point: with StaticPool every session shares one
    SQLite connection, so pending rows held across other database activity are
    lost. Each batch is committed before the next page is even read.
    """
    if not rows:
        return
    async with AsyncSessionLocal() as db:
        db.add_all([PageSnapshot(**row) for row in rows])
        await db.commit()


async def _set_status(run_id: str, status: str, pages_captured: Optional[int] = None,
                      error: Optional[str] = None, attempts: int = 3) -> None:
    """
    Write a run's terminal status, retrying briefly.

    The first serialized run of two real 545-page captures wrote all 545 rows
    and then lost only this final update, staying "running" forever: the next
    capture had already taken the lock and was hammering the shared SQLite
    connection. The data was right and only the status flag was wrong, which is
    worse than it sounds -- a run stuck in "running" looks like a crash.
    """
    for attempt in range(1, attempts + 1):
        try:
            async with AsyncSessionLocal() as db:
                run = (await db.execute(
                    select(SnapshotRun).where(SnapshotRun.id == run_id)
                )).scalar_one_or_none()
                if run is None:
                    return
                run.status = status
                if pages_captured is not None:
                    run.pages_captured = pages_captured
                if error is not None:
                    run.error = error[:2000]
                run.completed_at = datetime.now(timezone.utc)
                await db.commit()
            return
        except Exception as exc:
            logger.warning("Status update for %s failed (attempt %d/%d): %s",
                           run_id, attempt, attempts, exc)
            await asyncio.sleep(0.2 * attempt)

    logger.error("Could not record final status '%s' for snapshot run %s", status, run_id)


def _url_for_stem(stem: str, website: str) -> str:
    """
    Best-effort URL for a stored file.

    The scraper's URL -> filename rule is one-way (see safe_filename_stem), so
    a perfect inverse does not exist. The stem is used as the page's identity
    for comparison purposes; two runs of the same site produce the same stem
    for the same page, which is all the diff needs. Where a crawl exists,
    core.crawl_facts.load_crawl_url_map gives the real URLs.
    """
    return f"{website.rstrip('/')}/{stem}"


async def capture_snapshot(website: str, source_dir: str) -> str:
    """
    Extract and store one snapshot per HTML file in source_dir. Returns run id.

    Serialized by _CAPTURE_LOCK -- see that constant for why. A second capture
    waits rather than corrupting the first.

    A missing directory is recorded as a failed run and re-raised: a typo in
    the path must be visible rather than quietly producing an empty capture
    that later looks like "the whole site disappeared".
    """
    async with _capture_lock():
        return await _capture_snapshot_locked(website, source_dir)


async def _capture_snapshot_locked(website: str, source_dir: str) -> str:
    run_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        db.add(SnapshotRun(
            id=run_id, website=website, status="running",
            source_dir=source_dir, started_at=datetime.now(timezone.utc),
        ))
        await db.commit()

    try:
        if not os.path.isdir(source_dir):
            raise FileNotFoundError(f"No such directory: {source_dir}")

        files = sorted(f for f in os.listdir(source_dir) if f.lower().endswith(".html"))

        # Committed in batches, each in its own short-lived session, and never
        # holding pending rows across the whole capture.
        #
        # api/models/_base.py builds the engine with poolclass=StaticPool, so
        # every AsyncSessionLocal shares ONE physical SQLite connection. The
        # first real capture held a single session open for 2m40s over 545
        # pages while the server kept serving requests on that same connection;
        # the interleaved transactions silently discarded every pending insert,
        # leaving a run row that read "completed, 545 pages" against an empty
        # page_snapshots table. api/routes/visibility.py documents being bitten
        # by exactly this during Etapa 3.
        captured = 0
        batch = []

        for filename in files:
            path = os.path.join(source_dir, filename)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    html = fh.read()
            except OSError as exc:
                logger.warning("Could not read %s: %s", path, exc)
                continue

            stem = os.path.splitext(filename)[0]
            url = _url_for_stem(stem, website)
            fields = extract_page_fields(html, url)

            batch.append({
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "website": website,
                "url": url,
                "title": fields["title"],
                "meta_description": fields["meta_description"],
                "canonical": fields["canonical"],
                "h1": fields["h1"],
                "h2": fields["h2"],
                "h3": fields["h3"],
                "word_count": fields["word_count"],
                "internal_links": fields["internal_links"],
                "external_link_count": fields["external_link_count"],
                "images_total": fields["images_total"],
                "images_without_alt": fields["images_without_alt"],
                "schema_types": fields["schema_types"],
                "content_hash": fields["content_hash"],
                "captured_at": datetime.now(timezone.utc),
            })

            if len(batch) >= _BATCH_SIZE:
                await _flush(batch)
                captured += len(batch)
                batch = []

        if batch:
            await _flush(batch)
            captured += len(batch)

        await _set_status(run_id, "completed", pages_captured=captured)

        logger.info("Snapshot %s captured %d pages of %s", run_id, captured, website)
        return run_id

    except Exception as exc:
        logger.error("Snapshot %s of %s failed: %s", run_id, website, exc)
        await _set_status(run_id, "failed", error=str(exc))
        raise


async def compare_runs(before_run_id: str, after_run_id: str) -> dict:
    """
    Diff two snapshot runs.

    Pages are matched by URL. Unchanged pages are counted rather than listed --
    on a 545-page site, listing them would bury the handful that actually
    changed. The stored content hash short-circuits those before any field
    comparison runs.
    """
    async with AsyncSessionLocal() as db:
        before_pages = (await db.execute(
            select(PageSnapshot).where(PageSnapshot.run_id == before_run_id)
        )).scalars().all()
        after_pages = (await db.execute(
            select(PageSnapshot).where(PageSnapshot.run_id == after_run_id)
        )).scalars().all()

    before_by_url = {p.url: p for p in before_pages}
    after_by_url = {p.url: p for p in after_pages}

    removed = sorted(set(before_by_url) - set(after_by_url))
    added = sorted(set(after_by_url) - set(before_by_url))

    changed = []
    unchanged = 0
    all_changes = []

    for url in sorted(set(before_by_url) & set(after_by_url)):
        old, new = before_by_url[url], after_by_url[url]

        if old.content_hash and old.content_hash == new.content_hash:
            unchanged += 1
            continue

        page_changes = diff_snapshots(old.to_fields(), new.to_fields())
        if not page_changes:
            unchanged += 1
            continue

        all_changes.extend(page_changes)
        changed.append({
            "url": url,
            "summary": summarize(page_changes),
            "changes": page_changes,
        })

    # Worst-hit pages first, so triage starts where the damage is.
    changed.sort(key=lambda p: (-p["summary"]["high"], -p["summary"]["total"]))

    return {
        "before_run_id": before_run_id,
        "after_run_id": after_run_id,
        "changed": changed,
        "added": added,
        "removed": removed,
        "unchanged_count": unchanged,
        "summary": summarize(all_changes),
    }
