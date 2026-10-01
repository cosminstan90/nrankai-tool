"""
_get_gsc_credentials() must hand google-auth a NAIVE UTC expiry.

google-auth's Credentials.expired compares the expiry against
google.auth._helpers.utcnow(), which strips tzinfo. The code used to make
the expiry timezone-aware instead, so .expired raised TypeError, the
fallback treated that as "expired", and every GSC call forced a refresh.
Uses the real google-auth Credentials class; only the DB row and the
network refresh are faked.
"""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from api.routes.gsc import _shared


def _token_row(expiry):
    return SimpleNamespace(access_token="access", refresh_token="refresh", token_expiry=expiry)


class TestGscTokenExpiry(unittest.IsolatedAsyncioTestCase):
    async def _creds_for(self, expiry):
        refresh = MagicMock()
        with patch.object(_shared, "_load_token", AsyncMock(return_value=_token_row(expiry))), \
             patch("google.oauth2.credentials.Credentials.refresh", refresh):
            creds = await _shared._get_gsc_credentials()
        return creds, refresh

    async def test_a_valid_aware_expiry_does_not_force_a_refresh(self):
        """What the DB now returns (UTCDateTime): aware, an hour from now."""
        creds, refresh = await self._creds_for(datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertIsNone(creds.expiry.tzinfo)   # naive, as google-auth requires
        self.assertFalse(creds.expired)          # no TypeError
        refresh.assert_not_called()

    async def test_a_valid_naive_expiry_does_not_force_a_refresh(self):
        naive_utc = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)
        creds, refresh = await self._creds_for(naive_utc)
        self.assertFalse(creds.expired)
        refresh.assert_not_called()

    async def test_an_expired_token_still_refreshes(self):
        creds, refresh = await self._creds_for(datetime.now(timezone.utc) - timedelta(minutes=1))
        refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
