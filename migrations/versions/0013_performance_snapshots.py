"""Add performance_snapshots for Core Web Vitals (CrUX field + PSI lab data).

Etapa 2 of docs/IMPROVEMENTS_PLAN.md: the tool had zero performance data --
no PageSpeed, no CrUX, no Lighthouse, no LCP/INP/CLS anywhere -- despite both
Google APIs being free and keyless-auth (just an API key, no OAuth). This
closes that gap.

Dated like gsc_page_history/gsc_query_history (see that model's docstring):
CrUX field data moves week to week and CrUX's own History API only serves 25
weeks, so anything not captured as it happens is eventually gone from Google
too. A single row never mixes field (source="crux") and lab (source="psi")
data -- they measure fundamentally different things (real visitors vs one
simulated Lighthouse run) and conflating them into one row would make the
trend meaningless.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0013"
down_revision: Union[str, None]                = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "performance_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("strategy", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("p75_lcp", sa.Float(), nullable=True),
        sa.Column("p75_inp", sa.Float(), nullable=True),
        sa.Column("p75_cls", sa.Float(), nullable=True),
        sa.Column("p75_fcp", sa.Float(), nullable=True),
        sa.Column("p75_ttfb", sa.Float(), nullable=True),
        sa.Column("lcp_rating", sa.String(length=20), nullable=True),
        sa.Column("inp_rating", sa.String(length=20), nullable=True),
        sa.Column("cls_rating", sa.String(length=20), nullable=True),
        sa.Column("fcp_rating", sa.String(length=20), nullable=True),
        sa.Column("ttfb_rating", sa.String(length=20), nullable=True),
        sa.Column("performance_score", sa.Integer(), nullable=True),
        sa.Column("lab_lcp", sa.Float(), nullable=True),
        sa.Column("lab_cls", sa.Float(), nullable=True),
        sa.Column("lab_tbt", sa.Float(), nullable=True),
        sa.Column("lab_fcp", sa.Float(), nullable=True),
        sa.Column("lab_speed_index", sa.Float(), nullable=True),
        sa.Column("period_start", sa.String(length=10), nullable=False),
        sa.Column("period_end", sa.String(length=10), nullable=False),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url", "strategy", "source", "period_start", "period_end",
                             name="uq_performance_snapshot_period"),
    )
    with op.batch_alter_table("performance_snapshots", schema=None) as batch_op:
        batch_op.create_index("ix_performance_snapshot_url_period", ["url", "period_start"])


def downgrade() -> None:
    with op.batch_alter_table("performance_snapshots", schema=None) as batch_op:
        batch_op.drop_index("ix_performance_snapshot_url_period")
    op.drop_table("performance_snapshots")
