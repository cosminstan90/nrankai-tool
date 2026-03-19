"""Add analysis_status and analysis_error columns to benchmark_projects table.

These columns track the lifecycle of the AI-generated competitive analysis
that runs as a background task after a benchmark project is created.

  analysis_status:
      VARCHAR(20), NOT NULL, default 'pending'.
      Lifecycle: "pending" → "generating" → "completed" | "failed"
      Persisted so the frontend can distinguish all four states across
      server restarts and page reloads.

  analysis_error:
      TEXT, nullable.
      Populated with a truncated error message when analysis_status = 'failed',
      so the UI can surface a human-readable reason for the failure.

Migration notes:
  - SQLite requires batch mode (render_as_batch=True) for ALTER TABLE;
    env.py already configures this.
  - analysis_status is added with server_default='pending'.  A follow-up
    UPDATE backfills rows that already have benchmark_summary (i.e. the AI
    analysis ran before this column existed) to 'completed', so they are
    not incorrectly shown as pending in the UI.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add both columns.  batch_alter_table recreates the table so SQLite
    # honours the NOT NULL + server_default constraint correctly.
    with op.batch_alter_table("benchmark_projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "analysis_status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            )
        )
        batch_op.add_column(
            sa.Column("analysis_error", sa.Text(), nullable=True)
        )

    # Backfill: rows that already have a completed AI analysis should be
    # marked 'completed', not left as the default 'pending'.
    op.execute(
        "UPDATE benchmark_projects "
        "SET analysis_status = 'completed' "
        "WHERE benchmark_summary IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("benchmark_projects", schema=None) as batch_op:
        batch_op.drop_column("analysis_error")
        batch_op.drop_column("analysis_status")
