"""Add snapshot_runs and page_snapshots for content change detection.

Etapa 6 of docs/IMPROVEMENTS_PLAN.md: the tool had no memory of page content.
core/scrape_state.py hashes pages, so it knew *that* a page changed, but the
previous HTML is overwritten on the next scrape, so nothing could say *what*.

Dated and additive for the same reason as gsc_page_history and
performance_snapshots -- "what changed since last time" is unanswerable from a
latest-state-only row.

Extracted fields rather than archived HTML: measured at 4.3 KB against 67.7 KB
for the same real page, and re-extracting the same fields from stored markup
later would gain nothing.

title, meta_description and canonical are nullable and will be NULL for every
row until head capture exists: core/web_scraper.py stores document.body only,
and those three appear in 0 of 40 real stored pages. NULL means "not
captured", never "absent" -- core/page_diff.py skips comparisons involving it
so the first capture does not flag every page at once.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0017"
down_revision: Union[str, None]                = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "snapshot_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("website", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("pages_captured", sa.Integer(), server_default="0"),
        sa.Column("source_dir", sa.String(500), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_snapshot_runs_website", "snapshot_runs", ["website"])

    op.create_table(
        "page_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36),
                  sa.ForeignKey("snapshot_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("website", sa.String(255), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("canonical", sa.Text(), nullable=True),
        sa.Column("h1", sa.JSON(), nullable=True),
        sa.Column("h2", sa.JSON(), nullable=True),
        sa.Column("h3", sa.JSON(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column("internal_links", sa.JSON(), nullable=True),
        sa.Column("external_link_count", sa.Integer(), nullable=True),
        sa.Column("images_total", sa.Integer(), nullable=True),
        sa.Column("images_without_alt", sa.Integer(), nullable=True),
        sa.Column("schema_types", sa.JSON(), nullable=True),
        sa.Column("content_hash", sa.String(80), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("run_id", "url", name="uq_page_snapshots_run_url"),
    )
    op.create_index("ix_page_snapshots_run_id", "page_snapshots", ["run_id"])
    op.create_index("ix_page_snapshots_website", "page_snapshots", ["website"])
    op.create_index("ix_page_snapshots_url", "page_snapshots", ["url"])
    op.create_index("ix_page_snapshots_content_hash", "page_snapshots", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_page_snapshots_content_hash", table_name="page_snapshots")
    op.drop_index("ix_page_snapshots_url", table_name="page_snapshots")
    op.drop_index("ix_page_snapshots_website", table_name="page_snapshots")
    op.drop_index("ix_page_snapshots_run_id", table_name="page_snapshots")
    op.drop_table("page_snapshots")
    op.drop_index("ix_snapshot_runs_website", table_name="snapshot_runs")
    op.drop_table("snapshot_runs")
