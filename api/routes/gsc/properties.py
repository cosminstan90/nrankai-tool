"""GSC property management, data upload, and query/page retrieval."""

import asyncio
import csv
import io
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy import delete as sql_delete, update as sql_update

from api.models.database import (
    AsyncSessionLocal,
    DATABASE_PATH,
    GscProperty,
    GscQueryRow,
    GscPageRow,
    GoogleOAuthToken,
    KeywordResult,
    KeywordSession,
    AuditResult,
    UrlGuide,
)
from api.routes.costs import track_cost

from api.models.database import (
    AsyncSessionLocal,
    DATABASE_PATH,
    GscProperty,
    GscQueryRow,
    GscPageRow,
    GoogleOAuthToken,
    KeywordResult,
    KeywordSession,
    AuditResult,
    UrlGuide,
)
from api.routes.costs import track_cost

from ._shared import _oauth_available, _load_token, _get_gsc_credentials

router = APIRouter(prefix="/api/gsc", tags=["gsc"])

# ── Pydantic models ───────────────────────────────────────────────────────────

class CreatePropertyRequest(BaseModel):
    name:     str
    site_url: str


class PageOptimizeRequest(BaseModel):
    url:               str
    provider:          str             = "anthropic"
    model:             str             = "claude-sonnet-4-5"
    audit_types:       List[str]       = ["SEO_AUDIT", "GEO_AUDIT"]
    selected_queries:  Optional[List[dict]] = None  # if provided, skip API fetch and use these
    page_content:      Optional[str]   = None        # if provided, skip scraping entirely



# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.post("/properties", status_code=201)
async def create_property(req: CreatePropertyRequest):
    """Create a new GSC property entry."""
    async with AsyncSessionLocal() as db:
        prop = GscProperty(
            id       = str(uuid.uuid4()),
            name     = req.name.strip(),
            site_url = req.site_url.strip(),
        )
        db.add(prop)
        await db.commit()
    return {"id": prop.id, "name": prop.name, "site_url": prop.site_url}


