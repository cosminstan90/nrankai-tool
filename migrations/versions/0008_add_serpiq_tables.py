"""Add SerpIQ tables — SERP Snapshot & Instant Analysis engine.

Introduces two tables:

  serpiq_snapshots   — one row per analysis (URL or keyword input).
                       Stores the SERP results, verdict, optional LLM brief,
                       and on-page metrics when the input is a URL.

  serpiq_serp_items  — one row per ranked result (positions 1-20) for a snapshot.
                       Indexed on (snapshot_id, position) for fast ordered reads.

Migration notes:
  - No FKs across modules; serpiq_snapshots.user_id is intentionally nullable
    (anonymous analyses are allowed).
  - Boolean columns stored as INTEGER (0/1) in SQLite.
  - JSON columns stored as TEXT in SQLite; SQLAlchemy serialises automatically.
  - Purely additive — no existing tables are modified.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0008"
down_revision: Union[str, None]                = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # serpiq_snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "serpiq_snapshots",
        sa.Column("id",                 sa.Integer(),    nullable=False),
        sa.Column("user_id",            sa.Integer(),    nullable=True),
        sa.Column("input_type",         sa.String(10),   nullable=False),
        sa.Column("input_value",        sa.String(2048), nullable=False),
        sa.Column("keyword",            sa.String(500),  nullable=False),
        sa.Column("location_code",      sa.Integer(),    server_default="2040"),
        sa.Column("language_code",      sa.String(10),   server_default="ro"),
        sa.Column("serp_results",       sa.JSON(),       nullable=True),
        sa.Column("url_analysis",       sa.JSON(),       nullable=True),
        sa.Column("verdict",            sa.String(20),   nullable=True),
        sa.Column("verdict_evidence",   sa.JSON(),       nullable=True),
        sa.Column("llm_brief",          sa.Text(),       nullable=True),
        sa.Column("position_found",     sa.Integer(),    nullable=True),
        sa.Column("processing_time_ms", sa.Integer(),    nullable=True),
        sa.Column("created_at",         sa.DateTime(),   nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_serpiq_snapshots_keyword",    "serpiq_snapshots", ["keyword"])
    op.create_index("ix_serpiq_snapshots_verdict",    "serpiq_snapshots", ["verdict"])
    op.create_index("ix_serpiq_snapshots_user_id",    "serpiq_snapshots", ["user_id"])
    op.create_index("ix_serpiq_snapshots_created_at", "serpiq_snapshots", ["created_at"])

    # ------------------------------------------------------------------
    # serpiq_serp_items
    # ------------------------------------------------------------------
    op.create_table(
        "serpiq_serp_items",
        sa.Column("id",                    sa.Integer(),    nullable=False),
        sa.Column("snapshot_id",           sa.Integer(),    nullable=False),
        sa.Column("position",              sa.Integer(),    nullable=False),
        sa.Column("url",                   sa.String(2048), nullable=True),
        sa.Column("domain",                sa.String(512),  nullable=True),
        sa.Column("title",                 sa.String(512),  nullable=True),
        sa.Column("description",           sa.Text(),       nullable=True),
        sa.Column("word_count_estimated",  sa.Integer(),    nullable=True),
        sa.Column("has_schema",            sa.Boolean(),    server_default="0"),
        sa.Column("schema_types",          sa.JSON(),       nullable=True),
        sa.Column("is_featured_snippet",   sa.Boolean(),    server_default="0"),
        sa.Column("is_local_pack",         sa.Boolean(),    server_default="0"),
        sa.Column("is_video",              sa.Boolean(),    server_default="0"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["serpiq_snapshots.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_serpiq_serp_items_snapshot_id",       "serpiq_serp_items", ["snapshot_id"]
    )
    op.create_index(
        "ix_serpiq_serp_items_snapshot_position", "serpiq_serp_items", ["snapshot_id", "position"]
    )
    op.create_index(
        "ix_serpiq_serp_items_domain",            "serpiq_serp_items", ["domain"]
    )


def downgrade() -> None:
    op.drop_index("ix_serpiq_serp_items_domain",            table_name="serpiq_serp_items")
    op.drop_index("ix_serpiq_serp_items_snapshot_position", table_name="serpiq_serp_items")
    op.drop_index("ix_serpiq_serp_items_snapshot_id",       table_name="serpiq_serp_items")
    op.drop_table("serpiq_serp_items")

    op.drop_index("ix_serpiq_snapshots_created_at", table_name="serpiq_snapshots")
    op.drop_index("ix_serpiq_snapshots_user_id",    table_name="serpiq_snapshots")
    op.drop_index("ix_serpiq_snapshots_verdict",    table_name="serpiq_snapshots")
    op.drop_index("ix_serpiq_snapshots_keyword",    table_name="serpiq_snapshots")
    op.drop_table("serpiq_snapshots")
