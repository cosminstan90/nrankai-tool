"""
Google Search Console Data Integration.

Supports manual CSV upload from GSC Performance exports (queries + pages reports).
Auto-detects report type from column headers.
Provides cross-reference views against audit results and keyword research sessions.
Also provides OAuth 2.0 integration for live API sync.

Endpoints
---------
POST   /api/gsc/properties                        create a property
GET    /api/gsc/properties                        list all properties
DELETE /api/gsc/properties/{id}                   delete property + all its data
POST   /api/gsc/properties/{id}/upload            upload queries OR pages CSV (auto-detected)
POST   /api/gsc/properties/{id}/sync              sync queries+pages from GSC API (OAuth)
GET    /api/gsc/properties/{id}/queries           paginated + filtered query rows
GET    /api/gsc/properties/{id}/pages             paginated + filtered page rows
GET    /api/gsc/properties/{id}/cross-reference   cross-reference with audits + kw research
GET    /api/gsc/oauth/authorize                   start OAuth flow (redirect to Google)
GET    /api/gsc/oauth/callback                    OAuth callback (exchange code, store token)
GET    /api/gsc/oauth/status                      check if Google account is connected
DELETE /api/gsc/oauth/disconnect                  remove stored OAuth token
GET    /api/gsc/oauth/sites                       list GSC sites from Google API
"""

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

# ── Google OAuth config ────────────────────────────────────────────────────────

_GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
_GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
_GOOGLE_REDIRECT_URI  = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/gsc/oauth/callback")
_GSC_SCOPES           = ["https://www.googleapis.com/auth/webmasters.readonly"]
_CLIENT_CONFIG        = {
    "web": {
        "client_id":     _GOOGLE_CLIENT_ID,
        "client_secret": _GOOGLE_CLIENT_SECRET,
        "redirect_uris": [_GOOGLE_REDIRECT_URI],
        "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
        "token_uri":     "https://oauth2.googleapis.com/token",
    }
}


def _oauth_available() -> bool:
    """Return True if Google OAuth credentials are configured."""
    return bool(_GOOGLE_CLIENT_ID and _GOOGLE_CLIENT_SECRET)


async def _load_token() -> Optional[GoogleOAuthToken]:
    """Load the stored OAuth token (if any) from DB."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(GoogleOAuthToken).limit(1))
        return result.scalar_one_or_none()


async def _get_gsc_credentials():
    """
    Return a refreshed google.oauth2.credentials.Credentials object,
    or None if no token is stored. Refreshes access_token if expired.
    Raises ImportError if google-auth-oauthlib is not installed.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GoogleRequest

    token_row = await _load_token()
    if not token_row:
        return None

    creds = Credentials(
        token=token_row.access_token,
        refresh_token=token_row.refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=_GOOGLE_CLIENT_ID,
        client_secret=_GOOGLE_CLIENT_SECRET,
        scopes=_GSC_SCOPES,
    )
    if token_row.token_expiry:
        expiry = token_row.token_expiry
        # Ensure timezone-aware — google-auth compares expiry against utcnow()
        # which is tz-aware; a naive expiry causes a TypeError comparison failure.
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        creds.expiry = expiry

    try:
        is_expired = creds.expired
    except TypeError:
        is_expired = True   # can't determine, force a refresh
    if is_expired and creds.refresh_token:
        await asyncio.get_event_loop().run_in_executor(None, creds.refresh, GoogleRequest())
        # Persist updated token
        async with AsyncSessionLocal() as db:
            row = await db.execute(select(GoogleOAuthToken).limit(1))
            row = row.scalar_one_or_none()
            if row:
                row.access_token = creds.token
                row.token_expiry = creds.expiry.replace(tzinfo=None) if creds.expiry else None
                row.updated_at   = datetime.utcnow()
                await db.commit()

    return creds

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


# ── CSV parsing ───────────────────────────────────────────────────────────────

