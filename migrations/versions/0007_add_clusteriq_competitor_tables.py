"""Add ClusterIQ competitor analysis tables.

Introduces two tables that back the competitor gap-analysis feature:

  clusteriq_competitor_cache  — cached Ahrefs organic-keyword rows per competitor
                                domain; TTL-refreshed every 24 h by the service
  clusteriq_competitor_jobs   — async job-tracking for the analysis pipeline,
                                one row per (project_id, competitor_domain) run

Migration notes:
  - Both tables carry a project_id FK → clusteriq_projects with CASCADE delete.
  - Boolean columns are INTEGER (0/1) in SQLite as per existing convention.
  - No changes to existing tables; purely additive.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:           str               = "0007"
down_revision:      Union[str, None]  = "0006"
branch_labels:      Union[str, Sequence[str], None] = None
depends_on:         Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── clusteriq_competitor_cache ─────────────────────────────────────────
    op.create_table(
        "clusteriq_competitor_cache",
        sa.Column("id",                sa.Integer(),     nullable=False),
        sa.Column("project_id",        sa.Integer(),     nullable=False),
        sa.Column("competitor_domain", sa.String(512),   nullable=False),
        sa.Column("keyword",           sa.String(500),   nullable=False),
        sa.Column("competitor_url",    sa.String(2048),  nullable=True),
        sa.Column("position",          sa.Float(),       nullable=True),
        sa.Column("volume",            sa.Integer(),     nullable=True),
        sa.Column("fetched_at",        sa.DateTime(),    nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["clusteriq_projects.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "competitor_domain", "keyword",
            name="uq_clu_comp_cache_project_domain_keyword",
        ),
    )
    op.create_index(
        "ix_clu_comp_cache_project_domain",
        "clusteriq_competitor_cache",
        ["project_id", "competitor_domain"],
    )

    # ── clusteriq_competitor_jobs ──────────────────────────────────────────
    op.create_table(
        "clusteriq_competitor_jobs",
        sa.Column("id",                     sa.Integer(),    nullable=False),
        sa.Column("project_id",             sa.Integer(),    nullable=False),
        sa.Column("competitor_domain",      sa.String(512),  nullable=False),
        sa.Column("status",                 sa.String(20),   nullable=False),
        sa.Column("keywords_fetched",       sa.Integer(),    nullable=True),
        sa.Column("gap_keywords_found",     sa.Integer(),    nullable=True),
        sa.Column("create_decisions_added", sa.Integer(),    nullable=True),
        sa.Column("cost_estimate_credits",  sa.Float(),      nullable=True),
        sa.Column("error_message",          sa.Text(),       nullable=True),
        sa.Column("started_at",             sa.DateTime(),   nullable=True),
        sa.Column("completed_at",           sa.DateTime(),   nullable=True),
        sa.Column("created_at",             sa.DateTime(),   nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["clusteriq_projects.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_clu_comp_jobs_project_domain",
        "clusteriq_competitor_jobs",
        ["project_id", "competitor_domain"],
    )


def downgrade() -> None:
    op.drop_index("ix_clu_comp_jobs_project_domain",   table_name="clusteriq_competitor_jobs")
    op.drop_table("clusteriq_competitor_jobs")
    op.drop_index("ix_clu_comp_cache_project_domain",  table_name="clusteriq_competitor_cache")
    op.drop_table("clusteriq_competitor_cache")
