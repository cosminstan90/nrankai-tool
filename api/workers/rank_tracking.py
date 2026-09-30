"""
Google rank observations for visibility trackers (Etapa 8 of
docs/IMPROVEMENTS_PLAN.md).

Fed by the SERP the google_aio provider already fetches during a scan: one
DataForSEO call now answers both "is there an AI Overview, does it cite us?"
and "where do we rank?". Nothing here makes a network call of its own.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from api.models._base import AsyncSessionLocal
from api.models.database import SerpRankObservation, SerpOrganicResult, SerpAioReference
from core.serp_client import SerpResult, normalize_host

logger = logging.getLogger(__name__)


def build_observation(tracker_id: str, scan_id: Optional[str], website: str,
                      query: str, serp: SerpResult) -> dict:
    """The row to store for one query, as a plain dict (pure, testable)."""
    hit = serp.site_rank(website)
    return {
        "id": str(uuid.uuid4()),
        "tracker_id": tracker_id,
        "scan_id": scan_id,
        "query": query,
        "website": website,
        "location_code": serp.location_code,
        "language_code": serp.language_code,
        "depth": serp.depth,
        # How many organic results actually came back. A NULL rank means "not
        # among these", and this is what gives that statement its N.
        "results_count": len(serp.organic),
        "rank_group": hit.rank_group if hit else None,
        "rank_absolute": hit.rank_absolute if hit else None,
        "ranking_url": hit.url if hit else None,
        "is_featured_snippet": bool(hit and hit.is_featured_snippet),
        "aio_present": serp.ai_overview is not None,
        "aio_cites_site": serp.ai_overview_cites(website),
        "serp_features": list(serp.features),
        "observed_at": datetime.now(timezone.utc),
    }


async def record_observation(tracker_id: str, scan_id: Optional[str], website: str,
                             query: str, serp: SerpResult) -> None:
    """
    Store one observation in its own short-lived session, committed at once.

    Deliberately NOT added to the visibility scan's own session. That session
    stays open for the whole scan; pending rows in it would hold SQLite's
    single write lock for minutes. (Under the old StaticPool engine they were
    silently discarded instead -- that lost all 545 rows of the first real
    snapshot capture in Etapa 6.)

    Never raises: a failed observation must not fail the scan it rides on.
    """
    row = build_observation(tracker_id, scan_id, website, query, serp)
    try:
        async with AsyncSessionLocal() as db:
            db.add(SerpRankObservation(**row))
            # Force the parent row's INSERT before any child references it by
            # id below -- these are plain FK columns, not an ORM relationship(),
            # so nothing else guarantees insert order across the three tables.
            await db.flush()

            # The rest of this same, already-paid-for SERP -- competitors'
            # positions and whoever the AI Overview cites instead of us.
            # Pasul 8 of docs/superpowers/plans/2026-09-30-next-steps.md.
            for result in serp.organic:
                db.add(SerpOrganicResult(
                    observation_id=row["id"],
                    rank_group=result.rank_group,
                    rank_absolute=result.rank_absolute,
                    domain=normalize_host(result.domain),
                    url=result.url,
                    title=result.title,
                ))
            if serp.ai_overview:
                for position, ref in enumerate(serp.ai_overview.get("references") or [], start=1):
                    db.add(SerpAioReference(
                        observation_id=row["id"],
                        position=position,
                        domain=normalize_host(ref.get("domain") or ref.get("url") or ""),
                        url=ref.get("url"),
                        title=ref.get("title"),
                    ))

            await db.commit()
    except Exception as exc:
        logger.warning("Could not record rank observation for %r: %s", query, exc)
