"""Add serp_organic_results and serp_aio_references.

Pasul 8 of docs/superpowers/plans/2026-09-30-next-steps.md: serp_rank_observations
(migration 0019) kept only the tracked site's own rank and a yes/no for
"does the AI Overview cite us", throwing away the rest of an already-paid-for
SERP -- the same pattern the plan criticised at AI Overviews before that was
fixed. Free to add: the data is already in the SerpResult that produced each
observation, just not stored.

ON DELETE CASCADE against serp_rank_observations (not citation_trackers
directly) -- deleting an observation, or the tracker that owns it, takes
every result/reference row with it transitively (foreign_keys=ON is set on
connect for both engines, and SQLite cascades multi-level when it is).

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0022"
down_revision: Union[str, None]                = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "serp_organic_results",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("observation_id", sa.String(36),
                  sa.ForeignKey("serp_rank_observations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rank_group", sa.Integer(), nullable=False),
        sa.Column("rank_absolute", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(500), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
    )
    op.create_index("ix_serp_organic_results_observation_id", "serp_organic_results", ["observation_id"])
    op.create_index("ix_serp_organic_results_domain", "serp_organic_results", ["domain"])

    op.create_table(
        "serp_aio_references",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("observation_id", sa.String(36),
                  sa.ForeignKey("serp_rank_observations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("domain", sa.String(500), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
    )
    op.create_index("ix_serp_aio_references_observation_id", "serp_aio_references", ["observation_id"])
    op.create_index("ix_serp_aio_references_domain", "serp_aio_references", ["domain"])


def downgrade() -> None:
    op.drop_index("ix_serp_aio_references_domain", table_name="serp_aio_references")
    op.drop_index("ix_serp_aio_references_observation_id", table_name="serp_aio_references")
    op.drop_table("serp_aio_references")

    op.drop_index("ix_serp_organic_results_domain", table_name="serp_organic_results")
    op.drop_index("ix_serp_organic_results_observation_id", table_name="serp_organic_results")
    op.drop_table("serp_organic_results")
