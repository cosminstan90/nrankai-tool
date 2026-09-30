"""Add gsc_properties.history_synced_through.

Pasul 2 of docs/superpowers/plans/2026-09-30-next-steps.md: a daily worker
backfills gsc_page_history / gsc_query_history for the trailing 16 months.

GSC's Search Analytics API returns rows only for (key, day) pairs that had
impressions -- a day with genuinely zero traffic returns no rows at all. That
makes "which days are missing" impossible to answer by looking at what's
already stored: an unfetched day and a fetched-but-silent day look identical.

This column tracks fetch coverage explicitly instead: the last day the worker
successfully archived. NULL means "never archived" -- start from 16 months
back. A day advances this cursor only after its chunk's rows are committed to
gsc_page_history/gsc_query_history, so a crash mid-backfill re-fetches that
chunk rather than silently skipping it.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0020"
down_revision: Union[str, None]                = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("gsc_properties") as batch:
        batch.add_column(sa.Column("history_synced_through", sa.String(10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("gsc_properties") as batch:
        batch.drop_column("history_synced_through")
