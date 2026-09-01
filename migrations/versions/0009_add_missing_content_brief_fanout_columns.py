"""Add missing columns to content_briefs and fanout_sessions.

These columns exist in the SQLAlchemy models (api/models/content.py) but
were never propagated to the live database — no Alembic migration and no
ad-hoc ALTER TABLE ever added them. Confirmed via an exhaustive model<->DB
diff across all 80 tables (docs/audit/03-data-layer.md, F3-01): these two
tables are the ONLY ones with drift.

  content_briefs:
    - current_score       (Float, nullable)      — model: api/models/content.py:28
    - executive_summary   (Text, nullable)        — model: api/models/content.py:29

  fanout_sessions ("Prompt 15 enrichment columns", model: content.py:469-477):
    - query_origin        (String(20), default 'actual')
    - source_origin       (String(20), default 'citation')
    - prompt_cluster      (String(50), nullable, indexed)
    - run_cost_usd        (Float, default 0.0)
    - locale              (String(20), default 'en-US')
    - language            (String(10), default 'en')
    - confidence_score    (Float, nullable)
    - engine              (String(50), nullable, indexed)
    - model_version       (String(50), nullable)
    - from_cache          (Boolean, default False)

This is the direct fix for the two 500 errors confirmed live in the audit:
GET /api/briefs and GET /api/fanout/sessions (docs/audit/02-routes.md F2-03/F2-04).

Because the live DB already had every table from migrations 0001-0008
(created via Base.metadata.create_all(), not via `alembic upgrade`),
`alembic_version` was stamped to head ("0008") before writing this
migration — see docs/audit/03-data-layer.md F3-02. This migration is the
first one that actually runs `alembic upgrade` against this database.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0009"
down_revision: Union[str, None]                = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite requires batch mode for ALTER TABLE operations (render_as_batch=True in env.py)
    with op.batch_alter_table("content_briefs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("current_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("executive_summary", sa.Text(), nullable=True))

    with op.batch_alter_table("fanout_sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("query_origin", sa.String(length=20), nullable=True, server_default="actual")
        )
        batch_op.add_column(
            sa.Column("source_origin", sa.String(length=20), nullable=True, server_default="citation")
        )
        batch_op.add_column(
            sa.Column("prompt_cluster", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(
            sa.Column("run_cost_usd", sa.Float(), nullable=True, server_default="0.0")
        )
        batch_op.add_column(
            sa.Column("locale", sa.String(length=20), nullable=True, server_default="en-US")
        )
        batch_op.add_column(
            sa.Column("language", sa.String(length=10), nullable=True, server_default="en")
        )
        batch_op.add_column(
            sa.Column("confidence_score", sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("engine", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(
            sa.Column("model_version", sa.String(length=50), nullable=True)
        )
        batch_op.add_column(
            sa.Column("from_cache", sa.Boolean(), nullable=True, server_default="0")
        )
        batch_op.create_index("ix_fanout_sessions_prompt_cluster", ["prompt_cluster"])
        batch_op.create_index("ix_fanout_sessions_engine", ["engine"])


def downgrade() -> None:
    with op.batch_alter_table("fanout_sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_fanout_sessions_engine")
        batch_op.drop_index("ix_fanout_sessions_prompt_cluster")
        batch_op.drop_column("from_cache")
        batch_op.drop_column("model_version")
        batch_op.drop_column("engine")
        batch_op.drop_column("confidence_score")
        batch_op.drop_column("language")
        batch_op.drop_column("locale")
        batch_op.drop_column("run_cost_usd")
        batch_op.drop_column("prompt_cluster")
        batch_op.drop_column("source_origin")
        batch_op.drop_column("query_origin")

    with op.batch_alter_table("content_briefs", schema=None) as batch_op:
        batch_op.drop_column("executive_summary")
        batch_op.drop_column("current_score")
