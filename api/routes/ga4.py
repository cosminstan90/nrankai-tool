"""
Google Analytics 4 Data Integration.

Supports manual CSV upload from GA4 Exploration/Pages/Channels report exports.
Auto-detects report type (pages vs channel groups) from column headers.
Provides cross-reference views against GSC pages and audit results.

Endpoints
---------
POST   /api/ga4/properties                        create a property
GET    /api/ga4/properties                        list all properties
DELETE /api/ga4/properties/{id}                   delete property + all its data
POST   /api/ga4/properties/{id}/upload            upload pages OR channels CSV (auto-detected)
GET    /api/ga4/properties/{id}/pages             paginated + filtered page rows
GET    /api/ga4/properties/{id}/channels          paginated + filtered channel rows
GET    /api/ga4/properties/{id}/cross-reference   cross-reference with GSC + audit data
"""

import csv
import io
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy import delete as sql_delete

from api.models.database import (
    AsyncSessionLocal,
    Ga4Property,
    Ga4PageRow,
    Ga4ChannelRow,
    GscPageRow,
    AuditResult,
)

router = APIRouter(prefix="/api/ga4", tags=["ga4"])


# ── Pydantic models ───────────────────────────────────────────────────────────

class CreatePropertyRequest(BaseModel):
    name:     str
    site_url: str


# ── CSV parsing ───────────────────────────────────────────────────────────────

def _parse_ga4_csv(content: bytes) -> tuple[str, list[dict]]:
    """
    Parse a GA4 CSV export (Pages or Channel Groups report).

    Handles:
    - UTF-8 BOM (Windows exports)
    - Page path / landing page column names        → report_type = 'pages'
    - 'Session default channel group' column names → report_type = 'channels'
    - bounce_rate as plain decimal (0.45) — GA4 does NOT add % like GSC/Ads
    - Comma-separated integers ("1,234" → 1234)

    Returns (report_type, rows) where rows are dicts with all parsed fields.
    """
    # utf-8-sig strips BOM if present; fall back to latin-1 for legacy exports
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    if not fieldnames:
        raise ValueError("Empty or invalid CSV file — no column headers found.")

    # Normalise column names for matching
    col_norm = {f.strip().lower(): f for f in fieldnames}

    # Detect report type from first column
    first_col_lower = (fieldnames[0] or "").strip().lower()
    PAGE_HEADERS    = {
        "page path", "page", "page path and screen class",
        "page title and screen class", "landing page",
        "landing page + query string", "page title",
    }
    CHANNEL_HEADERS = {
        "session default channel group", "default channel grouping",
        "channel group", "channel", "session medium", "session source / medium",
    }

    if first_col_lower in PAGE_HEADERS:
        report_type = "pages"
    elif first_col_lower in CHANNEL_HEADERS:
        report_type = "channels"
    else:
        raise ValueError(
            f"Cannot detect report type from first column: {fieldnames[0]!r}. "
            "Expected a 'Page path', 'Landing page', or 'Session default channel group' column."
        )

    key_col = fieldnames[0]  # actual column name (preserve original casing)

    # Helpers
    def _get(row: dict, *names: str) -> Optional[str]:
        for n in names:
            col = col_norm.get(n)
            if col and col in row:
                return row[col]
        return None

    def _int(v) -> int:
        try:
            return int(str(v or 0).replace(",", "").strip())
        except (ValueError, TypeError):
            return 0

    def _float(v) -> Optional[float]:
        try:
            s = str(v or "").strip().rstrip("%")  # strip % if accidentally present
            return float(s) if s else None
        except (ValueError, TypeError):
            return None

    def _dur(v) -> Optional[float]:
        """Parse engagement time — may be 'mm:ss' format or plain seconds."""
        if v is None:
            return None
        s = str(v).strip()
        if ":" in s:
            parts = s.split(":")
            try:
                if len(parts) == 2:
                    return int(parts[0]) * 60 + float(parts[1])
                elif len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            except (ValueError, TypeError):
                return None
        try:
            return float(s) if s else None
        except (ValueError, TypeError):
            return None

    rows: list[dict] = []
    for row in reader:
        key_value = (row.get(key_col) or "").strip()
        if not key_value:
            continue

        if report_type == "pages":
            rows.append({
                "key":                  key_value,
                "views":                _int(_get(row, "views", "screen page views", "pageviews", "sessions")),
                "users":                _int(_get(row, "users", "total users", "active users")),
                "sessions":             _int(_get(row, "sessions")),
                "avg_engagement_time":  _dur(_get(row, "average engagement time per session",
                                                   "avg. session duration", "average session duration",
                                                   "engagement time")),
                "bounce_rate":          _float(_get(row, "bounce rate", "bounced sessions")),
                "conversions":          _float(_get(row, "conversions", "key events")),
            })
        else:  # channels
            rows.append({
                "key":                  key_value,
                "sessions":             _int(_get(row, "sessions")),
                "users":                _int(_get(row, "users", "total users", "active users")),
                "avg_engagement_time":  _dur(_get(row, "average engagement time per session",
                                                   "avg. session duration", "engagement time")),
                "conversions":          _float(_get(row, "conversions", "key events")),
                "conversion_rate":      _float(_get(row, "session conversion rate",
                                                     "conversion rate", "conversions / sessions")),
            })

    if not rows:
        raise ValueError("CSV parsed successfully but contained no data rows.")

    return report_type, rows


# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.post("/properties", status_code=201)
async def create_property(req: CreatePropertyRequest):
    """Create a new GA4 property entry."""
    async with AsyncSessionLocal() as db:
        prop = Ga4Property(
            id       = str(uuid.uuid4()),
            name     = req.name.strip(),
            site_url = req.site_url.strip(),
        )
        db.add(prop)
        await db.commit()
    return {"id": prop.id, "name": prop.name, "site_url": prop.site_url}


@router.get("/properties")
async def list_properties():
    """Return all GA4 properties, newest first."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Ga4Property).order_by(Ga4Property.created_at.desc())
        )).scalars().all()

    return [
        {
            "id":               p.id,
            "name":             p.name,
            "site_url":         p.site_url,
            "date_range_start": p.date_range_start,
            "date_range_end":   p.date_range_end,
            "total_pages":      p.total_pages,
            "total_channels":   p.total_channels,
            "created_at":       p.created_at.isoformat() if p.created_at else None,
            "updated_at":       p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in rows
    ]


@router.delete("/properties/{property_id}")
async def delete_property(property_id: str):
    """Delete a property and all its page/channel rows (CASCADE)."""
    async with AsyncSessionLocal() as db:
        await db.execute(sql_delete(Ga4PageRow).where(Ga4PageRow.property_id == property_id))
        await db.execute(sql_delete(Ga4ChannelRow).where(Ga4ChannelRow.property_id == property_id))
        await db.execute(sql_delete(Ga4Property).where(Ga4Property.id == property_id))
        await db.commit()
    return {"success": True}


# ── CSV upload ────────────────────────────────────────────────────────────────

@router.post("/properties/{property_id}/upload")
async def upload_csv(property_id: str, file: UploadFile = File(...)):
    """
    Upload a GA4 CSV export (pages OR channels).
    Report type is auto-detected from column headers.
    Replaces any previously uploaded data of that type for this property.
    """
    async with AsyncSessionLocal() as db:
        prop = await db.get(Ga4Property, property_id)
        if not prop:
            raise HTTPException(status_code=404, detail="Property not found")

        content = await file.read()
        try:
            report_type, rows = _parse_ga4_csv(content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        if report_type == "pages":
            await db.execute(
                sql_delete(Ga4PageRow).where(Ga4PageRow.property_id == property_id)
            )
            db.add_all([
                Ga4PageRow(
                    property_id         = property_id,
                    page                = r["key"],
                    views               = r["views"],
                    users               = r["users"],
                    sessions            = r["sessions"],
                    avg_engagement_time = r["avg_engagement_time"],
                    bounce_rate         = r["bounce_rate"],
                    conversions         = r["conversions"],
                )
                for r in rows
            ])
            prop.total_pages = len(rows)

        else:  # channels
            await db.execute(
                sql_delete(Ga4ChannelRow).where(Ga4ChannelRow.property_id == property_id)
            )
            db.add_all([
                Ga4ChannelRow(
                    property_id         = property_id,
                    channel             = r["key"],
                    sessions            = r["sessions"],
                    users               = r["users"],
                    avg_engagement_time = r["avg_engagement_time"],
                    conversions         = r["conversions"],
                    conversion_rate     = r["conversion_rate"],
                )
                for r in rows
            ])
            prop.total_channels = len(rows)

        prop.updated_at = datetime.utcnow()
        await db.commit()

    return {
        "report_type":   report_type,
        "rows_imported": len(rows),
        "property_id":   property_id,
    }


# ── Data query endpoints ──────────────────────────────────────────────────────

_PAGE_SORT = {
    "views_desc":    lambda: Ga4PageRow.views.desc(),
    "views_asc":     lambda: Ga4PageRow.views.asc(),
    "sessions_desc": lambda: Ga4PageRow.sessions.desc(),
    "sessions_asc":  lambda: Ga4PageRow.sessions.asc(),
    "users_desc":    lambda: Ga4PageRow.users.desc(),
    "users_asc":     lambda: Ga4PageRow.users.asc(),
    "bounce_desc":   lambda: Ga4PageRow.bounce_rate.desc(),
    "bounce_asc":    lambda: Ga4PageRow.bounce_rate.asc(),
    "page_asc":      lambda: Ga4PageRow.page.asc(),
}

_CHANNEL_SORT = {
    "sessions_desc": lambda: Ga4ChannelRow.sessions.desc(),
    "sessions_asc":  lambda: Ga4ChannelRow.sessions.asc(),
    "users_desc":    lambda: Ga4ChannelRow.users.desc(),
    "users_asc":     lambda: Ga4ChannelRow.users.asc(),
    "conv_desc":     lambda: Ga4ChannelRow.conversions.desc(),
    "conv_asc":      lambda: Ga4ChannelRow.conversions.asc(),
    "channel_asc":   lambda: Ga4ChannelRow.channel.asc(),
}


@router.get("/properties/{property_id}/pages")
async def get_pages(
    property_id: str,
    q:         str = "",
    sort:      str = "views_desc",
    page:      int = 0,
    page_size: int = 50,
    min_sessions: int = 0,
):
    """Return paginated, filtered, sorted page rows for a property."""
    order_fn = _PAGE_SORT.get(sort, _PAGE_SORT["views_desc"])

    async with AsyncSessionLocal() as db:
        stmt = select(Ga4PageRow).where(Ga4PageRow.property_id == property_id)
        if q:
            stmt = stmt.where(Ga4PageRow.page.ilike(f"%{q}%"))
        if min_sessions > 0:
            stmt = stmt.where(Ga4PageRow.sessions >= min_sessions)

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
                "id":                  r.id,
                "page":                r.page,
                "views":               r.views,
                "users":               r.users,
                "sessions":            r.sessions,
                "avg_engagement_time": round(r.avg_engagement_time, 1) if r.avg_engagement_time is not None else None,
                "bounce_rate":         round(r.bounce_rate * 100, 1)   if r.bounce_rate is not None else None,
                "conversions":         r.conversions,
            }
            for r in items
        ],
    }


@router.get("/properties/{property_id}/channels")
async def get_channels(
    property_id: str,
    sort:      str = "sessions_desc",
    page:      int = 0,
    page_size: int = 50,
):
    """Return paginated, sorted channel rows for a property."""
    order_fn = _CHANNEL_SORT.get(sort, _CHANNEL_SORT["sessions_desc"])

    async with AsyncSessionLocal() as db:
        stmt = select(Ga4ChannelRow).where(Ga4ChannelRow.property_id == property_id)

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
                "id":                  r.id,
                "channel":             r.channel,
                "sessions":            r.sessions,
                "users":               r.users,
                "avg_engagement_time": round(r.avg_engagement_time, 1) if r.avg_engagement_time is not None else None,
                "conversions":         r.conversions,
                "conversion_rate":     round(r.conversion_rate * 100, 2) if r.conversion_rate is not None else None,
            }
            for r in items
        ],
    }


# ── Cross-reference endpoint ──────────────────────────────────────────────────

@router.get("/properties/{property_id}/cross-reference")
async def cross_reference(property_id: str):
    """
    Cross-reference GA4 page data against GSC pages and audit results.

    Returns:
    - pages_with_all_data : pages present in GA4, GSC, and audit results
    - pages_no_audit      : GA4 pages with GSC data but no audit score
    - high_bounce_pages   : pages with bounce_rate > 60% and ≥ 50 sessions
    - summary             : counts
    """
    async with AsyncSessionLocal() as db:

        # ── Load GA4 pages ─────────────────────────────────────────────────
        ga4_pages = (await db.execute(
            select(Ga4PageRow).where(Ga4PageRow.property_id == property_id)
        )).scalars().all()

        # ── Build GSC lookup: normalised page URL → {clicks, impressions, ctr, position} ──
        gsc_rows = (await db.execute(
            select(GscPageRow.page, GscPageRow.clicks, GscPageRow.impressions,
                   GscPageRow.ctr, GscPageRow.position)
        )).all()
        gsc_lookup: dict[str, dict] = {}
        for page, clicks, impressions, ctr, position in gsc_rows:
            norm = (page or "").rstrip("/").lower()
            gsc_lookup[norm] = {
                "clicks":      clicks,
                "impressions": impressions,
                "ctr":         round(ctr * 100, 2) if ctr is not None else None,
                "position":    round(position, 1)  if position is not None else None,
            }

        # ── Build audit lookup: normalised URL → best score ────────────────
        audit_rows = (await db.execute(
            select(AuditResult.page_url, func.max(AuditResult.score))
            .group_by(AuditResult.page_url)
        )).all()
        audit_lookup: dict[str, int] = {
            (url or "").rstrip("/").lower(): (score or 0)
            for url, score in audit_rows
            if url
        }

    # ── Build cross-reference sections ─────────────────────────────────────
    pages_with_all_data: list[dict] = []
    pages_no_audit:      list[dict] = []
    high_bounce_pages:   list[dict] = []

    for p in ga4_pages:
        p_norm = (p.page or "").rstrip("/").lower()

        base_info = {
            "page":                p.page,
            "views":               p.views,
            "sessions":            p.sessions,
            "users":               p.users,
            "avg_engagement_time": round(p.avg_engagement_time, 1) if p.avg_engagement_time is not None else None,
            "bounce_rate":         round(p.bounce_rate * 100, 1)   if p.bounce_rate is not None else None,
            "conversions":         p.conversions,
        }

        gsc_data    = gsc_lookup.get(p_norm)
        audit_score = audit_lookup.get(p_norm)

        if gsc_data is not None and audit_score is not None:
            pages_with_all_data.append({**base_info, **gsc_data, "audit_score": audit_score})
        elif gsc_data is not None:
            pages_no_audit.append({**base_info, **gsc_data})

        # High bounce detection (raw value stored as decimal)
        if (p.bounce_rate is not None and p.bounce_rate > 0.60
                and p.sessions is not None and p.sessions >= 50):
            high_bounce_pages.append(base_info)

    # ── Sort ───────────────────────────────────────────────────────────────
    pages_with_all_data.sort(key=lambda x: x.get("sessions", 0), reverse=True)
    pages_no_audit.sort(     key=lambda x: x.get("sessions", 0), reverse=True)
    high_bounce_pages.sort(  key=lambda x: x.get("sessions", 0), reverse=True)

    return {
        "pages_with_all_data": pages_with_all_data[:100],
        "pages_no_audit":      pages_no_audit[:100],
        "high_bounce_pages":   high_bounce_pages[:100],
        "summary": {
            "total_ga4_pages":       len(ga4_pages),
            "pages_with_all_data":   len(pages_with_all_data),
            "pages_no_audit":        len(pages_no_audit),
            "high_bounce_count":     len(high_bounce_pages),
        },
    }
