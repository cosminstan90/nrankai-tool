"""
ContentIQ GSC OAuth + Metrics Fetcher (Prompt 04)
===================================================
OAuth flow + per-URL GSC performance data for ContentIQ audits.
Stores tokens in ciq_gsc_tokens table via SQLAlchemy async session.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import quote, urlencode

logger = logging.getLogger("contentiq.gsc")

_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPES    = ["https://www.googleapis.com/auth/webmasters.readonly"]


class GSCAuthError(Exception):
    pass


def _client_id()     -> str: return os.getenv("GSC_CLIENT_ID", os.getenv("GOOGLE_CLIENT_ID", ""))
def _client_secret() -> str: return os.getenv("GSC_CLIENT_SECRET", os.getenv("GOOGLE_CLIENT_SECRET", ""))
def _redirect_uri()  -> str:
    base = os.getenv("APP_BASE_URL", "http://localhost:8000")
    return os.getenv("GSC_REDIRECT_URI", f"{base}/content-iq/gsc/callback")


def get_oauth_url(state: str) -> str:
    """Build Google OAuth authorization URL."""
    params = {
        "client_id":     _client_id(),
        "redirect_uri":  _redirect_uri(),
        "response_type": "code",
        "scope":         " ".join(_SCOPES),
        "access_type":   "offline",
        "prompt":        "consent",
        "state":         state,
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """Exchange OAuth authorization code for tokens."""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(_TOKEN_URL, data={
            "code":          code,
            "client_id":     _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri":  _redirect_uri(),
            "grant_type":    "authorization_code",
        })
        r.raise_for_status()
        return r.json()


async def refresh_access_token(refresh_token: str) -> str:
    """Refresh an expired access token. Returns new access_token."""
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(_TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id":     _client_id(),
            "client_secret": _client_secret(),
            "grant_type":    "refresh_token",
        })
        r.raise_for_status()
        data = r.json()
        return data["access_token"]


async def save_tokens(audit_id: int, tokens: dict, property_url: str, db) -> None:
    """Upsert GSC tokens for an audit."""
    from sqlalchemy import select
    from api.models.contentiq import CiqGscToken

    expires_at = None
    if "expires_in" in tokens:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=tokens["expires_in"])

    existing = (await db.execute(
        select(CiqGscToken).where(CiqGscToken.audit_id == audit_id)
    )).scalar_one_or_none()

    if existing:
        existing.access_token  = tokens.get("access_token", existing.access_token)
        if tokens.get("refresh_token"):
            existing.refresh_token = tokens["refresh_token"]
        existing.expires_at  = expires_at
        existing.property_url = property_url or existing.property_url
    else:
        db.add(CiqGscToken(
            audit_id      = audit_id,
            access_token  = tokens.get("access_token"),
            refresh_token = tokens.get("refresh_token"),
            expires_at    = expires_at,
            property_url  = property_url,
        ))
    await db.commit()


async def load_tokens(audit_id: int, db) -> Optional["CiqGscToken"]:
    """Load GSC tokens for an audit. Returns None if not found."""
    from sqlalchemy import select
    from api.models.contentiq import CiqGscToken
    return (await db.execute(
        select(CiqGscToken).where(CiqGscToken.audit_id == audit_id)
    )).scalar_one_or_none()


async def _get_valid_token(token_row, db) -> str:
    """Return a valid access token, refreshing if expired."""
    now = datetime.now(timezone.utc)
    if token_row.expires_at and token_row.expires_at.replace(tzinfo=timezone.utc) < now + timedelta(minutes=5):
        if not token_row.refresh_token:
            raise GSCAuthError("Token expired and no refresh token available")
        new_token = await refresh_access_token(token_row.refresh_token)
        token_row.access_token = new_token
        token_row.expires_at   = now + timedelta(hours=1)
        await db.commit()
        return new_token
    return token_row.access_token


async def get_page_metrics(
    access_token: str,
    property_url: str,
    urls: List[str],
    date_range_days: int = 90,
) -> Dict[str, dict]:
    """
    Fetch per-URL GSC click/impression/CTR/position data.
    Returns dict mapping url -> {clicks, impressions, ctr, position}.
    """
    import httpx
    from datetime import date

    empty = {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    result: Dict[str, dict] = {u: dict(empty) for u in urls}

    end_date   = date.today()
    start_date = end_date - timedelta(days=date_range_days)
    endpoint   = (
        f"https://searchconsole.googleapis.com/v1/sites/"
        f"{quote(property_url, safe='')}/searchAnalytics/query"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
    }

    url_set = {u.rstrip("/") for u in urls}
    start_row = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            body = {
                "startDate":  start_date.isoformat(),
                "endDate":    end_date.isoformat(),
                "dimensions": ["page"],
                "rowLimit":   25000,
                "startRow":   start_row,
            }
            try:
                r = await client.post(endpoint, json=body, headers=headers)
                if r.status_code == 401:
                    raise GSCAuthError("GSC auth failed — token may be expired")
                r.raise_for_status()
                data = r.json()
            except GSCAuthError:
                raise
            except Exception as exc:
                logger.warning("GSC API error: %s", exc)
                break

            rows = data.get("rows", [])
            if not rows:
                break

            for row in rows:
                page_url = (row.get("keys") or [""])[0].rstrip("/")
                if page_url in url_set:
                    result[page_url + "/"] = result.get(page_url, dict(empty))
                    result[page_url] = {
                        "clicks":      int(row.get("clicks", 0)),
                        "impressions": int(row.get("impressions", 0)),
                        "ctr":         round(row.get("ctr", 0), 4),
                        "position":    round(row.get("position", 0), 1),
                    }

            if len(rows) < 25000:
                break
            start_row += 25000

    return result