def _parse_gsc_csv(content: bytes) -> tuple[str, list[dict]]:
    """
    Parse a GSC Performance CSV export.

    Handles:
    - UTF-8 BOM (Windows exports)
    - 'Top queries' / 'Query' / 'Keyword' column names  → report_type = 'queries'
    - 'Top pages'  / 'Landing page' / 'Page' / 'URL'    → report_type = 'pages'
    - CTR formatted as "5.00%" → stored as 0.05
    - Comma-separated integers ("1,234" → 1234)

    Returns (report_type, rows) where rows are dicts with keys:
        key, clicks, impressions, ctr, position
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

    # Detect report type
    first_col_lower = (fieldnames[0] or "").strip().lower()
    QUERY_HEADERS = {"top queries", "query", "queries", "keyword", "search query"}
    PAGE_HEADERS  = {"top pages", "landing page", "page", "pages", "url", "address"}

    if first_col_lower in QUERY_HEADERS:
        report_type = "queries"
    elif first_col_lower in PAGE_HEADERS:
        report_type = "pages"
    else:
        raise ValueError(
            f"Cannot detect report type from first column: {fieldnames[0]!r}. "
            "Expected 'Top queries', 'Query', 'Top pages', or 'Landing page'."
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
            return float(str(v or "").strip())
        except (ValueError, TypeError):
            return None

    def _ctr(v) -> Optional[float]:
        try:
            return float(str(v or "").strip().rstrip("%")) / 100.0
        except (ValueError, TypeError):
            return None

    rows: list[dict] = []
    for row in reader:
        key_value = (row.get(key_col) or "").strip()
        if not key_value:
            continue
        rows.append({
            "key":         key_value,
            "clicks":      _int(_get(row, "clicks")),
            "impressions": _int(_get(row, "impressions")),
            "ctr":         _ctr(_get(row, "ctr")),
            "position":    _float(_get(row, "position")),
        })

    if not rows:
        raise ValueError("CSV parsed successfully but contained no data rows.")

    return report_type, rows


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


# ── OAuth endpoints ────────────────────────────────────────────────────────────

@router.get("/oauth/status")
async def oauth_status():
    """Return whether a Google account is connected."""
    token = await _load_token()
    if token:
        return {"connected": True, "email": token.email}
    return {"connected": False, "email": None}


# In-memory store mapping OAuth state → PKCE code_verifier.
# Google now requires PKCE for all web-app flows; the verifier is generated
# during authorize and must be passed back during token exchange.
_pkce_store: dict = {}


@router.get("/oauth/authorize")
async def oauth_authorize():
    """Redirect the browser to the Google OAuth consent screen."""
    if not _oauth_available():
        raise HTTPException(400, "Google OAuth credentials not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env")
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError:
        raise HTTPException(500, "google-auth-oauthlib not installed. Run: pip install google-auth-oauthlib google-api-python-client")

    # autogenerate_code_verifier=True (default) — let the library generate PKCE.
    # We capture the verifier here and store it keyed by the OAuth state param;
    # the callback retrieves it and passes it to fetch_token().
    flow = Flow.from_client_config(_CLIENT_CONFIG, scopes=_GSC_SCOPES)
    flow.redirect_uri = _GOOGLE_REDIRECT_URI
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")

    # Persist code_verifier so the callback can complete PKCE exchange
    if flow.code_verifier:
        _pkce_store[state] = flow.code_verifier

    return RedirectResponse(auth_url)


@router.get("/oauth/callback")
async def oauth_callback(code: str = None, error: str = None, state: str = None):
    """Handle the OAuth callback from Google, store tokens."""
    if error:
        return RedirectResponse(f"/gsc?oauth_error={error}")
    if not code:
        return RedirectResponse("/gsc?oauth_error=no_code")

    # Retrieve (and discard) the stored PKCE verifier for this state
    code_verifier = _pkce_store.pop(state, None) if state else None
    import logging
    logging.warning(f"[oauth_callback] state={state!r} code_verifier={'SET' if code_verifier else 'MISSING'} pkce_store_keys={list(_pkce_store.keys())[:5]}")

    try:
        from googleapiclient.discovery import build
        import requests as _requests
        from google.oauth2.credentials import Credentials as _GCreds
    except ImportError:
        raise HTTPException(500, "google-api-python-client not installed")

    def _exchange(code_val, verifier):
        # POST directly to Google token endpoint — bypasses google-auth-oauthlib
        # PKCE complexity and gives full control over the request body.
        token_url = "https://oauth2.googleapis.com/token"
        payload = {
            "code":          code_val,
            "client_id":     os.getenv("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
            "redirect_uri":  _GOOGLE_REDIRECT_URI,
            "grant_type":    "authorization_code",
        }
        if verifier:
            payload["code_verifier"] = verifier

        resp = _requests.post(token_url, data=payload, timeout=15)
        tok = resp.json()
        if "error" in tok:
            raise ValueError(f"Token exchange error: {tok['error']}: {tok.get('error_description', '')}")

        creds = _GCreds(
            token=tok["access_token"],
            refresh_token=tok.get("refresh_token"),
            token_uri=token_url,
            client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            scopes=_GSC_SCOPES,
        )
        # Fetch email via userinfo
        try:
            svc = build("oauth2", "v2", credentials=creds)
            info = svc.userinfo().get().execute()
            email = info.get("email", "")
        except Exception:
            email = ""
        return creds, email

    creds, email = await asyncio.get_event_loop().run_in_executor(
        None, _exchange, code, code_verifier
    )

    expiry = creds.expiry.replace(tzinfo=None) if creds.expiry else None

    async with AsyncSessionLocal() as db:
        # Replace any existing token (single-row store)
        await db.execute(sql_delete(GoogleOAuthToken))
        db.add(GoogleOAuthToken(
            email=email,
            access_token=creds.token,
            refresh_token=creds.refresh_token or "",
            token_expiry=expiry,
        ))
        await db.commit()

    return RedirectResponse("/gsc?oauth_connected=1")


@router.delete("/oauth/disconnect")
async def oauth_disconnect():
    """Remove the stored Google OAuth token."""
    async with AsyncSessionLocal() as db:
        await db.execute(sql_delete(GoogleOAuthToken))
        await db.commit()
    return {"ok": True}


@router.get("/oauth/sites")
async def oauth_list_sites():
    """List all GSC properties accessible by the connected Google account."""
    creds = await _get_gsc_credentials()
    if not creds:
        raise HTTPException(401, "Google account not connected")

    try:
        from googleapiclient.discovery import build
    except ImportError:
        raise HTTPException(500, "google-api-python-client not installed")

    def _list():
        svc = build("searchconsole", "v1", credentials=creds)
        result = svc.sites().list().execute()
        return [s["siteUrl"] for s in result.get("siteEntry", [])]

    sites = await asyncio.get_event_loop().run_in_executor(None, _list)
    return {"sites": sites}


@router.post("/properties/{property_id}/sync")
async def sync_property(property_id: str, days: int = 90):
    """Pull queries + pages from the GSC API and replace existing rows."""
    creds = await _get_gsc_credentials()
    if not creds:
        raise HTTPException(401, "Google account not connected. Connect at /gsc first.")

    try:
        from googleapiclient.discovery import build
    except ImportError:
        raise HTTPException(500, "google-api-python-client not installed")

    # ── 1. Load property ───────────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        prop = await db.get(GscProperty, property_id)
        if not prop:
            raise HTTPException(404, "Property not found")
        site_url = prop.site_url

    end_date   = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days)
    start_str  = start_date.isoformat()
    end_str    = end_date.isoformat()

    def _fetch(dimension: str):
        svc  = build("searchconsole", "v1", credentials=creds)
        body = {
            "startDate":  start_str,
            "endDate":    end_str,
            "dimensions": [dimension],
            "rowLimit":   25000,
        }
        resp = svc.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return resp.get("rows", [])

    query_rows, page_rows = await asyncio.gather(
        asyncio.get_event_loop().run_in_executor(None, _fetch, "query"),
        asyncio.get_event_loop().run_in_executor(None, _fetch, "page"),
    )

    # ── 2-4. Bulk replace rows using synchronous sqlite3 in a thread ──────────
    # Runs in a thread pool to avoid blocking the event loop.
    # Uses sqlite3 directly (not SQLAlchemy/aiosqlite) which is rock-solid
    # for bulk writes and avoids any StaticPool connection conflicts.
    def _bulk_replace(db_path: str, pid: str, q_rows: list, p_rows: list) -> None:
        conn = sqlite3.connect(db_path, timeout=60, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM gsc_query_rows WHERE property_id = ?", (pid,))
            cur.execute("DELETE FROM gsc_page_rows  WHERE property_id = ?", (pid,))
            conn.commit()
            if q_rows:
                cur.executemany(
                    "INSERT INTO gsc_query_rows"
                    " (property_id, query, clicks, impressions, ctr, position)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            pid,
                            r.get("keys", [""])[0] if r.get("keys") else "",
                            int(r.get("clicks", 0)),
                            int(r.get("impressions", 0)),
                            r.get("ctr"),
                            r.get("position"),
                        )
                        for r in q_rows
                    ],
                )
                conn.commit()
            if p_rows:
                cur.executemany(
                    "INSERT INTO gsc_page_rows"
                    " (property_id, page, clicks, impressions, ctr, position)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            pid,
                            r.get("keys", [""])[0] if r.get("keys") else "",
                            int(r.get("clicks", 0)),
                            int(r.get("impressions", 0)),
                            r.get("ctr"),
                            r.get("position"),
                        )
                        for r in p_rows
                    ],
                )
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    await asyncio.get_event_loop().run_in_executor(
        None, _bulk_replace, DATABASE_PATH, property_id, query_rows, page_rows
    )

    # ── 5. Update property metadata via SQLAlchemy ─────────────────────────
    async with AsyncSessionLocal() as db:
        await db.execute(
            sql_update(GscProperty)
            .where(GscProperty.id == property_id)
            .values(
                total_queries=len(query_rows),
                total_pages=len(page_rows),
                date_range_start=start_str,
                date_range_end=end_str,
                last_synced_at=datetime.utcnow(),
                sync_type="api",
            )
        )
        await db.commit()

    return {
        "ok":       True,
        "queries":  len(query_rows),
        "pages":    len(page_rows),
        "date_range": {"start": start_str, "end": end_str},
    }


@router.get("/properties/{property_id}/page-queries")
async def get_page_queries(property_id: str, url: str, days: int = 90):
    """Return the search queries driving traffic to a specific page URL.

    Tries the live GSC API first (OAuth); falls back to an empty list if
    the account is not connected (CSV imports don't carry query→page mapping).
    """
    async with AsyncSessionLocal() as db:
        prop = await db.get(GscProperty, property_id)
        if not prop:
            raise HTTPException(404, "Property not found")
        site_url = prop.site_url

    creds = await _get_gsc_credentials()
    if not creds:
        return {"queries": [], "source": "none",
                "message": "Connect a Google account to see per-page queries."}

    try:
        from googleapiclient.discovery import build
    except ImportError:
        raise HTTPException(500, "google-api-python-client not installed")

    end_date   = datetime.utcnow().date()
    start_date = end_date - timedelta(days=days)

    def _fetch():
        svc  = build("searchconsole", "v1", credentials=creds)
        body = {
            "startDate":  start_date.isoformat(),
            "endDate":    end_date.isoformat(),
            "dimensions": ["query"],
            "dimensionFilterGroups": [{
                "filters": [{
                    "dimension":  "page",
                    "expression": url,
                    "operator":   "equals",
                }]
            }],
            "rowLimit": 500,
        }
        resp = svc.searchanalytics().query(siteUrl=site_url, body=body).execute()
        return resp.get("rows", [])

    rows = await asyncio.get_event_loop().run_in_executor(None, _fetch)
    queries = [
        {
            "query":       r["keys"][0] if r.get("keys") else "",
            "clicks":      int(r.get("clicks", 0)),
            "impressions": int(r.get("impressions", 0)),
            "ctr":         round((r.get("ctr") or 0) * 100, 2),
            "position":    round(r.get("position") or 0, 1),
        }
        for r in rows
    ]
    return {"queries": queries, "source": "api",
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat()}}


# ── Shared JSON extractor (used by optimize + cannibalization) ─────────────────

def _extract_json(text: str, label: str = "") -> dict:
    """Strip markdown fences, extract outermost {...} block, repair if truncated."""
    import re as _re, json as _json
    cleaned = _re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=_re.MULTILINE).strip()
    first = cleaned.find("{")
    if first == -1:
        return {"raw": text}
    last = cleaned.rfind("}")
    # Try 1: obvious slice between outermost { and last }
    if last > first:
        try:
            return _json.loads(cleaned[first:last + 1])
        except Exception:
            pass
    # Try 2: repair truncated JSON — close unmatched braces/brackets + strip trailing commas
    candidate = cleaned[first:]
    open_braces   = candidate.count("{") - candidate.count("}")
    open_brackets = candidate.count("[") - candidate.count("]")
    candidate += "]" * max(0, open_brackets) + "}" * max(0, open_braces)
    candidate = _re.sub(r',(\s*[}\]])', r'\1', candidate)  # strip trailing commas
    try:
        return _json.loads(candidate)
    except Exception:
        if label:
            print(f"[gsc] ⚠ JSON parse failed for {label}. Raw (first 500 chars):\n{text[:500]}")
        return {"raw": text}


# ── Page LLM Optimization ─────────────────────────────────────────────────────

async def _run_page_optimize(guide_id: int, property_id: Optional[str], req: PageOptimizeRequest):
    """Background task: fetch page content, get GSC queries, run augmented audit prompts."""
    import json
    import re

    # Mark running
    async with AsyncSessionLocal() as db:
        guide = await db.get(UrlGuide, guide_id)
        guide.status = "running"
        await db.commit()

    try:
        # 1. Fetch page HTML and extract text (or use provided content)
        if req.page_content:
            # User pasted content directly — skip scraping
            page_text = req.page_content[:30000]
        else:
            import httpx
            from bs4 import BeautifulSoup

            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(req.url, headers={"User-Agent": "GEO-Analyzer/2.1"})
                resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.extract()
            page_text = soup.get_text(separator=" ", strip=True)[:30000]

        # 2. Get per-page queries — use caller-provided selection or fetch from OAuth API
        queries = []
        if req.selected_queries is not None:
            # Use the queries the user selected in the UI (already formatted)
            queries = req.selected_queries
        else:
            creds = await _get_gsc_credentials()
            if creds:
                async with AsyncSessionLocal() as db:
                    prop = await db.get(GscProperty, property_id)
                    site_url = prop.site_url

                end_date   = datetime.utcnow().date()
                start_date = end_date - timedelta(days=90)

                def _fetch_queries():
                    from googleapiclient.discovery import build
                    svc  = build("searchconsole", "v1", credentials=creds)
                    body = {
                        "startDate":  start_date.isoformat(),
                        "endDate":    end_date.isoformat(),
                        "dimensions": ["query"],
                        "dimensionFilterGroups": [{
                            "filters": [{
                                "dimension":  "page",
                                "expression": req.url,
                                "operator":   "equals",
                            }]
                        }],
                        "rowLimit": 100,
                    }
                    return svc.searchanalytics().query(siteUrl=site_url, body=body).execute().get("rows", [])

                try:
                    rows = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, _fetch_queries),
                        timeout=20,
                    )
                except Exception:
                    rows = []
                queries = [
                    {
                        "query":       r["keys"][0] if r.get("keys") else "",
                        "clicks":      int(r.get("clicks", 0)),
                        "impressions": int(r.get("impressions", 0)),
                        "ctr":         round((r.get("ctr") or 0) * 100, 2),
                        "position":    round(r.get("position") or 0, 1),
                    }
                    for r in rows
                ]

        # 3. Build keyword context block to append to each audit prompt
        if queries:
            # Split into tiers for clearer LLM instructions
            ranking_well   = [q for q in queries if q["position"] <= 10]
            near_miss      = [q for q in queries if 10 < q["position"] <= 20]
            low_ctr        = [q for q in queries if q["impressions"] >= 100 and q["ctr"] < 3.0]

            def _fmt(q):
                return f'  - "{q["query"]}" — pos {q["position"]}, {q["clicks"]} clicks, {q["impressions"]} impr, CTR {q["ctr"]}%'

            parts = [
                "\n\n## KNOWN SEARCH QUERIES FOR THIS PAGE (Google Search Console — last 90 days)",
                "IMPORTANT: Use these real queries to drive ALL your recommendations below.",
                "Write every recommendation, before/after example, and suggestion in the SAME LANGUAGE as the page content.",
                "",
            ]

            if ranking_well:
                parts.append(f"### Already ranking (pos 1–10) — reinforce these keywords throughout the content:")
                parts.extend(_fmt(q) for q in ranking_well[:20])
                parts.append("")

            if near_miss:
                parts.append(f"### Near-miss opportunities (pos 11–20) — small content improvements can push these to page 1:")
                parts.extend(_fmt(q) for q in near_miss[:20])
                parts.append("")

            if low_ctr:
                parts.append(f"### Low CTR despite impressions (< 3% CTR with 100+ impressions) — title/meta optimisation can capture these:")
                parts.extend(_fmt(q) for q in low_ctr[:10])
                parts.append("")

            parts += [
                "### Required in your recommendations:",
                "1. TITLE TAG: Write a specific optimised title tag (50–60 chars) that uses the highest-value query from the list above naturally.",
                "2. META DESCRIPTION: Write a specific meta description (140–160 chars) with a CTA that incorporates 1–2 of the known queries.",
                "3. H1: Suggest a revised H1 that aligns with the primary query intent.",
                "4. For near-miss queries: explain exactly what content addition or restructuring would move them from pos 11–20 to pos 1–10.",
                "5. For low-CTR queries: explain what title/meta change would improve click-through for those impressions.",
            ]

            kw_block = "\n".join(parts)
        else:
            kw_block = ""

        # 4. Run each requested audit type with the augmented prompt
        from prompt_loader import load_prompt
        from api.routes.schema_gen import call_llm_for_schema

        _SCHEMA_GEN_SYSTEM = (
            "You are a structured data expert specializing in schema.org JSON-LD markup.\n\n"
            "Analyse the provided web page content and generate complete, production-ready JSON-LD structured data.\n\n"
            "RULES:\n"
            "1. Identify ALL applicable schema.org types (Article, FAQPage, HowTo, BreadcrumbList, Product, "
            "LocalBusiness, Organization, WebSite, WebPage, NewsArticle, Review, Event, etc.)\n"
            "2. Generate COMPLETE, VALID JSON-LD — absolutely no placeholders like 'FILL_IN_HERE' or 'TODO'\n"
            "3. Extract real data from the page content (actual names, descriptions, Q&A pairs)\n"
            "4. For FAQPage: extract or infer Q&A pairs from the page content — include minimum 3 pairs\n"
            "5. For Article/NewsArticle: populate headline, description, datePublished if detectable\n"
            "6. Prioritise schema types that qualify for Google Rich Results\n"
            "7. Combine all schemas into one implementation_html block\n\n"
            "LANGUAGE: All 'notes' and 'implementation_notes' fields in the SAME LANGUAGE as the audited page content.\n\n"
            "Return ONLY valid JSON:\n"
            "{\n"
            '  "schema_types_detected": ["Article", "FAQPage"],\n'
            '  "schemas": [\n'
            '    {\n'
            '      "type": "string",\n'
            '      "priority": "high|medium|low",\n'
            '      "rich_result_eligible": true,\n'
            '      "json_ld": { "@context": "https://schema.org", "@type": "...", "...": "..." },\n'
            '      "notes": "Why this schema type applies to this page"\n'
            '    }\n'
            '  ],\n'
            '  "implementation_html": "<script type=\'application/ld+json\'>...</script>",\n'
            '  "implementation_notes": "Where to place this markup and any caveats"\n'
            "}\n"
        )

        _FAQ_SYSTEM = (
            "You are an SEO content strategist. Analyse the provided web page content and its known search queries.\n\n"
            "Generate semantically related keywords and FAQ questions to strengthen topical authority.\n\n"
            "KEYWORD TYPES:\n"
            "- lsi: Latent Semantic Indexing terms (semantically related concepts)\n"
            "- long_tail: Longer, more specific query variations\n"
            "- synonym: Alternative terms for the same concept\n"
            "- semantic: Related but broader/narrower concepts within the topic cluster\n\n"
            "FAQ QUESTIONS must be:\n"
            "- Derived from real user intent visible in the known queries\n"
            "- Written as natural language questions a user would search or ask\n"
            "- Answerable with a paragraph (compatible with schema.org FAQPage markup)\n"
            "- Prioritised by content gap and search opportunity\n\n"
            "LANGUAGE: Respond in the SAME LANGUAGE as the audited page content.\n\n"
            "Return ONLY valid JSON, no preamble:\n"
            "{\n"
            '  "related_keywords": [\n'
            '    {"keyword": "string", "type": "lsi|long_tail|synonym|semantic", '
            '"intent": "informational|commercial|navigational|transactional", "priority": "high|medium|low"}\n'
            "  ],\n"
            '  "faq_questions": [\n'
            '    {"question": "string", "answer_hint": "string", "priority": "high|medium|low"}\n'
            "  ]\n"
            "}\n\n"
            "Generate 15–25 related keywords and 8–15 FAQ questions."
        )

        # _extract_json is now a module-level function

        async def _run_single_audit(audit_type: str):
            if audit_type == "FAQ_KEYWORDS":
                system_prompt = _FAQ_SYSTEM + kw_block
                user_content = page_text[:15000]
            elif audit_type == "SCHEMA_GEN":
                system_prompt = _SCHEMA_GEN_SYSTEM + kw_block
                user_content = page_text
            else:
                system_prompt = load_prompt(audit_type) + kw_block
                user_content = page_text

            response_text, _, _ = await asyncio.wait_for(
                call_llm_for_schema(
                    provider=req.provider,
                    model=req.model,
                    system_prompt=system_prompt,
                    user_content=user_content,
                    max_tokens=8192,
                    prefill="{",
                ),
                timeout=120,
            )
            return audit_type, _extract_json(response_text, label=audit_type)

        pairs = await asyncio.gather(*[_run_single_audit(t) for t in req.audit_types])
        results = dict(pairs)

        # 5. Store completed result
        async with AsyncSessionLocal() as db:
            guide = await db.get(UrlGuide, guide_id)
            guide.status    = "completed"
            guide.guide_json = json.dumps({
                "type":         "optimization",
                "results":      results,
                "queries_used": len(queries),
            })
            guide.updated_at = datetime.utcnow()
            await db.commit()

    except Exception as exc:
        async with AsyncSessionLocal() as db:
            guide = await db.get(UrlGuide, guide_id)
            guide.status        = "failed"
            guide.error_message = str(exc)
            guide.updated_at    = datetime.utcnow()
            await db.commit()


@router.get("/properties/{property_id}/guides")
async def list_property_guides(property_id: str, limit: int = 100):
    """Return all completed page-optimize guides for this GSC property."""
    from sqlalchemy import desc as _desc
    async with AsyncSessionLocal() as db:
        prop = await db.get(GscProperty, property_id)
        if not prop:
            raise HTTPException(404, "Property not found")
        rows = (await db.execute(
            select(UrlGuide)
            .where(
                UrlGuide.gsc_property_id == property_id,
                UrlGuide.status == "completed",
            )
            .order_by(_desc(UrlGuide.created_at))
            .limit(limit)
        )).scalars().all()

    guides = []
    for g in rows:
        gj = None
        audit_types = []
        scores = {}
        if g.guide_json:
            try:
                gj = json.loads(g.guide_json)
                audit_types = list((gj.get("results") or {}).keys())
                results = gj.get("results") or {}
                if "SEO_AUDIT" in results and isinstance(results["SEO_AUDIT"], dict):
                    seo = results["SEO_AUDIT"].get("seo_audit") or {}
                    if seo.get("overall_score") is not None:
                        scores["seo"] = seo["overall_score"]
                if "GEO_AUDIT" in results and isinstance(results["GEO_AUDIT"], dict):
                    geo = results["GEO_AUDIT"].get("geo_audit") or {}
                    if geo.get("overall_score") is not None:
                        scores["geo"] = geo["overall_score"]
            except Exception:
                pass
        guides.append({
            "id":           g.id,
            "url":          g.url,
            "model":        g.model or "",
            "reviewed":     bool(g.reviewed),
            "audit_types":  audit_types,
            "scores":       scores,
            "queries_used": (gj or {}).get("queries_used", 0),
            "created_at":   g.created_at.strftime("%Y-%m-%d %H:%M") if g.created_at else "",
        })
    return {"guides": guides, "total": len(guides)}


@router.post("/properties/{property_id}/page-optimize")
async def page_optimize(property_id: str, req: PageOptimizeRequest):
    """Create an LLM optimization job for a specific page URL.

    Fetches the page content, retrieves GSC queries for that page (if OAuth
    is connected), augments the SEO/GEO audit prompts with the known keywords,
    then calls the LLM. Poll GET /api/guide/{guide_id} for results.
    """
    async with AsyncSessionLocal() as db:
        prop = await db.get(GscProperty, property_id)
        if not prop:
            raise HTTPException(404, "GSC property not found")
        guide = UrlGuide(
            url=req.url,
            status="pending",
            provider=req.provider,
            model=req.model,
            gsc_property_id=property_id,
        )
        db.add(guide)
        await db.commit()
        await db.refresh(guide)
        guide_id = guide.id

    async def _guarded(gid, pid, r):
        try:
            await asyncio.wait_for(_run_page_optimize(gid, pid, r), timeout=300)
        except asyncio.TimeoutError:
            async with AsyncSessionLocal() as db:
                g = await db.get(UrlGuide, gid)
                if g and g.status in ("pending", "running"):
                    g.status = "failed"
                    g.error_message = "Timed out after 5 minutes"
                    g.updated_at = datetime.utcnow()
                    await db.commit()

    asyncio.create_task(_guarded(guide_id, property_id, req))
    return {"guide_id": guide_id, "status": "pending"}


# ── Keyword Cannibalization Detection ─────────────────────────────────────────

class CannibalizationRequest(BaseModel):
    min_impressions: int = 100
    days:            int = 90
    provider:        str = "anthropic"
    model:           str = "claude-haiku-4-5-20251001"


@router.post("/properties/{property_id}/cannibalization")
async def detect_cannibalization(property_id: str, req: CannibalizationRequest):
    """Detect keyword cannibalization using GSC page+query data and LLM semantic clustering."""
    from collections import defaultdict
    from api.routes.schema_gen import call_llm_for_schema

    creds = await _get_gsc_credentials()
    if not creds:
        raise HTTPException(status_code=400, detail="No GSC credentials. Please reconnect Google Search Console.")

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select as _sel
        prop = (await db.execute(_sel(GscProperty).where(GscProperty.id == property_id))).scalar_one_or_none()
        if not prop:
            raise HTTPException(status_code=404, detail="Property not found")
        site_url = prop.site_url

    end_date   = datetime.utcnow().date()
    start_date = end_date - timedelta(days=req.days)

    def _fetch_page_query():
        from googleapiclient.discovery import build
        svc  = build("searchconsole", "v1", credentials=creds)
        body = {
            "startDate":  start_date.isoformat(),
            "endDate":    end_date.isoformat(),
            "dimensions": ["page", "query"],
            "rowLimit":   25000,
        }
        return svc.searchanalytics().query(siteUrl=site_url, body=body).execute().get("rows", [])

    rows = await asyncio.get_event_loop().run_in_executor(None, _fetch_page_query)

    # Parse + filter by min_impressions
    page_query_data = []
    for r in rows:
        keys = r.get("keys", [])
        if len(keys) < 2:
            continue
        impressions = int(r.get("impressions", 0))
        if impressions < req.min_impressions:
            continue
        page_query_data.append({
            "page":        keys[0],
            "query":       keys[1],
            "clicks":      int(r.get("clicks", 0)),
            "impressions": impressions,
            "position":    round(r.get("position", 0), 1),
        })

    if not page_query_data:
        return {"conflicts": [], "total_conflicts": 0, "queries_analysed": 0,
                "message": "No data found. Try lowering the minimum impressions threshold."}

    # Find queries appearing on 2+ pages
    query_pages: dict = defaultdict(list)
    for row in page_query_data:
        query_pages[row["query"]].append(row)
    conflict_queries = {q: pages for q, pages in query_pages.items() if len(pages) >= 2}

    if not conflict_queries:
        return {"conflicts": [], "total_conflicts": 0, "queries_analysed": len(page_query_data),
                "message": "No cannibalization detected — no queries appear on multiple pages."}

    # LLM Step 1: semantically cluster conflict queries
    query_list = list(conflict_queries.keys())[:200]
    cluster_system = (
        "You are an SEO specialist. Group these search queries into semantic clusters by search intent.\n"
        "Only group queries that clearly target the same topic/intent. Be conservative.\n"
        "LANGUAGE: Use the same language as the queries.\n"
        "Return ONLY valid JSON:\n"
        '{"clusters": [{"name": "cluster topic name", "queries": ["query1", "query2"]}]}'
    )
    cluster_text, _, _ = await call_llm_for_schema(
        provider=req.provider, model=req.model,
        system_prompt=cluster_system,
        user_content="Search queries:\n" + "\n".join(f"- {q}" for q in query_list),
        max_tokens=4096, prefill="{",
    )
    cluster_data = _extract_json(cluster_text, label="cannibalization_clustering")
    clusters = cluster_data.get("clusters", []) if isinstance(cluster_data, dict) else []
    if not clusters:
        clusters = [{"name": q, "queries": [q]} for q in query_list[:50]]

    # Build conflict list per cluster
    conflicts = []
    for cluster in clusters:
        cluster_queries = cluster.get("queries", [])
        page_stats: dict = defaultdict(lambda: {"clicks": 0, "impressions": 0, "positions": [], "queries": set()})
        for cq in cluster_queries:
            if cq not in conflict_queries:
                continue
            for row in conflict_queries[cq]:
                s = page_stats[row["page"]]
                s["clicks"]      += row["clicks"]
                s["impressions"] += row["impressions"]
                s["positions"].append(row["position"])
                s["queries"].add(cq)
        if len(page_stats) < 2:
            continue
        pages_ranked = sorted(
            [{"url": url, "clicks": s["clicks"], "impressions": s["impressions"],
              "avg_position": round(sum(s["positions"]) / len(s["positions"]), 1) if s["positions"] else 99,
              "query_count": len(s["queries"])}
             for url, s in page_stats.items()],
            key=lambda x: (x["avg_position"], -x["clicks"])
        )
        conflicts.append({
            "cluster_name": cluster.get("name", ""),
            "queries":      list(cluster_queries),
            "winner":       pages_ranked[0],
            "losers":       pages_ranked[1:],
            "recommendation": None,
        })

    # LLM Step 2: recommendations for all conflicts in one call
    if conflicts:
        rec_system = (
            "You are an SEO specialist analyzing keyword cannibalization.\n"
            "For each conflict, choose ONE action and explain in 1-2 sentences.\n"
            "Actions:\n"
            "- redirect: loser is clearly inferior → recommend 301 to winner\n"
            "- consolidate: merge valuable content from loser into winner → 301\n"
            "- noindex: loser has unique value but must not rank for these queries\n"
            "- differentiate: rewrite loser to target a different search angle\n"
            "- keep_both: pages serve distinct purposes, optimise each separately\n"
            "LANGUAGE: Use the same language as the queries and URLs.\n"
            "Return ONLY valid JSON:\n"
            '{"recommendations": [{"cluster_name": "...", "action": "...", "reasoning": "...", "priority": "high|medium|low"}]}'
        )
        summaries = []
        for c in conflicts[:20]:
            w = c["winner"]
            losers_txt = "; ".join(f"{l['url']} (pos {l['avg_position']}, {l['clicks']} clicks)" for l in c["losers"])
            summaries.append(
                f"Cluster: {c['cluster_name']}\n"
                f"Queries: {', '.join(c['queries'][:5])}\n"
                f"Winner: {w['url']} (pos {w['avg_position']}, {w['clicks']} clicks)\n"
                f"Competing: {losers_txt}"
            )
        rec_text, _, _ = await call_llm_for_schema(
            provider=req.provider, model=req.model,
            system_prompt=rec_system,
            user_content="\n\n---\n\n".join(summaries),
            max_tokens=4096, prefill="{",
        )
        rec_data = _extract_json(rec_text, label="cannibalization_recommendations")
        recs     = rec_data.get("recommendations", []) if isinstance(rec_data, dict) else []
        rec_map  = {r["cluster_name"]: r for r in recs}
        for c in conflicts:
            r = rec_map.get(c["cluster_name"])
            c["recommendation"] = {
                "action":    r.get("action", "differentiate") if r else "differentiate",
                "reasoning": r.get("reasoning", "") if r else "",
                "priority":  r.get("priority", "medium") if r else "medium",
            }

    return {
        "conflicts":        conflicts,
        "total_conflicts":  len(conflicts),
        "queries_analysed": len(query_list),
        "min_impressions":  req.min_impressions,
    }


# ── Standalone Optimize ────────────────────────────────────────────────────────

class StandaloneOptimizeRequest(BaseModel):
    url:          str
    queries:      List[dict]      = []    # [{query, clicks, impressions, ctr, position}]
    provider:     str             = "anthropic"
    model:        str             = "claude-haiku-4-5-20251001"
    audit_types:  List[str]       = ["SEO_AUDIT", "GEO_AUDIT", "FAQ_KEYWORDS", "SCHEMA_GEN"]
    page_content: Optional[str]   = None  # if provided, skip scraping entirely


@router.post("/optimize")
async def standalone_optimize(req: StandaloneOptimizeRequest):
    """Create an LLM optimization job without requiring a GSC property.

    Accepts a URL + manually specified queries (can be empty).
    Poll GET /api/guide/{guide_id} for results.
    """
    # Re-use PageOptimizeRequest + _run_page_optimize by passing queries as selected_queries
    inner_req = PageOptimizeRequest(
        url=req.url,
        provider=req.provider,
        model=req.model,
        audit_types=req.audit_types,
        selected_queries=req.queries if req.queries else [],
        page_content=req.page_content,
    )
    async with AsyncSessionLocal() as db:
        guide = UrlGuide(
            url=req.url,
            status="pending",
            provider=req.provider,
            model=req.model,
            gsc_property_id=None,
        )
        db.add(guide)
        await db.commit()
        await db.refresh(guide)
        guide_id = guide.id

    async def _guarded_standalone(gid, r):
        try:
            await asyncio.wait_for(_run_page_optimize(gid, None, r), timeout=300)
        except asyncio.TimeoutError:
            async with AsyncSessionLocal() as db:
                g = await db.get(UrlGuide, gid)
                if g and g.status in ("pending", "running"):
                    g.status = "failed"
                    g.error_message = "Timed out after 5 minutes"
                    g.updated_at = datetime.utcnow()
                    await db.commit()

    asyncio.create_task(_guarded_standalone(guide_id, inner_req))
    return {"guide_id": guide_id, "status": "pending"}
