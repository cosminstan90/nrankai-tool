"""Add citation_trackers.samples_per_query.

Pasul 9 of docs/superpowers/plans/2026-09-30-next-steps.md: a visibility scan
asked each provider a tracking query exactly once, so 1/5 vs 2/5 citations
was mostly noise (LLM answers vary between runs). This lets a tracker opt
into N samples per query, reported with a Wilson confidence interval instead
of an over-precise single percentage.

NULL means 1 (today's behaviour, unchanged cost) -- a dedicated column, not a
providers_config key, because that field is a plain {provider: bool} map
read as `if enabled`; a sibling int key there would be silently treated as a
truthy "provider" to query.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0023"
down_revision: Union[str, None]                = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("citation_trackers") as batch:
        batch.add_column(sa.Column("samples_per_query", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("citation_trackers") as batch:
        batch.drop_column("samples_per_query")
