"""
Every session gets its own SQLite connection.

api/models/_base.py used to build its engines with StaticPool, so all sessions
shared one physical connection -- and therefore one transaction. Reproduced
before the fix, exactly as the first test below does it: session A inserts a
row, session B merely reads and closes, A commits without error, and the row
is gone. B's close rolled back the shared connection, taking A's pending write
with it. That is how a 545-page snapshot capture finished "completed, 545"
with 0 rows saved. B could also read A's uncommitted row.
"""
import asyncio
import unittest

from sqlalchemy import text

from api.models._base import AsyncSessionLocal


class TestSessionIsolation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async with AsyncSessionLocal() as db:
            await db.execute(text("CREATE TABLE IF NOT EXISTS _pool_probe (x INTEGER)"))
            await db.execute(text("DELETE FROM _pool_probe"))
            await db.commit()

    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            await db.execute(text("DROP TABLE IF EXISTS _pool_probe"))
            await db.commit()

    async def _count(self):
        async with AsyncSessionLocal() as db:
            return (await db.execute(text("SELECT count(*) FROM _pool_probe"))).scalar()

    async def test_another_session_closing_does_not_discard_pending_writes(self):
        async with AsyncSessionLocal() as a:
            await a.execute(text("INSERT INTO _pool_probe VALUES (1)"))
            async with AsyncSessionLocal() as b:
                await b.execute(text("SELECT 1"))
            await a.commit()
        self.assertEqual(await self._count(), 1)

    async def test_uncommitted_writes_are_invisible_to_other_sessions(self):
        async with AsyncSessionLocal() as a:
            await a.execute(text("INSERT INTO _pool_probe VALUES (1)"))
            self.assertEqual(await self._count(), 0)
            await a.rollback()
        self.assertEqual(await self._count(), 0)

    async def test_concurrent_writers_wait_for_each_other_instead_of_failing(self):
        """The second writer blocks on SQLite's write lock until the first commits."""
        first_has_lock = asyncio.Event()

        async def first():
            async with AsyncSessionLocal() as db:
                await db.execute(text("INSERT INTO _pool_probe VALUES (1)"))
                first_has_lock.set()
                await asyncio.sleep(0.5)
                await db.commit()

        async def second():
            await first_has_lock.wait()
            async with AsyncSessionLocal() as db:
                await db.execute(text("INSERT INTO _pool_probe VALUES (2)"))
                await db.commit()

        await asyncio.gather(first(), second())
        self.assertEqual(await self._count(), 2)

    async def test_cancelling_one_session_leaves_the_others_working(self):
        """
        The closed-database failure seen live did not reproduce in this simple
        form under StaticPool; this pins the behaviour that must hold now: a
        cancelled session's write is dropped and nobody else is affected.
        """
        started = asyncio.Event()

        async def victim():
            async with AsyncSessionLocal() as db:
                await db.execute(text("INSERT INTO _pool_probe VALUES (1)"))
                started.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(victim())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        async with AsyncSessionLocal() as db:
            await db.execute(text("INSERT INTO _pool_probe VALUES (2)"))
            await db.commit()
        self.assertEqual(await self._count(), 1)

    async def test_connection_pragmas(self):
        async with AsyncSessionLocal() as db:
            self.assertEqual((await db.execute(text("PRAGMA journal_mode"))).scalar(), "wal")
            self.assertEqual((await db.execute(text("PRAGMA foreign_keys"))).scalar(), 1)
            self.assertEqual((await db.execute(text("PRAGMA busy_timeout"))).scalar(), 30000)


if __name__ == "__main__":
    unittest.main()
