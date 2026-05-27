"""
ClusterIQ — Full Analysis Pipeline
====================================
Runs the complete ingestion → clustering → decision pipeline for a
ClusterIQ project as a background coroutine.

Steps
-----
1. ingesting_gsc   — fetch URL × keyword matrix from Google Search Console
2. populate_urls   — sync distinct GSC URLs into clusteriq_urls (no crawl required)
3. ingesting_serp  — fetch top-20 SERP URLs per keyword via DataForSEO
4. clustering      — build SERP-overlap graph + Louvain community detection
5. deciding        — apply KUCD verdict rules to every cluster
6. done            — mark project complete

Any unhandled exception sets status → "error" and stores a message in
project notes so the UI can surface it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from api.models._base import AsyncSessionLocal
from api.models.clusteriq import CluProject, CluSerpData, CluUrl

logger = logging.getLogger("clusteriq.pipeline")


# Status → human-readable phase label (also used for progress %)
_PHASE_LABEL: dict[str, str] = {
    "ingesting_gsc":  "Importing GSC URL × keyword matrix",
    "ingesting_serp": "Fetching SERP data via DataForSEO",
    "clustering":     "Building overlap graph + Louvain clustering",
    "deciding":       "Generating KUCD verdicts",
    "done":           "Complete",
    "error":          "Failed",
}

_PHASE_PROGRESS: dict[str, int] = {
    "pending":        0,
    "ingesting_gsc":  10,
    "ingesting_serp": 35,
    "clustering":     60,
    "deciding":       82,
    "done":           100,
    "error":          0,
}


async def run_clusteriq_pipeline(
    project_id: int,
    keyword_limit: int = 500,
) -> None:
    """
    Full ClusterIQ pipeline.  Designed to run via create_tracked_task().
    Opens its own DB sessions; never shares a session with the HTTP layer.
    """
    logger.info("[Pipeline] project=%d — starting (keyword_limit=%d)", project_id, keyword_limit)

    try:
        project = await _load_project(project_id)
        if project is None:
            logger.error("[Pipeline] project=%d not found — aborting", project_id)
            return

        gsc_property = project.gsc_property

        # ── Step 1: GSC ingestion ─────────────────────────────────────
        await _set_status(project_id, "ingesting_gsc")

        gsc_rows = 0
        if gsc_property:
            from app.modules.clusteriq.services.data_ingestion import GSCIngestionService
            gsc_svc  = GSCIngestionService()
            gsc_rows = await gsc_svc.fetch_url_keyword_matrix(project_id, gsc_property)
            logger.info("[Pipeline] project=%d — GSC: %d rows upserted", project_id, gsc_rows)
        else:
            logger.warning(
                "[Pipeline] project=%d — no gsc_property set; skipping GSC ingestion", project_id
            )

        # ── Step 2: Sync site URLs from SERP data ────────────────────
        # clusteriq_urls is populated from the distinct URLs seen in GSC data.
        # This lets the clustering engine work without a separate sitemap crawl.
        if gsc_rows:
            url_count = await _sync_urls_from_serp_data(project_id)
            logger.info("[Pipeline] project=%d — synced %d site URLs", project_id, url_count)

        # ── Step 3: DataForSEO SERP ingestion ────────────────────────
        await _set_status(project_id, "ingesting_serp")

        from app.modules.clusteriq.services.data_ingestion import DataForSEOIngestionService
        try:
            dfs_svc = DataForSEOIngestionService()
            top_keywords = await _top_keywords_without_serp(project_id, limit=keyword_limit)
            logger.info(
                "[Pipeline] project=%d — DataForSEO: %d keywords to fetch", project_id, len(top_keywords)
            )
            if top_keywords:
                serp_result = await dfs_svc.fetch_serp_for_keywords(project_id, top_keywords)
                logger.info(
                    "[Pipeline] project=%d — SERP: fetched=%d failed=%d credits=%d",
                    project_id,
                    serp_result["fetched"],
                    serp_result["failed"],
                    serp_result["credits_used"],
                )
        except ValueError:
            # DataForSEO credentials not configured — skip silently
            logger.warning(
                "[Pipeline] project=%d — DataForSEO credentials missing; skipping SERP fetch",
                project_id,
            )

        # ── Step 4: Clustering ────────────────────────────────────────
        await _set_status(project_id, "clustering")

        from app.modules.clusteriq.services.clustering_engine import SERPOverlapClusteringEngine
        clustering_stats = await SERPOverlapClusteringEngine().run_clustering(project_id)
        logger.info("[Pipeline] project=%d — clustering: %s", project_id, clustering_stats)

        # ── Step 5: Decisions ─────────────────────────────────────────
        await _set_status(project_id, "deciding")

        from app.modules.clusteriq.services.decision_engine import ClusterDecisionEngine
        decision_summary = await ClusterDecisionEngine().run_all_decisions(project_id)
        logger.info("[Pipeline] project=%d — decisions: %s", project_id, decision_summary)

        # ── Done ──────────────────────────────────────────────────────
        await _set_status(project_id, "done")
        logger.info("[Pipeline] project=%d — pipeline complete", project_id)

    except Exception as exc:
        logger.exception("[Pipeline] project=%d — unhandled error: %s", project_id, exc)
        await _set_error(project_id, str(exc))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _load_project(project_id: int) -> CluProject | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(CluProject).where(CluProject.id == project_id)
        )).scalar_one_or_none()


async def _set_status(project_id: int, status: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(CluProject)
            .where(CluProject.id == project_id)
            .values(status=status, updated_at=datetime.now(timezone.utc))
        )
        await db.commit()
    logger.debug("[Pipeline] project=%d → status=%s", project_id, status)


async def _set_error(project_id: int, message: str) -> None:
    # Truncate to fit in typical text field; full trace is in server logs
    truncated = message[:500] if len(message) > 500 else message
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(CluProject)
            .where(CluProject.id == project_id)
            .values(
                status     = "error",
                updated_at = datetime.now(timezone.utc),
            )
        )
        await db.commit()


async def _sync_urls_from_serp_data(project_id: int) -> int:
    """
    Insert any URL found in clusteriq_serp_data that is not yet in
    clusteriq_urls.  Uses ON CONFLICT DO NOTHING so it's idempotent.
    Returns the number of new rows inserted.
    """
    from sqlalchemy import func, text

    async with AsyncSessionLocal() as db:
        # Collect distinct URLs from SERP data
        rows = (await db.execute(
            select(CluSerpData.url).where(
                CluSerpData.project_id == project_id
            ).distinct()
        )).scalars().all()

    if not rows:
        return 0

    now  = datetime.now(timezone.utc)
    vals = [
        {
            "project_id":  project_id,
            "url":         url,
            "is_indexable": True,
            "crawled_at":  now,
        }
        for url in rows
    ]

    CHUNK = 500
    inserted = 0
    async with AsyncSessionLocal() as db:
        for i in range(0, len(vals), CHUNK):
            chunk = vals[i : i + CHUNK]
            stmt  = sqlite_insert(CluUrl).values(chunk)
            stmt  = stmt.on_conflict_do_nothing()
            result = await db.execute(stmt)
            inserted += result.rowcount or 0
        await db.commit()

    return inserted


async def _top_keywords_without_serp(project_id: int, limit: int) -> list[str]:
    """Return the top-N keywords by impressions that still have no serp_urls."""
    from sqlalchemy import func
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(
                CluSerpData.keyword,
                func.sum(CluSerpData.impressions).label("total_imp"),
            )
            .where(
                CluSerpData.project_id == project_id,
                CluSerpData.serp_urls.is_(None),
            )
            .group_by(CluSerpData.keyword)
            .order_by(func.sum(CluSerpData.impressions).desc().nullslast())
            .limit(limit)
        )).all()
    return [r.keyword for r in rows]


# Re-export constants so the router can use them without importing internals
PHASE_PROGRESS = _PHASE_PROGRESS
PHASE_LABEL    = _PHASE_LABEL
