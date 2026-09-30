"""
Database engine, session, and Base for ORM models.

Imported by domain model files to avoid circular imports.
"""

import logging
import os
import time

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

# Database file location
DATABASE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
os.makedirs(DATABASE_DIR, exist_ok=True)
# GEO_TOOL_DB_PATH lets the test suite point at a throwaway database.
# Read at import time, so anything overriding it must do so before this
# module is first imported (see tests/conftest.py).
DATABASE_PATH = os.environ.get("GEO_TOOL_DB_PATH") or os.path.join(DATABASE_DIR, "analyzer.db")

# Async SQLite URL
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

# One connection per session.
#
# These engines used to be built with StaticPool, which hands every session the
# same physical SQLite connection. Sessions then shared one transaction without
# knowing it: a session that merely read and closed rolled back another
# session's flushed-but-uncommitted writes, and the owner's later commit
# "succeeded" with nothing in it. That is how a 545-page snapshot capture ended
# "completed, 545" with 0 rows saved; it also deadlocked two concurrent
# captures and turned a cancelled task into "Cannot operate on a closed
# database" for everyone else. tests/test_db_session_isolation.py reproduces it.
#
# NullPool rather than a queue pool: sessions are opened from several event
# loops (worker threads, the test suite), and the async queue pool is bound to
# the loop that created it. Opening a local SQLite file costs well under a
# millisecond.
#
# With separate connections SQLite's single-writer rule applies for real, so a
# writer that finds the database locked waits up to BUSY_TIMEOUT_S instead of
# failing at once. A session that keeps a write transaction open across a slow
# call (an LLM, a crawl) now makes other writers wait and, past the timeout,
# fail loudly with "database is locked" -- commit before slow work.
BUSY_TIMEOUT_S = 30

engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_S},
    poolclass=NullPool,
    echo=False
)

# Sync engine for migrations
sync_engine = create_engine(
    f"sqlite:///{DATABASE_PATH}",
    connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_S},
    poolclass=NullPool
)


def _enable_wal(dbapi_connection, connection_record):
    """WAL lets readers proceed while one connection writes. Persistent in the
    file, so this is a no-op on analyzer.db; it matters for fresh databases
    such as the test suite's. foreign_keys is set in api/models/database.py."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


event.listen(engine.sync_engine, "connect", _enable_wal)
event.listen(sync_engine, "connect", _enable_wal)

# Warn when a session holds SQLite's write lock too long.
#
# Since NullPool (see above), a writer that finds the database locked waits
# up to BUSY_TIMEOUT_S instead of losing data -- but a session that commits
# an LLM call or a crawl step *before* its own write is a bug, not a fixed
# hazard, and used to fail silently. This surfaces it as a log line instead
# of waiting for someone to notice "database is locked" in production.
#
# conn.info is a per-Connection-checkout dict: with NullPool each session
# gets its own DBAPI connection, so different sessions never share it. A
# single long-lived session that commits several times (some worker loops
# do) gets timed per write burst -- the entry is cleared on every commit or
# rollback, whether or not it crossed the threshold.
WRITE_TXN_WARN_S = 5.0
_WRITE_STMT_KEYWORDS = ("INSERT", "UPDATE", "DELETE")


def _mark_first_write(conn, cursor, statement, parameters, context, executemany):
    if "_write_txn_started_at" in conn.info:
        return
    if statement.lstrip()[:6].upper() in _WRITE_STMT_KEYWORDS:
        conn.info["_write_txn_started_at"] = time.monotonic()
        conn.info["_write_txn_statement"] = statement.strip()[:120]


def _check_write_txn_duration(conn):
    started_at = conn.info.pop("_write_txn_started_at", None)
    statement = conn.info.pop("_write_txn_statement", None)
    if started_at is None:
        return
    duration = time.monotonic() - started_at
    if duration > WRITE_TXN_WARN_S:
        logger.warning(
            "Write transaction held open for %.1fs (threshold %.1fs) -- "
            "commit before slow work (LLM calls, HTTP, crawling). Statement: %s",
            duration, WRITE_TXN_WARN_S, statement,
        )


event.listen(engine.sync_engine, "before_cursor_execute", _mark_first_write)
event.listen(engine.sync_engine, "commit", _check_write_txn_duration)
event.listen(engine.sync_engine, "rollback", _check_write_txn_duration)

# Session factory
AsyncSessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# Base class for all ORM models
Base = declarative_base()
