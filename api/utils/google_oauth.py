"""
Shared Google OAuth 2.0 token-exchange and refresh mechanics.

Three GSC-integration flows in this project each grew their own copy of the
"POST to Google's token endpoint" logic, with the token *storage* model
genuinely differing per use case (a single global connection for the main
/gsc sync feature, one connection per Fan-Out project, one per ContentIQ
audit — see docs/audit/04-integrations.md section C). This module extracts
only the part that was actually duplicated: the raw HTTP mechanics and,
critically, consistent detection of `invalid_grant` (an expired/revoked
refresh token) so every flow can react the same way — clear the dead token
and prompt the user to reconnect — instead of leaking a raw HTTP error or
(worse) silently continuing to use tokens that will never work again.

Each flow keeps its own storage/session model and calls these functions.
"""
from __future__ import annotations

from typing import Optional

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleOAuthInvalidGrantError(Exception):
    """Raised when Google rejects a refresh_token as invalid/expired/revoked.

    The stored token is unrecoverable — callers should delete it and prompt
    the user to reconnect, rather than retrying or surfacing a raw HTTP error.
    """


async def exchange_code_for_tokens(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code_verifier: Optional[str] = None,
    token_url: str = TOKEN_URL,
) -> dict:
    """Exchange an OAuth authorization code for an access/refresh token pair.

    Raises httpx.HTTPStatusError on any non-2xx response (including a
    malformed/expired code) — that failure is not recoverable by retrying
    with a fresh refresh token, so it is not folded into
    GoogleOAuthInvalidGrantError; callers should surface it as a one-off
    "connection failed, try again" error.
    """
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(token_url, data=payload)
        resp.raise_for_status()
        return resp.json()


async def refresh_google_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
    token_url: str = TOKEN_URL,
) -> dict:
    """Refresh an expired access token.

    Raises GoogleOAuthInvalidGrantError if Google reports the refresh token
    itself is dead (expired, revoked, or the user removed app access) — the
    one case every calling flow needs to treat the same way: delete the
    stored token, don't retry.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(token_url, data={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        })

    if resp.status_code == 400:
        try:
            error_code = resp.json().get("error", "")
        except Exception:
            error_code = ""
        if error_code == "invalid_grant":
            raise GoogleOAuthInvalidGrantError(
                "Google rejected the refresh token (invalid_grant) — it is expired, "
                "revoked, or access was removed. Reconnect required."
            )

    resp.raise_for_status()
    return resp.json()
