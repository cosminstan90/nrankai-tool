"""
Point the whole test suite at a throwaway database.

Until this existed there was no isolation at all: tests imported
AsyncSessionLocal and wrote straight into api/data/analyzer.db, the real
production database. They created GscProperty and audit rows there and
committed them. Teardowns that deleted a parent relied on ON DELETE CASCADE,
which was inert because FK enforcement never reached the async engine, so
each run permanently stranded 3 orphan rows -- 60 of them had piled up in
gsc_page_rows by the time it was noticed.

The cascade is fixed now, so teardowns do clean up, but tests were still
writing to production. This removes that entirely.

GEO_TOOL_DB_PATH must be set before anything imports api.models._base: that
module reads it at import time and builds its engines from it once. pytest
imports conftest.py ahead of every test module, so setting it at module level
here is early enough -- provided this file imports nothing from api itself
until inside a fixture.
"""

import os
import pathlib
import shutil
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="geo_tool_tests_")
os.environ["GEO_TOOL_DB_PATH"] = str(pathlib.Path(_TMP_DIR) / "test_analyzer.db")

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    """Build the schema in the temp DB, then remove it when the run ends."""
    from api.models._base import Base, engine, sync_engine
    import api.models.database  # noqa: F401  -- registers every model onto Base

    # create_all is enough for a fresh file; init_db()'s extra ALTERs only
    # matter when upgrading an existing database.
    Base.metadata.create_all(bind=sync_engine)

    yield

    sync_engine.dispose()
    try:
        import asyncio
        asyncio.get_event_loop().run_until_complete(engine.dispose())
    except Exception:
        pass
    shutil.rmtree(_TMP_DIR, ignore_errors=True)
