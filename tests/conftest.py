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

The temp DB deliberately does NOT use tempfile's default location
(`%TEMP%`/`$TMPDIR`): on this dev machine that resolves to the Windows system
drive, which was found completely full (0 bytes free) while chasing what
looked like flaky tests -- SQLite was intermittently failing mid-CREATE TABLE
with "database or disk is full", by chance, depending on what else needed a
byte at that moment. Using a directory next to the repo instead sidesteps
that specific drive's state entirely, and is more portable than hardcoding a
drive letter: wherever this repo is checked out, its temp test DB lives on
the same volume as the repo itself, not on whatever the OS happens to default
%TEMP% to. This does not fix a full system drive -- it only stops the test
suite from depending on it.
"""

import os
import pathlib
import shutil
import tempfile

_TEST_TMP_ROOT = pathlib.Path(__file__).parent.parent / ".pytest_tmp"
_TEST_TMP_ROOT.mkdir(exist_ok=True)
_TMP_DIR = tempfile.mkdtemp(prefix="geo_tool_tests_", dir=str(_TEST_TMP_ROOT))
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

    # Windows can briefly hold a file handle on the just-disposed sqlite/WAL
    # files open past dispose() returning, so a single rmtree attempt right
    # away sometimes hits PermissionError ("used by another process") on the
    # -db file itself, not just an empty leftover directory. Must actually
    # catch that on every attempt but the last -- ignore_errors=False on an
    # early attempt still raises immediately, which skips the retry/sleep
    # entirely (caught live: this exact bug surfaced as a spurious teardown
    # error attributed to whatever test happened to run last in the session,
    # since this fixture is session-scoped). Now living under the repo (see
    # module docstring for why), leftovers here are visible clutter, not just
    # invisible noise in whatever %TEMP% happened to be.
    import time
    for attempt in range(3):
        try:
            shutil.rmtree(_TMP_DIR)
            break
        except FileNotFoundError:
            break
        except OSError:
            if attempt == 2:
                break  # give up quietly; a stray dir under .pytest_tmp/ is cosmetic, not a leak of real data
            time.sleep(0.5)
