"""Add serp_rank_observations and citation_trackers.serp_location.

Etapa 8 of docs/IMPROVEMENTS_PLAN.md: the tool tracked audit SCORES over time
but never Google positions, so "we optimised -- did the ranking move?" had no
answer. The google_aio provider already fetched the full SERP for every
tracked query and kept only the AI Overview; the organic rankings in the same
response were thrown away. This stores them.

Dated and additive, one row per (scan, query). rank_group is the organic
position; rank_absolute counts every block on the page, so an AI Overview
above a result pushes it down -- on a real Romanian SERP the first organic
result was rank_group 1 but rank_absolute 2. NULL rank_group means "not among
the results_count results returned", never position 0.

citation_trackers.serp_location is an optional market override. Without it
the market is derived from the country-code TLD, then the language. The only
real tracker, ing.ro, has language="English" (the default, never changed) and
Romanian queries, so the language alone would have picked the United States.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0019"
down_revision: Union[str, None]                = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("citation_trackers", sa.Column("serp_location", sa.String(8), nullable=True))

    op.create_table(
        "serp_rank_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tracker_id", sa.String(36),
                  sa.ForeignKey("citation_trackers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scan_id", sa.String(36),
                  sa.ForeignKey("citation_scans.id", ondelete="SET NULL"), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("website", sa.String(255), nullable=False),
        sa.Column("location_code", sa.Integer(), nullable=False),
        sa.Column("language_code", sa.String(10), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("results_count", sa.Integer(), nullable=False),
        sa.Column("rank_group", sa.Integer(), nullable=True),
        sa.Column("rank_absolute", sa.Integer(), nullable=True),
        sa.Column("ranking_url", sa.Text(), nullable=True),
        sa.Column("is_featured_snippet", sa.Boolean(), server_default=sa.text("0")),
        sa.Column("aio_present", sa.Boolean(), server_default=sa.text("0")),
        sa.Column("aio_cites_site", sa.Boolean(), server_default=sa.text("0")),
        sa.Column("serp_features", sa.JSON(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_serp_rank_observations_tracker_id", "serp_rank_observations", ["tracker_id"])
    op.create_index("ix_serp_rank_observations_scan_id", "serp_rank_observations", ["scan_id"])
    op.create_index("ix_serp_rank_observations_observed_at", "serp_rank_observations", ["observed_at"])


def downgrade() -> None:
    op.drop_index("ix_serp_rank_observations_observed_at", table_name="serp_rank_observations")
    op.drop_index("ix_serp_rank_observations_scan_id", table_name="serp_rank_observations")
    op.drop_index("ix_serp_rank_observations_tracker_id", table_name="serp_rank_observations")
    op.drop_table("serp_rank_observations")
    with op.batch_alter_table("citation_trackers") as batch:
        batch.drop_column("serp_location")
