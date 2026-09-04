"""Add gsc_page_history and gsc_query_history for time-series GSC data.

Etapa 1 of docs/IMPROVEMENTS_PLAN.md: gsc_page_rows and gsc_query_rows carry
no date at all, and every sync (CSV upload or OAuth API sync) deletes the
existing rows before inserting the new ones -- one flat snapshot per
property, overwritten on every import. The Search Console API only serves
the trailing 16 months, so anything not captured before that window rolls
past is gone for good.

These two tables are additive, not a column added to the existing ones.
8 call sites across the app (ads.py, ga4.py, meta_generator.py, guide.py,
llms_txt.py, insights.py, integration_views.py, pages/_shared.py) query
GscPageRow/GscQueryRow filtered by property_id alone -- several running
func.avg/func.sum or order_by(clicks.desc()) -- all assuming exactly one row
per page/query. Turning those tables themselves into a time series would
silently corrupt every one of those aggregates. GscPageRow/GscQueryRow keep
their current "latest snapshot, replaced on sync" behavior unchanged; the
history tables accumulate alongside them.

period_start/period_end are both set for a CSV upload (one row = the whole
date range the user's GSC export covers) and equal to each other for a single
API sync day (dimensions=[dimension, "date"]). source distinguishes csv/api
provenance, and is part of the uniqueness key together with property_id +
key + period, so re-syncing the same day updates that row instead of
duplicating it, and a CSV upload covering a period never silently overwrites
numbers an API sync already recorded for the same period (or the reverse).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0012"
down_revision: Union[str, None]                = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gsc_page_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("property_id", sa.String(length=36), nullable=False),
        sa.Column("page", sa.String(length=2000), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("ctr", sa.Float(), nullable=True),
        sa.Column("position", sa.Float(), nullable=True),
        sa.Column("period_start", sa.String(length=10), nullable=False),
        sa.Column("period_end", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["property_id"], ["gsc_properties.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("property_id", "page", "period_start", "period_end", "source",
                             name="uq_gsc_page_history_period"),
    )
    with op.batch_alter_table("gsc_page_history", schema=None) as batch_op:
        batch_op.create_index("ix_gsc_page_history_prop_period", ["property_id", "period_start"])
        batch_op.create_index("ix_gsc_page_history_prop_page_period", ["property_id", "page", "period_start"])

    op.create_table(
        "gsc_query_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("property_id", sa.String(length=36), nullable=False),
        sa.Column("query", sa.String(length=1000), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("ctr", sa.Float(), nullable=True),
        sa.Column("position", sa.Float(), nullable=True),
        sa.Column("period_start", sa.String(length=10), nullable=False),
        sa.Column("period_end", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["property_id"], ["gsc_properties.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("property_id", "query", "period_start", "period_end", "source",
                             name="uq_gsc_query_history_period"),
    )
    with op.batch_alter_table("gsc_query_history", schema=None) as batch_op:
        batch_op.create_index("ix_gsc_query_history_prop_period", ["property_id", "period_start"])
        batch_op.create_index("ix_gsc_query_history_prop_query_period", ["property_id", "query", "period_start"])


def downgrade() -> None:
    with op.batch_alter_table("gsc_query_history", schema=None) as batch_op:
        batch_op.drop_index("ix_gsc_query_history_prop_query_period")
        batch_op.drop_index("ix_gsc_query_history_prop_period")
    op.drop_table("gsc_query_history")

    with op.batch_alter_table("gsc_page_history", schema=None) as batch_op:
        batch_op.drop_index("ix_gsc_page_history_prop_page_period")
        batch_op.drop_index("ix_gsc_page_history_prop_period")
    op.drop_table("gsc_page_history")
