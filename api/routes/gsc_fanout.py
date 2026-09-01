"""
GSC Fanout Integration (Prompt 27)
====================================
OAuth flow + cross-reference endpoint for linking GSC data with fan-out sessions.

Endpoints:
  GET    /api/gsc-fanout/connect/{project_id}     → OAuth redirect
  GET    /api/gsc-fanout/callback                 → save tokens
  GET    /api/gsc-fanout/status/{project_id}
  POST   /api/gsc-fanout/crossref                 → run crossref
  DELETE /api/gsc-fanout/disconnect/{project_id}
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, GscFanoutConnection, FanoutSession, FanoutQuery
from api.utils.errors import raise_not_found, raise_bad_request
from api.utils.google_oauth import (
    exchange_code_for_tokens,
    refresh_google_token,
    GoogleOAuthInvalidGrantError,
)

logger = logging.getLogger("gsc_fanout")
router = APIRouter(prefix="/api/gsc-fanout", tags=["gsc-fanout"])

_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def _client_id()     -> str: return os.getenv("GOOGLE_CLIENT_ID", "")
def _client_secret() -> str: return os.getenv("GOOGLE_CLIENT_SECRET", "")
def _redirect_uri()  -> str:
    base = os.getenv("APP_BASE_URL", "http://localhost:8000")
    return f"{base}/api/gsc-fanout/callback"


def _make_oauth_state(project_id: str) -> str:
    """Return a signed state token: '{project_id}:{nonce}:{sig}'."""
    nonce = secrets.token_urlsafe(16)
    payload = f"{project_id}:{nonce}"
    secret = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{project_id}:{nonce}:{sig}"


def _verify_oauth_state(state: str) -> str:
    """Return project_id if the state signature is valid; raise ValueError otherwise."""
    parts = state.split(":")
    if len(parts) != 3:
        raise ValueError("Invalid OAuth state format")
    project_id, nonce, sig = parts
    payload = f"{project_id}:{nonce}"
    secret = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    if not secrets.compare_digest(sig, expected):
        raise ValueError("OAuth state signature mismatch — possible CSRF")
    return project_id


async def _get_valid_access_token(conn: GscFanoutConnection, db: AsyncSession) -> str:
    """Return a valid access token for this connection, refreshing if near expiry.

    Previously this connection's access_token was used as-is with no refresh
    at all, so cross-reference calls would silently start failing with 401s
    roughly an hour after connecting. Raises ValueError (via raise_bad_request
    at the call site) if the refresh token itself is dead.
    """
    now = datetime.now(timezone.utc)
    expiry = conn.token_expiry
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if expiry and expiry > now + timedelta(minutes=5):
        return conn.access_token

    if not conn.refresh_token:
        raise GoogleOAuthInvalidGrantError("No refresh token stored — reconnect required.")

    tokens = await refresh_google_token(
        refresh_token=conn.refresh_token,
        client_id=_client_id(),
        client_secret=_client_secret(),
    )
    conn.access_token = tokens["access_token"]
    if "expires_in" in tokens:
        conn.token_expiry = now + timedelta(seconds=tokens["expires_in"])
    conn.updated_at = now
    await db.commit()
    return conn.access_token


@router.get("/connect/{project_id}")
async def gsc_connect(project_id: str):
    """Redirect to Google OAuth to authorise GSC access for a project."""
    if not _client_id():
        raise_bad_request("GOOGLE_CLIENT_ID not configured")

    from urllib.parse import urlencode
    params = {
        "client_id":     _client_id(),
        "redirect_uri":  _redirect_uri(),
        "response_type": "code",
        "scope":         " ".join(_SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",
        "state":         _make_oauth_state(project_id),
    }
    url = f"{_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url=url)


@router.get("/callback")
async def gsc_callback(
    code: Optional[str]  = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Handle OAuth callback: exchange code for tokens and persist."""
    if error or not code:
        raise_bad_request(f"OAuth error: {error or 'no code'}")

    try:
        tokens = await exchange_code_for_tokens(
            code=code,
            client_id=_client_id(),
            client_secret=_client_secret(),
            redirect_uri=_redirect_uri(),
        )
    except Exception as exc:
        raise_bad_request(f"Token exchange failed: {exc}")

    try:
        project_id = _verify_oauth_state(state or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state — please try again")

    # Discover GSC property — list sites and take the first verified one
    gsc_property = ""
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://www.googleapis.com/webmasters/v3/sites",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            sites = r.json().get("siteEntry", [])
            for s in sites:
                if s.get("permissionLevel") in ("siteOwner", "siteFullUser", "siteRestrictedUser"):
                    gsc_property = s.get("siteUrl", "")
                    break
    except Exception:
        pass

    # Expiry
    expiry = None
    if "expires_in" in tokens:
        from datetime import timedelta
        expiry = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])

    # Upsert
    existing = (await db.execute(
        select(GscFanoutConnection).where(GscFanoutConnection.project_id == project_id)
    )).scalar_one_or_none()

    if existing:
        existing.access_token  = tokens.get("access_token")
        existing.refresh_token = tokens.get("refresh_token", existing.refresh_token)
        existing.token_expiry  = expiry
        existing.gsc_property  = gsc_property or existing.gsc_property
        existing.updated_at    = datetime.now(timezone.utc)
    else:
        db.add(GscFanoutConnection(
            project_id    = project_id,
            gsc_property  = gsc_property,
            access_token  = tokens.get("access_token"),
            refresh_token = tokens.get("refresh_token"),
            token_expiry  = expiry,
        ))
    await db.commit()

    # Redirect to project dashboard
    return RedirectResponse(url=f"/projects/{project_id}?gsc_connected=1")


