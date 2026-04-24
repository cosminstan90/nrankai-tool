"""Shared OAuth helpers and credentials for GSC routes."""

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
                row.updated_at   = datetime.now(timezone.utc)
                await db.commit()

    return creds


