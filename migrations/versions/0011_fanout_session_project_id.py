"""Add a dedicated project_id column to fanout_sessions.

Etapa 4.2 of the consolidation (docs/CONSOLIDATION_PLAN.md): api/routes/
projects.py linked FanoutSession rows to a FanoutProject by storing the
project's UUID in `audit_id` -- a column declared as a real foreign key to
the unrelated `audits` table (ForeignKey("audits.id", ondelete="SET NULL")).
The original code even flagged this in its own comment ("we store it in
engine field hack or audit_id").

This only worked in practice because PRAGMA foreign_keys=ON is applied to
sync_engine only (api/models/database.py's event listener), never to the
async engine every actual request goes through -- so the FK constraint was
never really enforced, and the misuse never raised an error. Confirmed live
during Etapa 4.2 testing: creating a project, then quick-analyzing it (which
writes FanoutProject.id into FanoutSession.audit_id), succeeded with zero
FanoutSession rows referencing a real audits.id at the time -- had FK
enforcement been active on the async engine, this insert would have failed
outright for any project ID that didn't happen to already exist as an
audits.id.

No data migration needed: at the time of writing, zero fanout_sessions rows
have a non-null audit_id in production (Fan-Out only started producing real
sessions once Etapa 1's schema fix unblocked it, and no project-linked
session had been created before this fix).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0011"
down_revision: Union[str, None]                = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("fanout_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
        batch_op.create_index("ix_fanout_sessions_project_id", ["project_id"])


def downgrade() -> None:
    with op.batch_alter_table("fanout_sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_fanout_sessions_project_id")
        batch_op.drop_column("project_id")
