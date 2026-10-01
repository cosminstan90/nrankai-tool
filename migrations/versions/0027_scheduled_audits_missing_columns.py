"""Add the scheduled_audits columns the schedules router always assumed.

api/routes/schedules.py reads and writes language, use_perplexity,
concurrency, summary_provider, summary_model, run_count, last_audit_id and
updated_at on ScheduledAudit, but none of them ever existed -- not in the
model, not in the table, not even before the database.py split (checked in
git history back to the initial commit). Creating a schedule therefore
raised "'language' is an invalid keyword argument for ScheduledAudit" and no
schedule could ever have been created or run. The table is empty on the real
database, so the server defaults only matter for correctness, not backfill.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0027"
down_revision: Union[str, None]                = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("scheduled_audits") as batch:
        batch.add_column(sa.Column("language", sa.String(50), nullable=False, server_default="English"))
        batch.add_column(sa.Column("use_perplexity", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("concurrency", sa.Integer(), nullable=False, server_default="5"))
        batch.add_column(sa.Column("summary_provider", sa.String(20), nullable=True))
        batch.add_column(sa.Column("summary_model", sa.String(100), nullable=True))
        batch.add_column(sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("last_audit_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scheduled_audits") as batch:
        for col in ("updated_at", "last_audit_id", "run_count", "summary_model",
                    "summary_provider", "concurrency", "use_perplexity", "language"):
            batch.drop_column(col)
