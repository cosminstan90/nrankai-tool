"""Initial schema — create all tables.

This migration establishes the complete database schema for GEO Analyzer v2.1.
It is safe to run against an **existing** database: every `create_table` call
uses `checkfirst=True`, so tables that already exist are silently skipped.

Workflow for developers adding Alembic to an already-running instance:
    1.  Install alembic:  pip install alembic
    2.  From the project root, stamp the current DB as already up-to-date:
            alembic stamp head
        This marks revision 0001 as applied without actually running upgrade().
    3.  Future schema changes:
            alembic revision --autogenerate -m "add column foo"
            alembic upgrade head

Revision ID: 0001
Revises:
Create Date: 2026-02-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# revision identifiers
# ---------------------------------------------------------------------------
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# upgrade — create all tables (checkfirst=True → skip if already exists)
# ---------------------------------------------------------------------------
def upgrade() -> None:
    """Create all application tables.

    Uses Base.metadata.create_all(checkfirst=True) so the migration is
    idempotent — safe to run on both a fresh SQLite file and an existing DB.
    """
    import sys
    from pathlib import Path

    # Make sure the project root is importable regardless of working directory
    project_root = Path(__file__).parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from api.models.database import Base  # registers all ORM models

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


# ---------------------------------------------------------------------------
# downgrade — drop all tables (use with care — DATA LOSS)
# ---------------------------------------------------------------------------
def downgrade() -> None:
    """Drop all application tables.

    WARNING: This destroys all data in the database.
    Only intended for local development / CI tear-downs.
    """
    import sys
    from pathlib import Path

    project_root = Path(__file__).parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from api.models.database import Base

    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