@router.get("/properties")
async def list_properties():
    """Return all GSC properties, newest first."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(GscProperty).order_by(GscProperty.created_at.desc())
        )).scalars().all()

    return [
        {
            "id":               p.id,
            "name":             p.name,
            "site_url":         p.site_url,
            "date_range_start": p.date_range_start,
            "date_range_end":   p.date_range_end,
            "total_queries":    p.total_queries,
            "total_pages":      p.total_pages,
            "created_at":       p.created_at.isoformat() if p.created_at else None,
            "updated_at":       p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in rows
    ]


@router.delete("/properties/{property_id}")
async def delete_property(property_id: str):
    """Delete a property and all its query/page rows (CASCADE)."""
    async with AsyncSessionLocal() as db:
        await db.execute(sql_delete(GscQueryRow).where(GscQueryRow.property_id == property_id))
        await db.execute(sql_delete(GscPageRow).where(GscPageRow.property_id == property_id))
        await db.execute(sql_delete(GscProperty).where(GscProperty.id == property_id))
        await db.commit()
    return {"success": True}


# ── CSV upload ────────────────────────────────────────────────────────────────

@router.post("/properties/{property_id}/upload")
async def upload_csv(property_id: str, file: UploadFile = File(...)):
    """
    Upload a GSC Performance CSV (queries OR pages).
    Report type is auto-detected from column headers.
    Replaces any previously uploaded data of that type for this property.
    """
    async with AsyncSessionLocal() as db:
        prop = await db.get(GscProperty, property_id)
        if not prop:
            raise HTTPException(status_code=404, detail="Property not found")

        content = await file.read()
        try:
            report_type, rows = _parse_gsc_csv(content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        if report_type == "queries":
            await db.execute(
                sql_delete(GscQueryRow).where(GscQueryRow.property_id == property_id)
            )
            db.add_all([
                GscQueryRow(
                    property_id = property_id,
                    query       = r["key"],
                    clicks      = r["clicks"],
                    impressions = r["impressions"],
                    ctr         = r["ctr"],
                    position    = r["position"],
                )
                for r in rows
            ])
            prop.total_queries = len(rows)

        else:  # pages
            await db.execute(
                sql_delete(GscPageRow).where(GscPageRow.property_id == property_id)
            )
            db.add_all([
                GscPageRow(
                    property_id = property_id,
                    page        = r["key"],
                    clicks      = r["clicks"],
                    impressions = r["impressions"],
                    ctr         = r["ctr"],
                    position    = r["position"],
                )
                for r in rows
            ])
            prop.total_pages = len(rows)

        prop.updated_at = datetime.utcnow()
        await db.commit()

    return {
        "report_type":   report_type,
        "rows_imported": len(rows),
        "property_id":   property_id,
    }


# ── Data query endpoints ──────────────────────────────────────────────────────

_QUERY_SORT = {
    "clicks_desc":      lambda: GscQueryRow.clicks.desc(),
    "clicks_asc":       lambda: GscQueryRow.clicks.asc(),
    "impressions_desc": lambda: GscQueryRow.impressions.desc(),
    "impressions_asc":  lambda: GscQueryRow.impressions.asc(),
    "position_asc":     lambda: GscQueryRow.position.asc(),
    "position_desc":    lambda: GscQueryRow.position.desc(),
    "ctr_desc":         lambda: GscQueryRow.ctr.desc(),
    "ctr_asc":          lambda: GscQueryRow.ctr.asc(),
    "query_asc":        lambda: GscQueryRow.query.asc(),
}

_PAGE_SORT = {
    "clicks_desc":      lambda: GscPageRow.clicks.desc(),
    "clicks_asc":       lambda: GscPageRow.clicks.asc(),
    "impressions_desc": lambda: GscPageRow.impressions.desc(),
    "impressions_asc":  lambda: GscPageRow.impressions.asc(),
    "position_asc":     lambda: GscPageRow.position.asc(),
    "position_desc":    lambda: GscPageRow.position.desc(),
    "ctr_desc":         lambda: GscPageRow.ctr.desc(),
    "ctr_asc":          lambda: GscPageRow.ctr.asc(),
    "page_asc":         lambda: GscPageRow.page.asc(),
}


@router.get("/properties/{property_id}/queries")
async def get_queries(
    property_id: str,
    q:         str = "",
    sort:      str = "clicks_desc",
    page:      int = 0,
    page_size: int = 50,
    min_impressions: int = 0,
):
    """Return paginated, filtered, sorted query rows for a property."""
    order_fn = _QUERY_SORT.get(sort, _QUERY_SORT["clicks_desc"])

    async with AsyncSessionLocal() as db:
        stmt = select(GscQueryRow).where(GscQueryRow.property_id == property_id)
        if q:
            stmt = stmt.where(GscQueryRow.query.ilike(f"%{q}%"))
        if min_impressions > 0:
            stmt = stmt.where(GscQueryRow.impressions >= min_impressions)

        total = (await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar_one()

        items = (await db.execute(
            stmt.order_by(order_fn()).offset(page * page_size).limit(page_size)
        )).scalars().all()

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "items": [
            {
                "id":          r.id,
                "query":       r.query,
                "clicks":      r.clicks,
                "impressions": r.impressions,
                "ctr":         round(r.ctr * 100, 2) if r.ctr is not None else None,
                "position":    round(r.position, 1)  if r.position is not None else None,
            }
            for r in items
        ],
    }


@router.get("/properties/{property_id}/pages")
async def get_pages(
    property_id: str,
    q:         str = "",
    sort:      str = "clicks_desc",
    page:      int = 0,
    page_size: int = 50,
    min_impressions: int = 0,
):
    """Return paginated, filtered, sorted page rows for a property."""
    order_fn = _PAGE_SORT.get(sort, _PAGE_SORT["clicks_desc"])

    async with AsyncSessionLocal() as db:
        stmt = select(GscPageRow).where(GscPageRow.property_id == property_id)
        if q:
            stmt = stmt.where(GscPageRow.page.ilike(f"%{q}%"))
        if min_impressions > 0:
            stmt = stmt.where(GscPageRow.impressions >= min_impressions)

        total = (await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar_one()

        items = (await db.execute(
            stmt.order_by(order_fn()).offset(page * page_size).limit(page_size)
        )).scalars().all()

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "items": [
            {
                "id":          r.id,
                "page":        r.page,
                "clicks":      r.clicks,
                "impressions": r.impressions,
                "ctr":         round(r.ctr * 100, 2) if r.ctr is not None else None,
                "position":    round(r.position, 1)  if r.position is not None else None,
            }
            for r in items
        ],
    }


# ── Cross-reference endpoint ──────────────────────────────────────────────────

@router.get("/properties/{property_id}/cross-reference")
async def cross_reference(property_id: str):
    """
    Cross-reference GSC data against keyword research sessions and audit results.

    Returns four sections:
    - queries_matched       : GSC queries also found in keyword research sessions
    - near_miss_queries     : Position 4–20 queries NOT in any research session (opportunity gap)
    - pages_audited         : GSC pages that have audit result data (with score)
    - pages_unaudited       : High-traffic GSC pages with no audit results (should audit)
    - low_ctr_pages         : Pages with ≥500 impressions but <3% CTR (title/meta fix candidates)
    """
    async with AsyncSessionLocal() as db:

        # ── Load GSC data ──────────────────────────────────────────────────
        all_queries = (await db.execute(
            select(GscQueryRow).where(GscQueryRow.property_id == property_id)
        )).scalars().all()

        all_pages = (await db.execute(
            select(GscPageRow).where(GscPageRow.property_id == property_id)
        )).scalars().all()

        # ── Build keyword research lookup ──────────────────────────────────
        kw_rows = (await db.execute(
            select(KeywordResult.keyword, KeywordResult.session_id, KeywordResult.is_question)
        )).all()

        # lowercase keyword → list of {session_id, is_question}
        kw_lookup: dict[str, list] = {}
        for kw, sid, is_q in kw_rows:
            k = kw.lower().strip()
            kw_lookup.setdefault(k, []).append({"session_id": sid, "is_question": bool(is_q)})

        # session_id → session_name
        sessions = (await db.execute(
            select(KeywordSession.id, KeywordSession.name)
        )).all()
        session_names: dict[str, str] = {s.id: s.name for s in sessions}

        # ── Build audit lookup ─────────────────────────────────────────────
        # latest (highest) score per page_url
        audit_rows = (await db.execute(
            select(AuditResult.page_url, func.max(AuditResult.score))
            .group_by(AuditResult.page_url)
        )).all()
        # normalise URL: lowercase, strip trailing slash
        audit_lookup: dict[str, int] = {
            (url or "").rstrip("/").lower(): (score or 0)
            for url, score in audit_rows
            if url
        }

    # ── Cross-reference queries ────────────────────────────────────────────
    queries_matched:   list[dict] = []
    near_miss_queries: list[dict] = []

    for q in all_queries:
        q_lower = q.query.lower().strip()
        info = {
            "query":       q.query,
            "clicks":      q.clicks,
            "impressions": q.impressions,
            "ctr":         round(q.ctr * 100, 2)  if q.ctr      is not None else None,
            "position":    round(q.position, 1)   if q.position is not None else None,
        }

        if q_lower in kw_lookup:
            matches = kw_lookup[q_lower]
            queries_matched.append({
                **info,
                "in_research": [
                    {
                        "session_id":   m["session_id"],
                        "session_name": session_names.get(m["session_id"], "Unknown session"),
                        "is_question":  m["is_question"],
                    }
                    for m in matches[:5]
                ],
            })
        elif q.position is not None and 4.0 <= q.position <= 20.0:
            near_miss_queries.append(info)

    # ── Cross-reference pages ──────────────────────────────────────────────
    pages_audited:   list[dict] = []
    pages_unaudited: list[dict] = []
    low_ctr_pages:   list[dict] = []

    for p in all_pages:
        p_norm = p.page.rstrip("/").lower()
        page_info = {
            "page":        p.page,
            "clicks":      p.clicks,
            "impressions": p.impressions,
            "ctr":         round(p.ctr * 100, 2) if p.ctr      is not None else None,
            "position":    round(p.position, 1)  if p.position is not None else None,
        }

        audit_score = audit_lookup.get(p_norm)
        if audit_score is not None:
            pages_audited.append({**page_info, "audit_score": audit_score})
        elif p.clicks >= 10:
            pages_unaudited.append(page_info)

        if p.impressions >= 500 and p.ctr is not None and p.ctr < 0.03:
            low_ctr_pages.append(page_info)

    # ── Sort & cap ─────────────────────────────────────────────────────────
    queries_matched.sort(   key=lambda x: x["clicks"],      reverse=True)
    near_miss_queries.sort( key=lambda x: x["impressions"],  reverse=True)
    pages_audited.sort(     key=lambda x: x["clicks"],      reverse=True)
    pages_unaudited.sort(   key=lambda x: x["clicks"],      reverse=True)
    low_ctr_pages.sort(     key=lambda x: x["impressions"], reverse=True)

    return {
        "queries_matched":   queries_matched[:200],
        "near_miss_queries": near_miss_queries[:100],
        "pages_audited":     pages_audited[:100],
        "pages_unaudited":   pages_unaudited[:100],
        "low_ctr_pages":     low_ctr_pages[:100],
        "summary": {
            "total_queries":         len(all_queries),
            "total_pages":           len(all_pages),
            "queries_in_research":   len(queries_matched),
            "near_miss_count":       len(near_miss_queries),
            "pages_with_audits":     len(pages_audited),
            "pages_need_audit":      len(pages_unaudited),
            "low_ctr_count":         len(low_ctr_pages),
        },
    }


