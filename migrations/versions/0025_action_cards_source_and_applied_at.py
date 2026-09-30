"""Make action_cards.audit_id nullable; add source/applied_at/metric_baseline.

Pasul 18 of docs/superpowers/plans/2026-09-30-next-steps.md: without this,
pasii 12-17 produce recommendations but there is nowhere durable to record
"this specific action was proposed for this URL, and here is what the metric
looked like at that moment" -- action_cards already exists and is UI-wired,
but it requires an audit_id (NOT NULL) and only has updated_at, which
changes on any edit, not specifically on "the user applied this."

New columns:
  - source: which recommendation engine produced this card --
    'audit' (existing/legacy rows and the normal per-audit flow),
    'js_visibility', 'gsc_opportunity', 'internal_link', 'decay',
    'citation_gap', 'fanout_gap' (pasii 12-17).
  - applied_at: set exactly once, when the user marks the action as applied
    (distinct from updated_at, which also changes on e.g. a priority edit).
  - metric_baseline: JSON snapshot of the relevant metric(s) at the moment
    the action was created/applied, so a later report can compare against
    it without re-deriving "what did this look like before" from history
    tables that may have since rolled off their retention window.

SQLite can't ALTER COLUMN to relax NOT NULL directly, hence batch_alter_table
(Alembic recreates the table under the hood). Existing action_cards rows are
untouched (audit_id keeps its value) -- 'audit_id nullable' only changes what
NEW rows are allowed to omit; source defaults to 'audit' for existing rows via
server_default so nothing already saved reads as source=None.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0025"
down_revision: Union[str, None]                = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("action_cards") as batch:
        batch.alter_column("audit_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("source", sa.String(30), nullable=False, server_default="audit"))
        batch.add_column(sa.Column("applied_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("metric_baseline", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("action_cards") as batch:
        batch.drop_column("metric_baseline")
        batch.drop_column("applied_at")
        batch.drop_column("source")
        batch.alter_column("audit_id", existing_type=sa.String(36), nullable=False)
