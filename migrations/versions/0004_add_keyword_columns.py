"""Add keyword columns — keyword_sessions.source and keyword_results clustering fields.

Adds columns introduced in commit c00b217 (feat: keyword research CSV import with
LLM clustering and intent analysis) that were not covered by prior migrations.

  keyword_sessions.source:
      VARCHAR(20), NOT NULL, server_default='dataforseo'.
      Distinguishes sessions seeded from the DataForSEO API vs. CSV import.
      Lifecycle values: "dataforseo" | "import"

  keyword_results.intent:
      VARCHAR(30), nullable.
      LLM-classified search intent: informational | commercial | transactional | navigational

  keyword_results.cluster:
      VARCHAR(200), nullable.
      Topic cluster label assigned by LLM clustering step.

  keyword_results.priority_score:
      FLOAT, nullable.
      Composite priority score (1–10) calculated after clustering.

Migration notes:
  - SQLite requires batch mode (render_as_batch=True) for ALTER TABLE;
    env.py already configures this.
  - keyword_sessions.source uses server_default='dataforseo' so existing rows
    get a sensible value without a backfill UPDATE.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # keyword_sessions.source
    with op.batch_alter_table("keyword_sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source",
                sa.String(length=20),
                nullable=False,
                server_default="dataforseo",
            )
        )

    # keyword_results clustering / intent columns
    with op.batch_alter_table("keyword_results", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("intent", sa.String(length=30), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cluster", sa.String(length=200), nullable=True)
        )
        batch_op.add_column(
            sa.Column("priority_score", sa.Float(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("keyword_results", schema=None) as batch_op:
        batch_op.drop_column("priority_score")
        batch_op.drop_column("cluster")
        batch_op.drop_column("intent")

    with op.batch_alter_table("keyword_sessions", schema=None) as batch_op:
        batch_op.drop_column("source")