@router.get("/status/{project_id}")
async def gsc_status(project_id: str, db: AsyncSession = Depends(get_db)):
    """Return GSC connection status for a project."""
    conn = (await db.execute(
        select(GscFanoutConnection).where(GscFanoutConnection.project_id == project_id)
    )).scalar_one_or_none()

    if not conn:
        return {"connected": False, "project_id": project_id}
    return {**conn.to_dict(), "connected": bool(conn.access_token)}


@router.delete("/disconnect/{project_id}")
async def gsc_disconnect(project_id: str, db: AsyncSession = Depends(get_db)):
    """Remove GSC tokens for a project."""
    conn = (await db.execute(
        select(GscFanoutConnection).where(GscFanoutConnection.project_id == project_id)
    )).scalar_one_or_none()
    if conn:
        conn.access_token  = None
        conn.refresh_token = None
        await db.commit()
    return {"ok": True, "project_id": project_id}


class GscCrossrefRequest(BaseModel):
    session_id: str
    project_id: str
    date_range_days: int = 90


@router.post("/crossref")
async def gsc_crossref(req: GscCrossrefRequest, db: AsyncSession = Depends(get_db)):
    """Cross-reference a fan-out session's queries against GSC data."""
    from sqlalchemy.orm import selectinload
    from api.workers.gsc_fanout_crossref import fetch_gsc_query_data, crossref_fanout_gsc

    # Load GSC connection
    conn = (await db.execute(
        select(GscFanoutConnection).where(GscFanoutConnection.project_id == req.project_id)
    )).scalar_one_or_none()
    if not conn or not conn.access_token:
        raise_bad_request("GSC not connected for this project. Visit /api/gsc-fanout/connect/{project_id}")

    # Load fan-out session queries
    stmt = (
        select(FanoutSession)
        .where(FanoutSession.id == req.session_id)
        .options(selectinload(FanoutSession.queries))
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise_not_found("Fan-out session")

    queries = [q.query_text for q in (session.queries or [])]
    if not queries:
        raise_bad_request("Session has no fan-out queries")

    try:
        access_token = await _get_valid_access_token(conn, db)
    except GoogleOAuthInvalidGrantError:
        conn.access_token = None
        conn.refresh_token = None
        await db.commit()
        raise_bad_request("GSC connection expired — reconnect via /api/gsc-fanout/connect/{project_id}")

    gsc_data = await fetch_gsc_query_data(
        access_token    = access_token,
        gsc_property    = conn.gsc_property,
        queries         = queries,
        date_range_days = req.date_range_days,
    )

    result = crossref_fanout_gsc(queries, gsc_data, session.target_url)
    return {
        "session_id":   req.session_id,
        "project_id":   req.project_id,
        "gsc_property": conn.gsc_property,
        **result,
    }
