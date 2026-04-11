"""Add fanout_sessions, fanout_queries, fanout_sources tables.

Introduces the WLA Fan-Out Analyzer storage layer.

  fanout_sessions  — one row per prompt analysis (provider, model, stats,
                     optional target URL coverage data)
  fanout_queries   — individual predicted/actual search queries per session
  fanout_sources   — cited source URLs extracted from AI responses per session

Migration notes:
  - SQLite requires batch mode for ALTER TABLE; env.py already sets
    render_as_batch=True so new tables are created normally via op.create_table.
  - Boolean columns are stored as INTEGER (0/1) in SQLite.
  - FKs reference audits.id with SET NULL on delete (session is independent).

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # fanout_sessions
    # ------------------------------------------------------------------
    op.create_table(
        "fanout_sessions",
        sa.Column("id",                   sa.String(36),  primary_key=True),
        sa.Column("prompt",               sa.Text(),      nullable=False),
        sa.Column("provider",             sa.String(20),  nullable=False),
        sa.Column("model",                sa.String(100), nullable=False),
        sa.Column("user_location",        sa.String(200), nullable=True),
        sa.Column("total_fanout_queries", sa.Integer(),   server_default="0"),
        sa.Column("total_sources",        sa.Integer(),   server_default="0"),
        sa.Column("total_search_calls",   sa.Integer(),   server_default="0"),
        sa.Column("target_url",           sa.String(500), nullable=True),
        sa.Column("target_found",         sa.Boolean(),   server_default="0"),
        sa.Column("target_position",      sa.Integer(),   nullable=True),
        sa.Column("audit_id",             sa.String(36),  sa.ForeignKey("audits.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at",           sa.DateTime(),  server_default=sa.func.now()),
    )
    op.create_index("ix_fanout_sessions_target_url", "fanout_sessions", ["target_url"])
    op.create_index("ix_fanout_sessions_created_at", "fanout_sessions", ["created_at"])
    op.create_index("ix_fanout_sessions_audit_id",   "fanout_sessions", ["audit_id"])

    # ------------------------------------------------------------------
    # fanout_queries
    # ------------------------------------------------------------------
    op.create_table(
        "fanout_queries",
        sa.Column("id",             sa.Integer(),   primary_key=True, autoincrement=True),
        sa.Column("session_id",     sa.String(36),  sa.ForeignKey("fanout_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("query_text",     sa.Text(),      nullable=False),
        sa.Column("query_position", sa.Integer(),   nullable=True),
    )
    op.create_index("ix_fanout_queries_session_id", "fanout_queries", ["session_id"])

    # ------------------------------------------------------------------
    # fanout_sources
    # ------------------------------------------------------------------
    op.create_table(
        "fanout_sources",
        sa.Column("id",              sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column("session_id",      sa.String(36),   sa.ForeignKey("fanout_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url",             sa.String(2000), nullable=False),
        sa.Column("title",           sa.String(500),  nullable=True),
        sa.Column("domain",          sa.String(500),  nullable=True),
        sa.Column("is_target",       sa.Boolean(),    server_default="0"),
        sa.Column("source_position", sa.Integer(),    nullable=True),
    )
    op.create_index("ix_fanout_sources_session_id", "fanout_sources", ["session_id"])
    op.create_index("ix_fanout_sources_domain",     "fanout_sources", ["domain"])


def downgrade() -> None:
    op.drop_table("fanout_sources")
    op.drop_table("fanout_queries")
    op.drop_table("fanout_sessions")
