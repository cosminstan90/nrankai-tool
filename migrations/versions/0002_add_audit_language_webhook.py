"""Add language and webhook_url columns to audits table.

These columns were added in v2.1 (Phase 5):
  - language:    stores the output language requested for this audit (e.g. "English", "Romanian").
                 Previously only passed through the pipeline but never persisted; now stored so
                 retries and display are correct.
  - webhook_url: optional URL to POST a notification to on audit completion or failure.

Both columns are nullable with no defaults to ensure zero impact on existing rows.

Revision ID: 0002
Revises: 0001
Create Date: 2026-02-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite requires batch mode for ALTER TABLE operations (render_as_batch=True in env.py)
    with op.batch_alter_table("audits", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("language", sa.String(length=30), nullable=True)
        )
        batch_op.add_column(
            sa.Column("webhook_url", sa.String(length=512), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("audits", schema=None) as batch_op:
        batch_op.drop_column("webhook_url")
        batch_op.drop_column("language")
