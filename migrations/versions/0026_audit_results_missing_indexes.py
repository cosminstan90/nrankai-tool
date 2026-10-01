"""Create the audit_results indexes the model already declares.

AuditResult.page_url and .classification are declared index=True in
api/models/audit.py ("used in WHERE/JOIN filters" / "used in GROUP BY /
ORDER BY"), but no migration ever created them -- `alembic check` reported
both as missing on the real database, so every lookup on those columns was
a full scan of audit_results (~6k rows and growing).

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-01
"""

from typing import Sequence, Union

from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0026"
down_revision: Union[str, None]                = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # if_not_exists: a database created fresh by init_db()/create_all() already
    # has them; only databases that grew through migrations are missing them.
    op.create_index("ix_audit_results_page_url", "audit_results", ["page_url"], if_not_exists=True)
    op.create_index("ix_audit_results_classification", "audit_results", ["classification"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_audit_results_classification", table_name="audit_results", if_exists=True)
    op.drop_index("ix_audit_results_page_url", table_name="audit_results", if_exists=True)
