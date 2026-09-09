"""Add crawl_redirects for internal redirect chains.

Closes a gap stated in the Etapa 5 commits: the crawl already asked Screaming
Frog for the Redirect Chains report, but nothing ever parsed it.

Separate from crawl_links because a chain is not an edge -- it carries a hop
count, a final destination and a loop flag that an edge has nowhere to put.

Kept small by the same discipline as the rest of the crawl tables. Measured on
a real report: 1,836 rows covering only 7 distinct addresses, none of them
internal, because four external CDN assets (tailwind, unpkg) repeated once per
page that loaded them. Only internal addresses are stored, deduplicated by
address, since the same redirect linked from ten pages is one thing to fix.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0016"
down_revision: Union[str, None]                = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crawl_redirects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("crawl_id", sa.String(36),
                  sa.ForeignKey("site_crawls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=True),
        sa.Column("address", sa.String(2048), nullable=False),
        sa.Column("final_url", sa.String(2048), nullable=True),
        sa.Column("final_status_code", sa.Integer(), nullable=True),
        sa.Column("hops", sa.Integer(), server_default="0"),
        sa.Column("is_loop", sa.Boolean(), server_default=sa.text("0")),
        sa.Column("has_temp_redirect", sa.Boolean(), server_default=sa.text("0")),
        sa.Column("anchor", sa.Text(), nullable=True),
        sa.Column("link_position", sa.String(50), nullable=True),
    )
    op.create_index("ix_crawl_redirects_crawl_id", "crawl_redirects", ["crawl_id"])
    op.create_index("ix_crawl_redirects_address", "crawl_redirects", ["address"])


def downgrade() -> None:
    op.drop_index("ix_crawl_redirects_address", table_name="crawl_redirects")
    op.drop_index("ix_crawl_redirects_crawl_id", table_name="crawl_redirects")
    op.drop_table("crawl_redirects")
