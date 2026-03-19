"""
Alembic environment script.

Connects to the same SQLite database used by the API and exposes all
SQLAlchemy models (via Base.metadata) so that --autogenerate works correctly.

Usage (from the project root C:\\geo_tool\\):
    alembic upgrade head
    alembic revision --autogenerate -m "describe the change"
    alembic downgrade -1
    alembic stamp head   # mark existing DB as current without running migrations
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Make the project root importable so that `api.models.database` can be found
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env so DATABASE_PATH uses any env overrides
from dotenv import load_dotenv  # noqa: E402
load_dotenv(PROJECT_ROOT / ".env")

# Import Base (this also registers all models on the metadata object)
from api.models.database import Base, DATABASE_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# Alembic config object — gives access to values in alembic.ini
# ---------------------------------------------------------------------------
alembic_cfg = context.config

# Override the URL at runtime so it always points to the real DB file,
# regardless of what is in alembic.ini.
alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{DATABASE_PATH}")

# Set up logging from alembic.ini (only when a config file was provided)
if alembic_cfg.config_file_name is not None:
    fileConfig(alembic_cfg.config_file_name)

# This is the metadata Alembic compares against the live DB for --autogenerate
target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline mode — generate SQL without a live connection (for review / CI)
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    url = alembic_cfg.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER TABLE, so batch mode is required
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online mode — run against a live connection
# ---------------------------------------------------------------------------
def run_migrations_online() -> None:
    connectable = engine_from_config(
        alembic_cfg.get_section(alembic_cfg.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # render_as_batch=True is REQUIRED for SQLite:
            # SQLite does not support DROP/ADD COLUMN natively; Alembic
            # handles schema changes by re-creating the table in a temp copy.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
