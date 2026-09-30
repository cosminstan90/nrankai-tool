"""Add worker_runs.

Pasul 7 of docs/superpowers/plans/2026-09-30-next-steps.md: a health panel
(GET /api/status) needs "when did each background worker last succeed",
surviving a server restart. Almost every real failure so far (Claude failing
every scan for months, Perplexity's key rejected, GSC OAuth silently
disconnected, the sync route broken for weeks) failed silently -- nothing
recorded that a worker had stopped doing its job.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0021"
down_revision: Union[str, None]                = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "worker_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("worker", sa.String(50), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_worker_runs_worker_finished_at", "worker_runs", ["worker", "finished_at"])


def downgrade() -> None:
    op.drop_index("ix_worker_runs_worker_finished_at", table_name="worker_runs")
    op.drop_table("worker_runs")
