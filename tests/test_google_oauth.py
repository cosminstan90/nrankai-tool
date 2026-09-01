"""
Unit tests for api.utils.google_oauth — the shared Google OAuth token
exchange/refresh mechanics extracted from three previously-duplicated
implementations (docs/audit/04-integrations.md section C,
docs/CONSOLIDATION_PLAN.md Etapa 2.5).

The key behavior under test: refresh_google_token() must raise
GoogleOAuthInvalidGrantError specifically on an `invalid_grant` response,
distinguishable from any other failure, so every calling flow can react the
same way (clear the dead token, prompt reconnect) instead of leaking a raw
HTTP error or retrying forever.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from api.utils.google_oauth import (
    GoogleOAuthInvalidGrantError,
    exchange_code_for_tokens,
    refresh_google_token,
)


def _mock_response(status_code: int, json_body: dict):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_body

    def _raise_for_status():
        if status_code >= 400:
            raise httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
    resp.raise_for_status.side_effect = _raise_for_status
    return resp


class TestExchangeCodeForTokens(unittest.IsolatedAsyncioTestCase):
    @patch("api.utils.google_oauth.httpx.AsyncClient")
    async def test_successful_exchange_returns_tokens(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(200, {"access_token": "abc", "refresh_token": "xyz"})
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await exchange_code_for_tokens(
            code="somecode", client_id="cid", client_secret="secret", redirect_uri="http://x/cb"
        )
        self.assertEqual(result["access_token"], "abc")

    @patch("api.utils.google_oauth.httpx.AsyncClient")
    async def test_failed_exchange_raises_http_error(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(400, {"error": "invalid_grant"})
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        with self.assertRaises(httpx.HTTPStatusError):
            await exchange_code_for_tokens(
                code="badcode", client_id="cid", client_secret="secret", redirect_uri="http://x/cb"
            )


class TestRefreshGoogleToken(unittest.IsolatedAsyncioTestCase):
    @patch("api.utils.google_oauth.httpx.AsyncClient")
    async def test_successful_refresh_returns_new_token(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(200, {"access_token": "new-token"})
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = await refresh_google_token(refresh_token="rt", client_id="cid", client_secret="secret")
        self.assertEqual(result["access_token"], "new-token")

    @patch("api.utils.google_oauth.httpx.AsyncClient")
    async def test_invalid_grant_raises_specific_error(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(400, {"error": "invalid_grant"})
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        with self.assertRaises(GoogleOAuthInvalidGrantError):
            await refresh_google_token(refresh_token="dead-token", client_id="cid", client_secret="secret")

    @patch("api.utils.google_oauth.httpx.AsyncClient")
    async def test_other_400_does_not_raise_invalid_grant_error(self, mock_client_cls):
        """A 400 for a different reason (e.g. malformed request) should not be
        mistaken for a dead refresh token — it should surface as a plain HTTP error."""
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(400, {"error": "invalid_request"})
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        with self.assertRaises(httpx.HTTPStatusError):
            await refresh_google_token(refresh_token="rt", client_id="cid", client_secret="secret")

    @patch("api.utils.google_oauth.httpx.AsyncClient")
    async def test_server_error_does_not_raise_invalid_grant_error(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.post.return_value = _mock_response(500, {})
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        with self.assertRaises(httpx.HTTPStatusError):
            await refresh_google_token(refresh_token="rt", client_id="cid", client_secret="secret")


if __name__ == "__main__":
    unittest.main()
