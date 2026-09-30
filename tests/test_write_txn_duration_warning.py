"""
Pasul 3 of docs/superpowers/plans/2026-09-30-next-steps.md.

After api/models/_base.py stopped sharing one SQLite connection across every
session (StaticPool -> NullPool, tests/test_db_session_isolation.py), SQLite's
real single-writer rule applies: a session that holds a write transaction open
across a slow call (an LLM, a crawl) now makes other writers wait, and after
BUSY_TIMEOUT_S fail with "database is locked". That's a correct failure, but
a silent one -- nothing said which session was slow until it started blocking
others. This warns as soon as a write transaction runs long, before it ever
becomes a lock conflict.
"""
import asyncio
import unittest
from unittest.mock import patch

from sqlalchemy import text

from api.models import _base
from api.models._base import AsyncSessionLocal


class TestWriteTransactionDurationWarning(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with AsyncSessionLocal() as db:
            await db.execute(text("CREATE TABLE IF NOT EXISTS _write_txn_probe (x INTEGER)"))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            await db.execute(text("DROP TABLE IF EXISTS _write_txn_probe"))
            await db.commit()

    async def test_a_slow_write_transaction_logs_a_warning(self):
        with patch.object(_base, "WRITE_TXN_WARN_S", 0.05):
            with self.assertLogs("api.models._base", level="WARNING") as logs:
                async with AsyncSessionLocal() as db:
                    await db.execute(text("INSERT INTO _write_txn_probe VALUES (1)"))
                    await asyncio.sleep(0.15)
                    await db.commit()

        self.assertTrue(any("Write transaction held open" in m for m in logs.output), logs.output)

    async def test_a_fast_write_transaction_does_not_log_a_warning(self):
        with patch.object(_base, "WRITE_TXN_WARN_S", 5.0):
            async with self._assert_no_warning_logged():
                async with AsyncSessionLocal() as db:
                    await db.execute(text("INSERT INTO _write_txn_probe VALUES (2)"))
                    await db.commit()

    async def test_a_read_only_session_never_logs_a_warning(self):
        with patch.object(_base, "WRITE_TXN_WARN_S", 0.0):
            async with self._assert_no_warning_logged():
                async with AsyncSessionLocal() as db:
                    await db.execute(text("SELECT * FROM _write_txn_probe"))
                    await asyncio.sleep(0.05)
                    await db.commit()

    async def test_the_timer_resets_after_each_commit_in_a_long_lived_session(self):
        """A long scan that commits repeatedly must not accumulate one session's whole lifetime."""
        with patch.object(_base, "WRITE_TXN_WARN_S", 5.0):
            async with self._assert_no_warning_logged():
                async with AsyncSessionLocal() as db:
                    for i in range(3):
                        await db.execute(text("INSERT INTO _write_txn_probe VALUES (:v)"), {"v": i})
                        await db.commit()
                        await asyncio.sleep(0.05)   # idle between writes, not held mid-transaction

    class _assert_no_warning_logged:
        """assertNoLogs is Python 3.10+; this project's runtime may be older -- roll our own."""
        async def __aenter__(self):
            import logging
            self._handler = logging.Handler()
            self._records = []
            self._handler.emit = self._records.append
            self._logger = logging.getLogger("api.models._base")
            self._logger.addHandler(self._handler)
            self._prev_level = self._logger.level
            self._logger.setLevel(logging.WARNING)
            return self

        async def __aexit__(self, *exc):
            self._logger.removeHandler(self._handler)
            self._logger.setLevel(self._prev_level)
            warnings = [r for r in self._records if r.levelno >= logging.WARNING]
            assert not warnings, f"unexpected warning(s) logged: {[r.getMessage() for r in warnings]}"
            return False


if __name__ == "__main__":
    unittest.main()
