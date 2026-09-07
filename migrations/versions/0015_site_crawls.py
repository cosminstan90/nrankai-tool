"""Add site_crawls, crawl_pages and crawl_links for the internal link graph.

Etapa 5 of docs/IMPROVEMENTS_PLAN.md: core/web_scraper.py starts from the
sitemap and never follows a link, so orphan pages, internal 404s, redirect
chains and crawl depth were invisible, and prompts/internal_linking.yaml had
to guess a page's inbound links from that page's own HTML.

crawl_links stores a FILTERED graph, not the whole thing. Measured on a real
crawl: 174,244 hyperlink edges, of which 152,277 (87%) were navigation and 157
(0.09%) were body content. Persisting all of them would store one copy of the
site's nav menu per page -- 55MB of CSV for a ~100-route app. Only content,
broken and redirecting edges get rows; navigation volume is kept as the
crawl_pages.nav_inlinks counter instead.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0015"
down_revision: Union[str, None]                = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "site_crawls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("website", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("pages_crawled", sa.Integer(), server_default="0"),
        sa.Column("content_edges", sa.Integer(), server_default="0"),
        sa.Column("nav_edges_discarded", sa.Integer(), server_default="0"),
        sa.Column("sf_version", sa.String(20), nullable=True),
        sa.Column("config_name", sa.String(255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_site_crawls_website", "site_crawls", ["website"])

    op.create_table(
        "crawl_pages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("crawl_id", sa.String(36),
                  sa.ForeignKey("site_crawls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("indexability", sa.String(50), nullable=True),
        sa.Column("crawl_depth", sa.Integer(), nullable=True),
        sa.Column("inlinks_total", sa.Integer(), server_default="0"),
        sa.Column("unique_inlinks", sa.Integer(), server_default="0"),
        sa.Column("outlinks_total", sa.Integer(), server_default="0"),
        sa.Column("unique_outlinks", sa.Integer(), server_default="0"),
        sa.Column("content_inlinks", sa.Integer(), server_default="0"),
        sa.Column("nav_inlinks", sa.Integer(), server_default="0"),
        sa.Column("is_orphan", sa.Boolean(), server_default=sa.text("0")),
    )
    op.create_index("ix_crawl_pages_crawl_id", "crawl_pages", ["crawl_id"])
    op.create_index("ix_crawl_pages_url", "crawl_pages", ["url"])

    op.create_table(
        "crawl_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("crawl_id", sa.String(36),
                  sa.ForeignKey("site_crawls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=False),
        sa.Column("dest_url", sa.String(2048), nullable=False),
        sa.Column("anchor", sa.Text(), nullable=True),
        sa.Column("link_position", sa.String(50), nullable=True),
        sa.Column("follow", sa.Boolean(), server_default=sa.text("1")),
        sa.Column("dest_status_code", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(20), nullable=False),
    )
    op.create_index("ix_crawl_links_crawl_id", "crawl_links", ["crawl_id"])
    op.create_index("ix_crawl_links_dest_url", "crawl_links", ["dest_url"])


def downgrade() -> None:
    op.drop_index("ix_crawl_links_dest_url", table_name="crawl_links")
    op.drop_index("ix_crawl_links_crawl_id", table_name="crawl_links")
    op.drop_table("crawl_links")
    op.drop_index("ix_crawl_pages_url", table_name="crawl_pages")
    op.drop_index("ix_crawl_pages_crawl_id", table_name="crawl_pages")
    op.drop_table("crawl_pages")
    op.drop_index("ix_site_crawls_website", table_name="site_crawls")
    op.drop_table("site_crawls")
