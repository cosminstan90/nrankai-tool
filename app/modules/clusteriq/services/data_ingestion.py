"""
ClusterIQ — Data Ingestion Services
=====================================
Pulls keyword/URL performance data from two sources:

  GSCIngestionService        — Google Search Console Search Analytics API
                               (page × query matrix, dimensions=['page','query'])
  DataForSEOIngestionService — DataForSEO SERP organic/live/advanced endpoint
                               (top-20 organic URLs per keyword, location-aware)

Both services are driven directly by app/modules/clusteriq/tasks/pipeline.py
(run_clusteriq_pipeline), the real end-to-end flow: GSC ingestion -> sync
clusteriq_urls from SERP data -> DataForSEO SERP -> clustering -> KUCD
decisions, with its own fine-grained status vocabulary (ingesting_gsc /
ingesting_serp / clustering / deciding / done / error).

GSC auth reuses the shared GoogleOAuthToken row (one connected Google account).
Token refresh is delegated to api.workers.contentiq.gsc.refresh_access_token so
we don't duplicate the httpx-based refresh logic.

DataForSEO concurrency is capped at 10 simultaneous requests via asyncio.Semaphore.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.models._base import AsyncSessionLocal
from api.models.clusteriq import CluProject, CluSerpData

# Reuse token-refresh logic from ContentIQ without duplicating it
from api.workers.contentiq.gsc import refresh_access_token, GSCAuthError

logger = logging.getLogger("clusteriq.ingestion")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GSC_ENDPOINT   = "https://searchconsole.googleapis.com/v1/sites/{property}/searchAnalytics/query"
_DFS_SERP_URL   = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
_GSC_ROW_LIMIT  = 25_000          # max rows per GSC API page
_DFS_SERP_DEPTH = 20              # top-N organic URLs to extract per keyword
_DFS_COST_PER_TASK = 0.003        # USD estimate per live/advanced task (update per plan)
_DFS_SEMAPHORE     = 10           # max concurrent DataForSEO requests


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dfs_auth_header() -> str:
    login = os.getenv("DATAFORSEO_LOGIN", "")
    pw    = os.getenv("DATAFORSEO_PASSWORD", "")
    return "Basic " + base64.b64encode(f"{login}:{pw}".encode()).decode()


async def _load_gsc_token():
    """Return the stored GoogleOAuthToken row, or None."""
    from api.models.database import GoogleOAuthToken
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(GoogleOAuthToken).limit(1))
        return result.scalar_one_or_none()


async def _get_valid_access_token() -> str:
    """
    Load the stored GSC token and refresh it if expired (within 5 min buffer).
    Raises GSCAuthError if no token is stored or refresh fails.
    """
    token_row = await _load_gsc_token()
    if not token_row:
        raise GSCAuthError("No Google account connected — complete OAuth flow first")

    now = datetime.now(timezone.utc)
    expiry = token_row.token_expiry
    if expiry is not None:
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry < now + timedelta(minutes=5):
            if not token_row.refresh_token:
                raise GSCAuthError("GSC token expired and no refresh_token available")
            new_token = await refresh_access_token(token_row.refresh_token)
            # Persist updated token
            from api.models.database import GoogleOAuthToken
            async with AsyncSessionLocal() as db:
                row = (await db.execute(select(GoogleOAuthToken).limit(1))).scalar_one_or_none()
                if row:
                    row.access_token = new_token
                    row.token_expiry = now + timedelta(hours=1)
                    row.updated_at   = now
                    await db.commit()
            return new_token

    return token_row.access_token


# ---------------------------------------------------------------------------
# Service 1 — GSC Ingestion
# ---------------------------------------------------------------------------

class GSCIngestionService:
    """
    Fetches URL × keyword performance data from Google Search Console and
    stores it in clusteriq_serp_data with data_source='gsc'.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_url_keyword_matrix(
        self,
        project_id: int,
        gsc_property: str,
        date_range_days: int = 90,
    ) -> int:
        """
        Pull the full page × query matrix from GSC Search Analytics and upsert
        results into clusteriq_serp_data.

        Only rows with impressions > 0 are returned by the API by default.
        Pagination is handled automatically (25 000 rows per page).

        Returns the number of rows upserted.
        """
        access_token = await _get_valid_access_token()
        rows = await self._fetch_all_pages(access_token, gsc_property, date_range_days)

        if not rows:
            logger.info("[GSC] project=%d — no data returned for %s", project_id, gsc_property)
            return 0

        upserted = await self._save_serp_rows(project_id, rows, data_source="gsc")
        logger.info(
            "[GSC] project=%d — fetched %d rows, upserted %d into clusteriq_serp_data",
            project_id, len(rows), upserted,
        )
        return upserted

    async def get_top_queries_for_url(
        self,
        project_id: int,
        url: str,
        limit: int = 50,
    ) -> list[dict]:
        """
        Return queries driving traffic to a specific URL, sorted by impressions DESC.
        Data is read from the already-ingested clusteriq_serp_data table.
        """
        async with AsyncSessionLocal() as db:
            stmt = (
                select(CluSerpData)
                .where(
                    CluSerpData.project_id == project_id,
                    CluSerpData.url == url,
                )
                .order_by(CluSerpData.impressions.desc().nullslast())
                .limit(limit)
            )
            results = (await db.execute(stmt)).scalars().all()
        return [r.to_dict() for r in results]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_all_pages(
        self,
        access_token: str,
        gsc_property: str,
        date_range_days: int,
    ) -> list[dict]:
        """Paginate through GSC Search Analytics with dimensions=[page, query]."""
        end_date   = date.today()
        start_date = end_date - timedelta(days=date_range_days)
        endpoint   = _GSC_ENDPOINT.format(property=quote(gsc_property, safe=""))
        headers    = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type":  "application/json",
        }

        all_rows: list[dict] = []
        start_row = 0

        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                body = {
                    "startDate":  start_date.isoformat(),
                    "endDate":    end_date.isoformat(),
                    "dimensions": ["page", "query"],
                    "rowLimit":   _GSC_ROW_LIMIT,
                    "startRow":   start_row,
                }
                try:
                    resp = await client.post(endpoint, json=body, headers=headers)
                except httpx.RequestError as exc:
                    logger.error("[GSC] Network error: %s", exc)
                    break

                if resp.status_code == 401:
                    raise GSCAuthError("GSC 401 — token rejected")
                if resp.status_code != 200:
                    logger.error("[GSC] HTTP %d: %s", resp.status_code, resp.text[:200])
                    break

                data = resp.json()
                page_rows = data.get("rows", [])
                if not page_rows:
                    break

                for row in page_rows:
                    keys = row.get("keys") or []
                    if len(keys) < 2:
                        continue
                    all_rows.append({
                        "url":         keys[0],
                        "keyword":     keys[1],
                        "clicks":      int(row.get("clicks",      0)),
                        "impressions": int(row.get("impressions", 0)),
                        "position":    round(float(row.get("position", 0.0)), 1),
                    })

                if len(page_rows) < _GSC_ROW_LIMIT:
                    break
                start_row += _GSC_ROW_LIMIT

        return all_rows

    async def _save_serp_rows(
        self,
        project_id: int,
        rows: list[dict],
        data_source: str,
    ) -> int:
        """Upsert GSC rows into clusteriq_serp_data. Returns count of rows processed."""
        now = datetime.now(timezone.utc)
        upserted = 0

        # Batch upserts in chunks of 500 to avoid huge transactions
        CHUNK = 500
        async with AsyncSessionLocal() as db:
            for i in range(0, len(rows), CHUNK):
                chunk = rows[i : i + CHUNK]
                values = [
                    {
                        "project_id":  project_id,
                        "keyword":     r["keyword"],
                        "url":         r["url"],
                        "position":    r.get("position"),
                        "impressions": r.get("impressions"),
                        "clicks":      r.get("clicks"),
                        "serp_urls":   None,   # populated by DataForSEO later
                        "data_source": data_source,
                        "fetched_at":  now,
                    }
                    for r in chunk
                ]
                stmt = sqlite_insert(CluSerpData).values(values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "keyword", "url"],
                    set_={
                        "position":    stmt.excluded.position,
                        "impressions": stmt.excluded.impressions,
                        "clicks":      stmt.excluded.clicks,
                        "fetched_at":  stmt.excluded.fetched_at,
                    },
                )
                await db.execute(stmt)
                upserted += len(chunk)

            await db.commit()

        return upserted


