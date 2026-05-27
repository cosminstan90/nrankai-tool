"""
ClusterIQ — Competitor Analysis Service
========================================
Fetches organic keywords for a competitor domain via Ahrefs v4, maps them
against the project's SERP data to classify overlap, and generates "Create"
decisions for high-volume keywords the site is missing entirely.

Usage
-----
    svc = CompetitorClusterAnalyzer()
    await svc.run_full_analysis(project_id, "competitor.com", keyword_limit=1000, job_id=42)

Or call steps individually:
    kws     = await svc.fetch_competitor_keywords(project_id, "competitor.com", limit=1000)
    report  = await svc.map_competitor_to_clusters(project_id, kws)
    n       = await svc.create_gap_decisions(project_id, report, "competitor.com")
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from api.models._base import AsyncSessionLocal
from api.models.clusteriq import (
    CluCluster,
    CluCompetitorCache,
    CluCompetitorJob,
    CluDecision,
    CluProject,
    CluSerpData,
)

logger = logging.getLogger("clusteriq.competitor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_TTL_HOURS      = 24     # hours before a re-fetch is triggered
_CREATE_VOL_THRESHOLD = 100    # min monthly volume to generate a Create decision
_AHREFS_CREDITS_PER_K = 0.5   # rough estimate: credits per 1000 keywords fetched

# CTR curve for impression-share estimation (position → organic CTR)
_CTR_CURVE: dict[int, float] = {
    1: 0.280, 2: 0.150, 3: 0.110, 4: 0.080, 5: 0.065,
    6: 0.050, 7: 0.040, 8: 0.035, 9: 0.030, 10: 0.025,
}
_CTR_DEFAULT = 0.010  # positions 11+


def _ctr(position: float | None) -> float:
    if position is None:
        return _CTR_DEFAULT
    p = max(1, round(position))
    return _CTR_CURVE.get(p, _CTR_DEFAULT)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class CompetitorClusterAnalyzer:
    """All methods are async; each opens its own DB session."""

    # ── Public API ──────────────────────────────────────────────────────────

    async def fetch_competitor_keywords(
        self,
        project_id:        int,
        competitor_domain: str,
        limit:             int = 1000,
        job_id:            Optional[int] = None,
    ) -> list[dict]:
        """
        Return organic keyword rows for *competitor_domain*.

        Checks the DB cache first; only calls Ahrefs when the cache is absent
        or older than _CACHE_TTL_HOURS.  Upserts fresh rows on fetch.
        """
        # ── 1. Check cache freshness ────────────────────────────────────────
        cached = await self._load_from_cache(project_id, competitor_domain)
        if cached:
            logger.info(
                "[Competitor] project=%d domain=%s — serving %d rows from cache",
                project_id, competitor_domain, len(cached),
            )
            return cached

        # ── 2. Fetch from Ahrefs ────────────────────────────────────────────
        logger.info(
            "[Competitor] project=%d domain=%s — fetching up to %d keywords from Ahrefs",
            project_id, competitor_domain, limit,
        )
        try:
            from api.workers.contentiq.ahrefs import get_client
            client = get_client()
        except RuntimeError:
            logger.warning("[Competitor] AHREFS_API_KEY not set — skipping fetch")
            return []

        rows = await client.get_domain_organic_keywords(competitor_domain, limit=limit)
        logger.info(
            "[Competitor] project=%d domain=%s — Ahrefs returned %d rows",
            project_id, competitor_domain, len(rows),
        )

        if not rows:
            return []

        # ── 3. Upsert to cache ──────────────────────────────────────────────
        now    = datetime.now(timezone.utc)
        values = [
            {
                "project_id":        project_id,
                "competitor_domain": competitor_domain,
                "keyword":           r["keyword"],
                "competitor_url":    r.get("url", ""),
                "position":          r.get("position"),
                "volume":            r.get("volume", 0),
                "fetched_at":        now,
            }
            for r in rows
            if r.get("keyword")
        ]
        CHUNK = 500
        async with AsyncSessionLocal() as db:
            # Wipe previous cache for this project/domain before re-inserting
            await db.execute(
                delete(CluCompetitorCache).where(
                    CluCompetitorCache.project_id        == project_id,
                    CluCompetitorCache.competitor_domain == competitor_domain,
                )
            )
            for i in range(0, len(values), CHUNK):
                stmt = sqlite_insert(CluCompetitorCache).values(values[i : i + CHUNK])
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "competitor_domain", "keyword"],
                    set_={
                        "competitor_url": stmt.excluded.competitor_url,
                        "position":       stmt.excluded.position,
                        "volume":         stmt.excluded.volume,
                        "fetched_at":     stmt.excluded.fetched_at,
                    },
                )
                await db.execute(stmt)
            await db.commit()

        # ── 4. Update job counter ───────────────────────────────────────────
        if job_id is not None:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(CluCompetitorJob)
                    .where(CluCompetitorJob.id == job_id)
                    .values(keywords_fetched=len(rows))
                )
                await db.commit()

        return [
            {
                "keyword":          r["keyword"],
                "competitor_url":   r.get("url", ""),
                "position":         r.get("position"),
                "volume":           r.get("volume", 0),
            }
            for r in rows
        ]

    async def map_competitor_to_clusters(
        self,
        project_id:           int,
        competitor_keywords:  list[dict],
    ) -> dict:
        """
        Join competitor keywords against the project's SERP data.

        Returns the raw gap-report dict (without impression share — call
        generate_gap_report for the full enriched version).
        """
        if not competitor_keywords:
            return _empty_gap_report()

        kw_set = {r["keyword"].lower(): r for r in competitor_keywords}

        # Load project SERP data (keyword → your best position + impressions)
        async with AsyncSessionLocal() as db:
            our_rows = (await db.execute(
                select(
                    CluSerpData.keyword,
                    func.min(CluSerpData.position).label("best_position"),
                    func.sum(CluSerpData.impressions).label("total_impressions"),
                )
                .where(CluSerpData.project_id == project_id)
                .group_by(CluSerpData.keyword)
            )).all()

        our_kws: dict[str, dict] = {
            r.keyword.lower(): {
                "position":    r.best_position,
                "impressions": r.total_impressions or 0,
            }
            for r in our_rows
        }

        # Load cluster labels for context
        async with AsyncSessionLocal() as db:
            cluster_rows = (await db.execute(
                select(CluCluster.id, CluCluster.cluster_label, CluCluster.primary_keyword)
                .where(CluCluster.project_id == project_id)
            )).all()

        # Simple keyword → cluster_id lookup via primary_keyword
        kw_to_cluster: dict[str, dict] = {
            (r.primary_keyword or "").lower(): {"cluster_id": r.id, "cluster_label": r.cluster_label}
            for r in cluster_rows
            if r.primary_keyword
        }

        win_list:     list[dict] = []
        lose_list:    list[dict] = []
        missing_list: list[dict] = []

        for kw_lower, comp_row in kw_set.items():
            their_pos = comp_row.get("position")
            volume    = comp_row.get("volume", 0) or 0
            comp_url  = comp_row.get("competitor_url", "")
            keyword   = comp_row["keyword"]

            if kw_lower in our_kws:
                our_pos = our_kws[kw_lower]["position"]
                cluster_meta = kw_to_cluster.get(kw_lower, {})
                row = {
                    "keyword":        keyword,
                    "your_position":  our_pos,
                    "their_position": their_pos,
                    "volume":         volume,
                    "cluster_id":     cluster_meta.get("cluster_id"),
                    "cluster_label":  cluster_meta.get("cluster_label", ""),
                }
                if our_pos and their_pos and our_pos < their_pos:
                    win_list.append(row)
                else:
                    lose_list.append(row)
            else:
                missing_list.append({
                    "keyword":          keyword,
                    "competitor_url":   comp_url,
                    "volume":           volume,
                    "their_position":   their_pos,
                    "suggested_action": "Create",
                })

        # Sort by volume desc within each bucket
        for lst in (win_list, lose_list, missing_list):
            lst.sort(key=lambda x: x.get("volume", 0) or 0, reverse=True)

        return {
            "clusters_you_win":          win_list,
            "clusters_you_lose":         lose_list,
            "clusters_missing_entirely": missing_list,
        }

    async def generate_gap_report(
        self,
        project_id:        int,
        competitor_domain: str,
    ) -> dict:
        """
        Full enriched gap report with impression-share calculation.
        Reads entirely from DB cache — call fetch_competitor_keywords first.
        """
        cached = await self._load_from_cache(project_id, competitor_domain)
        if not cached:
            return {**_empty_gap_report(), "status": "no_data"}

        raw = await self.map_competitor_to_clusters(project_id, cached)
        if not raw:
            return {**_empty_gap_report(), "status": "no_data"}

        # Impression share via CTR model over win + lose sets
        est_you  = 0.0
        est_them = 0.0
        for row in raw["clusters_you_win"] + raw["clusters_you_lose"]:
            vol   = row.get("volume", 0) or 0
            y_pos = row.get("your_position")
            t_pos = row.get("their_position")
            est_you  += vol * _ctr(y_pos)
            est_them += vol * _ctr(t_pos)

        total_est = est_you + est_them
        if total_est > 0:
            share_you  = round(est_you  / total_est * 100, 1)
            share_them = round(est_them / total_est * 100, 1)
        else:
            share_you  = 0.0
            share_them = 0.0

        # Fetch cache timestamp
        async with AsyncSessionLocal() as db:
            latest_fetch = (await db.execute(
                select(func.max(CluCompetitorCache.fetched_at))
                .where(
                    CluCompetitorCache.project_id        == project_id,
                    CluCompetitorCache.competitor_domain == competitor_domain,
                )
            )).scalar()

        return {
            "competitor_domain":          competitor_domain,
            "status":                     "done",
            "clusters_you_win":           raw["clusters_you_win"],
            "clusters_you_lose":          raw["clusters_you_lose"],
            "clusters_missing_entirely":  raw["clusters_missing_entirely"],
            "impression_share_you":       share_you,
            "impression_share_competitor": share_them,
            "total_overlap_keywords":     len(raw["clusters_you_win"]) + len(raw["clusters_you_lose"]),
            "total_missing_keywords":     len(raw["clusters_missing_entirely"]),
            "fetched_at":                 latest_fetch.isoformat() if latest_fetch else None,
        }

    async def create_gap_decisions(
        self,
        project_id:        int,
        gap_report:        dict,
        competitor_domain: str,
        volume_threshold:  int = _CREATE_VOL_THRESHOLD,
    ) -> int:
        """
        For each high-volume keyword the site is missing entirely:
          1. Create a placeholder CluCluster (url_count=0, no URL members).
          2. Attach a CluDecision(verdict="Create") to it.

        Returns the number of decisions inserted.
        """
        missing = [
            m for m in gap_report.get("clusters_missing_entirely", [])
            if (m.get("volume") or 0) >= volume_threshold
        ]
        if not missing:
            return 0

        now     = datetime.now(timezone.utc)
        created = 0

        async with AsyncSessionLocal() as db:
            for m in missing:
                keyword     = m["keyword"]
                comp_url    = m.get("competitor_url", "")
                volume      = m.get("volume", 0) or 0
                their_pos   = m.get("their_position")

                # Create placeholder cluster
                cluster = CluCluster(
                    project_id              = project_id,
                    cluster_label           = keyword,
                    primary_keyword         = keyword,
                    primary_url             = None,
                    url_count               = 0,
                    total_impressions       = 0,
                    search_demand_confirmed = False,
                    created_at              = now,
                )
                db.add(cluster)
                await db.flush()   # get cluster.id

                pos_str = f"#{round(their_pos, 1)}" if their_pos else "o pozitie buna"
                vol_str = f"{volume:,}" if volume else "necunoscut"
                evidence = [
                    (
                        f"Competitor {competitor_domain} rankheaza pe {pos_str} pentru "
                        f"'{keyword}' ({vol_str} cautari/luna)."
                    ),
                    "Nu ai nicio pagina care sa serveasca aceasta cerere.",
                    (f"URL competitor: {comp_url}" if comp_url else "URL competitor necunoscut."),
                ]

                decision = CluDecision(
                    cluster_id   = cluster.id,
                    url_id       = None,
                    verdict      = "Create",
                    evidence     = evidence,
                    confidence   = 0.85,
                    generated_by = "competitor_gap",
                    created_at   = now,
                )
                db.add(decision)
                created += 1

            await db.commit()

        logger.info(
            "[Competitor] project=%d domain=%s — created %d gap decisions (threshold=%d)",
            project_id, competitor_domain, created, volume_threshold,
        )
        return created

    async def run_full_analysis(
        self,
        project_id:        int,
        competitor_domain: str,
        keyword_limit:     int = 1000,
        job_id:            Optional[int] = None,
    ) -> None:
        """
        Full pipeline: fetch → gap report → create decisions → mark job done.
        Designed to run as a background coroutine via create_tracked_task().
        """
        async def _set_job(status: str, **kwargs) -> None:
            if job_id is None:
                return
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(CluCompetitorJob)
                    .where(CluCompetitorJob.id == job_id)
                    .values(status=status, **kwargs)
                )
                await db.commit()

        try:
            await _set_job("running", started_at=datetime.now(timezone.utc))

            kws = await self.fetch_competitor_keywords(
                project_id, competitor_domain,
                limit=keyword_limit, job_id=job_id,
            )

            gap_report = await self.map_competitor_to_clusters(project_id, kws)

            n_created = await self.create_gap_decisions(
                project_id, gap_report, competitor_domain
            )

            await _set_job(
                "done",
                keywords_fetched       = len(kws),
                gap_keywords_found     = len(gap_report.get("clusters_missing_entirely", [])),
                create_decisions_added = n_created,
                completed_at           = datetime.now(timezone.utc),
            )
            logger.info(
                "[Competitor] project=%d domain=%s — analysis complete: "
                "%d kws fetched, %d missing, %d Create decisions",
                project_id, competitor_domain, len(kws),
                len(gap_report.get("clusters_missing_entirely", [])), n_created,
            )

        except Exception as exc:
            logger.exception(
                "[Competitor] project=%d domain=%s — unhandled error: %s",
                project_id, competitor_domain, exc,
            )
            await _set_job(
                "error",
                error_message=str(exc)[:500],
                completed_at=datetime.now(timezone.utc),
            )

    # ── Helpers ─────────────────────────────────────────────────────────────

    async def _load_from_cache(
        self, project_id: int, competitor_domain: str
    ) -> list[dict]:
        """Return cached rows if present and younger than _CACHE_TTL_HOURS; else []."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_CACHE_TTL_HOURS)
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(CluCompetitorCache)
                .where(
                    CluCompetitorCache.project_id        == project_id,
                    CluCompetitorCache.competitor_domain == competitor_domain,
                    CluCompetitorCache.fetched_at        >= cutoff,
                )
                .order_by(CluCompetitorCache.volume.desc().nullslast())
            )).scalars().all()
        if not rows:
            return []
        return [
            {
                "keyword":        r.keyword,
                "competitor_url": r.competitor_url or "",
                "position":       r.position,
                "volume":         r.volume or 0,
            }
            for r in rows
        ]


def _empty_gap_report() -> dict:
    return {
        "clusters_you_win":           [],
        "clusters_you_lose":          [],
        "clusters_missing_entirely":  [],
        "impression_share_you":       0.0,
        "impression_share_competitor": 0.0,
        "total_overlap_keywords":     0,
        "total_missing_keywords":     0,
        "fetched_at":                 None,
    }


def estimate_ahrefs_credits(keyword_limit: int) -> float:
    """Conservative credit estimate for keyword_limit Ahrefs rows."""
    batches = max(1, keyword_limit / 1000)
    return round(batches * _AHREFS_CREDITS_PER_K, 2)
