"""Add text_embeddings.

Shared embeddings cache -- Pasul 14/17 of
docs/superpowers/plans/2026-09-30-next-steps.md: Pasul 14 (internal link
suggestions) needs page-to-page topical similarity, Pasul 17 (Fan-Out
sub-query coverage) needs query-to-passage similarity. Built once here per
the plan's explicit instruction not to duplicate this infrastructure.

Keyed on (content_hash, model) so the same exact text is never paid for
twice regardless of which feature asked for it first.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0024"
down_revision: Union[str, None]                = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "text_embeddings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("vector", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("content_hash", "model", name="uq_text_embeddings_hash_model"),
    )
    op.create_index("ix_text_embeddings_content_hash", "text_embeddings", ["content_hash"])


def downgrade() -> None:
    op.drop_index("ix_text_embeddings_content_hash", table_name="text_embeddings")
    op.drop_table("text_embeddings")
