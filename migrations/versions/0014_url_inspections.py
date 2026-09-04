"""Add url_inspections and url_inspection_quota_log for GSC URL Inspection.

Etapa 3 of docs/IMPROVEMENTS_PLAN.md: only searchanalytics and site listing
were ever called against the Search Console API. URL Inspection -- whether a
page is actually indexed, what canonical Google chose vs what the page
declares, mobile usability, rich results eligibility -- was never touched,
so those questions had no answer at all.

url_inspections is dated/additive for the same reason as gsc_page_history
(see that model's docstring): indexation state changes over time, and a
"latest state only" row would erase the fact that a page used to be indexed.
Unlike GSC performance data there is no CSV alternative -- URL Inspection is
API-only -- so there's no source column here.

url_inspection_quota_log is separate and append-only, one row per real API
call made, specifically so quota usage can't be undercounted: Google enforces
roughly 2000 inspections/day/property, and a forced re-check of an
already-cached URL still spends a unit even though it upserts the same
url_inspections row rather than adding a new one.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0014"
down_revision: Union[str, None]                = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "url_inspections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("property_id", sa.String(length=36), nullable=False),
        sa.Column("page_url", sa.String(length=2000), nullable=False),
        sa.Column("checked_date", sa.String(length=10), nullable=False),
        sa.Column("verdict", sa.String(length=20), nullable=True),
        sa.Column("coverage_state", sa.String(length=200), nullable=True),
        sa.Column("robots_txt_state", sa.String(length=30), nullable=True),
        sa.Column("indexing_state", sa.String(length=40), nullable=True),
        sa.Column("page_fetch_state", sa.String(length=30), nullable=True),
        sa.Column("google_canonical", sa.String(length=2000), nullable=True),
        sa.Column("user_canonical", sa.String(length=2000), nullable=True),
        sa.Column("sitemaps_json", sa.Text(), nullable=True),
        sa.Column("last_crawl_time", sa.DateTime(), nullable=True),
        sa.Column("mobile_usability_verdict", sa.String(length=20), nullable=True),
        sa.Column("rich_results_verdict", sa.String(length=20), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["property_id"], ["gsc_properties.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("property_id", "page_url", "checked_date",
                             name="uq_url_inspection_period"),
    )
    with op.batch_alter_table("url_inspections", schema=None) as batch_op:
        batch_op.create_index("ix_url_inspection_prop_page", ["property_id", "page_url"])

    op.create_table(
        "url_inspection_quota_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("property_id", sa.String(length=36), nullable=False),
        sa.Column("checked_date", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["property_id"], ["gsc_properties.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("url_inspection_quota_log", schema=None) as batch_op:
        batch_op.create_index("ix_url_inspection_quota_prop_date", ["property_id", "checked_date"])


def downgrade() -> None:
    with op.batch_alter_table("url_inspection_quota_log", schema=None) as batch_op:
        batch_op.drop_index("ix_url_inspection_quota_prop_date")
    op.drop_table("url_inspection_quota_log")

    with op.batch_alter_table("url_inspections", schema=None) as batch_op:
        batch_op.drop_index("ix_url_inspection_prop_page")
    op.drop_table("url_inspections")
