"""Add page_snapshots.meta_robots.

Etapa 6 follow-up: the scraper now captures <head> alongside the body
(core/web_scraper.py's HEAD_META_SCRIPT), so title, meta description,
canonical and the robots directive finally reach storage. The first three
already had columns; robots did not.

It earns one because a page switching to noindex is the most damaging change a
client can make and the least visible: the page still loads, still looks right,
and quietly leaves the index. core/page_diff.py reports it as high severity
with an explicit NOINDEX note.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0018"
down_revision: Union[str, None]                = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("page_snapshots", sa.Column("meta_robots", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("page_snapshots", "meta_robots")
