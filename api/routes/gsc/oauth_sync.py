"""GSC OAuth 2.0 endpoints and live sync from Google API."""

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
from api.utils.errors import raise_not_found, raise_bad_request
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
        raise_bad_request("Google OAuth credentials not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env")
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
            raise_not_found("Property")
        site_url = prop.site_url

    end_date   = datetime.now(timezone.utc).date()
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
                last_synced_at=datetime.now(timezone.utc),
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
            raise_not_found("Property")
        site_url = prop.site_url

    creds = await _get_gsc_credentials()
    if not creds:
        return {"queries": [], "source": "none",
                "message": "Connect a Google account to see per-page queries."}

    try:
        from googleapiclient.discovery import build
    except ImportError:
        raise HTTPException(500, "google-api-python-client not installed")

    end_date   = datetime.now(timezone.utc).date()
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