# ---------------------------------------------------------------------------
# Service 2 — DataForSEO SERP Ingestion
# ---------------------------------------------------------------------------

class DataForSEOIngestionService:
    """
    Fetches organic SERP results from DataForSEO and stores the top-20 URLs
    per keyword in clusteriq_serp_data.serp_urls.
    """

    def __init__(
        self,
        api_login: str | None = None,
        api_password: str | None = None,
    ) -> None:
        self._login    = api_login    or os.getenv("DATAFORSEO_LOGIN", "")
        self._password = api_password or os.getenv("DATAFORSEO_PASSWORD", "")
        if not (self._login and self._password):
            raise ValueError(
                "DataForSEO credentials missing — set DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD"
            )
        self._auth = "Basic " + base64.b64encode(
            f"{self._login}:{self._password}".encode()
        ).decode()
        self._semaphore = asyncio.Semaphore(_DFS_SEMAPHORE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_serp_for_keywords(
        self,
        project_id: int,
        keywords: list[str],
        location_code: int = 2040,   # 2040 = Romania
    ) -> dict[str, Any]:
        """
        Fetch organic top-20 SERP URLs for each keyword and store them in
        clusteriq_serp_data.serp_urls.  Also increments
        clusteriq_projects.dataforseo_credits_used.

        Returns a summary dict: {fetched, failed, credits_used}.
        """
        if not keywords:
            return {"fetched": 0, "failed": 0, "credits_used": 0}

        tasks_list = [
            self._fetch_one_keyword(project_id, kw, location_code)
            for kw in keywords
        ]
        results = await asyncio.gather(*tasks_list, return_exceptions=True)

        fetched = sum(1 for r in results if r is True)
        failed  = sum(1 for r in results if r is not True)

        # Increment project credit counter
        credits_this_run = fetched   # 1 credit per successful task
        await self._increment_credits(project_id, credits_this_run)

        logger.info(
            "[DataForSEO] project=%d — fetched=%d failed=%d credits_used=%d",
            project_id, fetched, failed, credits_this_run,
        )
        return {"fetched": fetched, "failed": failed, "credits_used": credits_this_run}

    def estimate_cost(self, keyword_count: int) -> float:
        """Return estimated USD cost for fetching SERP data for N keywords."""
        return round(keyword_count * _DFS_COST_PER_TASK, 4)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_one_keyword(
        self,
        project_id: int,
        keyword: str,
        location_code: int,
    ) -> bool:
        """
        Fetch SERP for a single keyword, extract top-20 organic URLs, persist.
        Returns True on success, False on failure.
        """
        async with self._semaphore:
            payload = [{
                "keyword":       keyword,
                "location_code": location_code,
                "device":        "desktop",
                "os":            "windows",
                "depth":         _DFS_SERP_DEPTH,
            }]
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        _DFS_SERP_URL,
                        headers={
                            "Authorization": self._auth,
                            "Content-Type":  "application/json",
                        },
                        json=payload,
                    )
                data = resp.json()
            except Exception as exc:
                logger.warning("[DataForSEO] keyword=%r request error: %s", keyword, exc)
                return False

            serp_urls = self._extract_organic_urls(data)
            if serp_urls is None:
                # API-level error already logged
                return False

            await self._save_serp_urls(project_id, keyword, serp_urls)
            return True

    def _extract_organic_urls(self, data: dict) -> list[str] | None:
        """
        Walk the DataForSEO response and collect organic result URLs.
        Returns None if the API reported an error task.
        """
        for task in data.get("tasks", []):
            if task.get("status_code") != 20000:
                logger.warning(
                    "[DataForSEO] task error %s: %s",
                    task.get("status_code"),
                    task.get("status_message"),
                )
                return None
            for result_item in task.get("result") or []:
                urls: list[str] = []
                for item in result_item.get("items") or []:
                    if item.get("type") == "organic":
                        url = item.get("url") or item.get("domain")
                        if url:
                            urls.append(url)
                        if len(urls) >= _DFS_SERP_DEPTH:
                            break
                return urls   # first result_item is enough
        return []

    async def _save_serp_urls(
        self,
        project_id: int,
        keyword: str,
        serp_urls: list[str],
    ) -> None:
        """
        Upsert serp_urls into clusteriq_serp_data.
        If a row for (project_id, keyword, url) already exists we update serp_urls.
        If no row exists at all (keyword came from DataForSEO, not GSC), we insert one
        using the primary URL (first SERP result) as the url value.
        """
        now = datetime.now(timezone.utc)
        if not serp_urls:
            return

        async with AsyncSessionLocal() as db:
            # Update all existing GSC rows for this keyword with the serp_urls list
            await db.execute(
                update(CluSerpData)
                .where(
                    CluSerpData.project_id == project_id,
                    CluSerpData.keyword    == keyword,
                )
                .values(serp_urls=serp_urls, fetched_at=now)
            )

            # If no GSC row existed, insert a synthetic row so serp_urls are not lost
            existing = (await db.execute(
                select(CluSerpData).where(
                    CluSerpData.project_id == project_id,
                    CluSerpData.keyword    == keyword,
                ).limit(1)
            )).scalar_one_or_none()

            if existing is None:
                stmt = sqlite_insert(CluSerpData).values(
                    project_id  = project_id,
                    keyword     = keyword,
                    url         = serp_urls[0],   # first organic result as canonical URL
                    data_source = "dataforseo",
                    serp_urls   = serp_urls,
                    fetched_at  = now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "keyword", "url"],
                    set_={"serp_urls": stmt.excluded.serp_urls, "fetched_at": stmt.excluded.fetched_at},
                )
                await db.execute(stmt)

            await db.commit()

    async def _increment_credits(self, project_id: int, count: int) -> None:
        if count <= 0:
            return
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(CluProject)
                .where(CluProject.id == project_id)
                .values(dataforseo_credits_used=CluProject.dataforseo_credits_used + count)
            )
            await db.commit()

