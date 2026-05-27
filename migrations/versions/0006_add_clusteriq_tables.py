"""Add ClusterIQ tables — SERP Cluster Intelligence engine.

Introduces the storage layer for ClusterIQ:

  clusteriq_projects    — one analysis run per domain; tracks crawl/cluster status
  clusteriq_urls        — crawled URLs with indexability and on-page metadata
  clusteriq_serp_data   — keyword/URL pairs from GSC or DataForSEO (UNIQUE per project+keyword+url)
  clusteriq_clusters    — Louvain community clusters with aggregated demand metrics
  clusteriq_url_clusters — M2M bridge: URL role and impression share within a cluster
  clusteriq_decisions   — KUCD/LLM verdicts per URL+cluster pair
  clusteriq_duplicates  — duplicate URL pairs with similarity score and dev brief

Migration notes:
  - SQLite requires batch mode for ALTER TABLE; env.py already sets
    render_as_batch=True so new tables are created normally via op.create_table.
  - Boolean columns stored as INTEGER (0/1) in SQLite.
  - JSON columns stored as TEXT in SQLite (SQLAlchemy serialises automatically).
  - clusteriq_projects.project_id is intentionally NOT a FK — no generic
    projects table exists yet; it acts as an optional external reference.
  - CluDecision.url_id uses SET NULL so decisions survive URL pruning.
  - CluDuplicate winner/duplicate FKs also use SET NULL for the same reason.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # clusteriq_projects
    # ------------------------------------------------------------------
    op.create_table(
        "clusteriq_projects",
        sa.Column("id",                      sa.Integer(),     primary_key=True, autoincrement=True),
        sa.Column("project_id",              sa.Integer(),     nullable=True),
        sa.Column("domain",                  sa.String(512),   nullable=False),
        sa.Column("status",                  sa.String(20),    server_default="pending"),
        sa.Column("gsc_property",            sa.String(512),   nullable=True),
        sa.Column("dataforseo_credits_used", sa.Integer(),     server_default="0"),
        sa.Column("created_at",              sa.DateTime(),    server_default=sa.func.now()),
        sa.Column("updated_at",              sa.DateTime(),    server_default=sa.func.now()),
    )
    op.create_index("ix_clusteriq_projects_status",     "clusteriq_projects", ["status"])
    op.create_index("ix_clusteriq_projects_project_id", "clusteriq_projects", ["project_id"])

    # ------------------------------------------------------------------
    # clusteriq_urls
    # ------------------------------------------------------------------
    op.create_table(
        "clusteriq_urls",
        sa.Column("id",            sa.Integer(),      primary_key=True, autoincrement=True),
        sa.Column("project_id",    sa.Integer(),      sa.ForeignKey("clusteriq_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url",           sa.String(2048),   nullable=False),
        sa.Column("status_code",   sa.Integer(),      nullable=True),
        sa.Column("is_indexable",  sa.Boolean(),      server_default="1"),
        sa.Column("canonical_url", sa.String(2048),   nullable=True),
        sa.Column("title",         sa.String(512),    nullable=True),
        sa.Column("word_count",    sa.Integer(),      nullable=True),
        sa.Column("crawled_at",    sa.DateTime(),     nullable=True),
    )
    op.create_index("ix_clusteriq_urls_project_id",  "clusteriq_urls", ["project_id"])
    op.create_index("ix_clusteriq_urls_project_url", "clusteriq_urls", ["project_id", "url"])

    # ------------------------------------------------------------------
    # clusteriq_serp_data
    # ------------------------------------------------------------------
    op.create_table(
        "clusteriq_serp_data",
        sa.Column("id",          sa.Integer(),      primary_key=True, autoincrement=True),
        sa.Column("project_id",  sa.Integer(),      sa.ForeignKey("clusteriq_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("keyword",     sa.String(500),    nullable=False),
        sa.Column("url",         sa.String(2048),   nullable=False),
        sa.Column("position",    sa.Float(),        nullable=True),
        sa.Column("impressions", sa.Integer(),      nullable=True),
        sa.Column("clicks",      sa.Integer(),      nullable=True),
        sa.Column("serp_urls",   sa.JSON(),         nullable=True),
        sa.Column("data_source", sa.String(20),     nullable=False),
        sa.Column("fetched_at",  sa.DateTime(),     server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "keyword", "url", name="uq_clu_serp_project_keyword_url"),
    )
    op.create_index("ix_clusteriq_serp_data_project_id", "clusteriq_serp_data", ["project_id"])
    op.create_index("ix_clusteriq_serp_data_keyword",    "clusteriq_serp_data", ["keyword"])

    # ------------------------------------------------------------------
    # clusteriq_clusters
    # ------------------------------------------------------------------
    op.create_table(
        "clusteriq_clusters",
        sa.Column("id",                      sa.Integer(),      primary_key=True, autoincrement=True),
        sa.Column("project_id",              sa.Integer(),      sa.ForeignKey("clusteriq_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cluster_label",           sa.String(512),    nullable=False),
        sa.Column("primary_url",             sa.String(2048),   nullable=True),
        sa.Column("primary_keyword",         sa.String(500),    nullable=True),
        sa.Column("total_impressions",       sa.Integer(),      server_default="0"),
        sa.Column("total_clicks",            sa.Integer(),      server_default="0"),
        sa.Column("url_count",               sa.Integer(),      server_default="0"),
        sa.Column("search_demand_confirmed", sa.Boolean(),      server_default="0"),
        sa.Column("louvain_community_id",    sa.Integer(),      nullable=True),
        sa.Column("created_at",              sa.DateTime(),     server_default=sa.func.now()),
    )
    op.create_index("ix_clusteriq_clusters_project_id", "clusteriq_clusters", ["project_id"])

    # ------------------------------------------------------------------
    # clusteriq_url_clusters
    # ------------------------------------------------------------------
    op.create_table(
        "clusteriq_url_clusters",
        sa.Column("id",                   sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("url_id",               sa.Integer(), sa.ForeignKey("clusteriq_urls.id",     ondelete="CASCADE"), nullable=False),
        sa.Column("cluster_id",           sa.Integer(), sa.ForeignKey("clusteriq_clusters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role",                 sa.String(20), server_default="collateral"),
        sa.Column("impression_share_pct", sa.Float(),   nullable=True),
        sa.Column("rank_in_cluster",      sa.Integer(), nullable=True),
        sa.Column("is_multi_topic",       sa.Boolean(), server_default="0"),
    )
    op.create_index("ix_clusteriq_url_clusters_url_id",     "clusteriq_url_clusters", ["url_id"])
    op.create_index("ix_clusteriq_url_clusters_cluster_id", "clusteriq_url_clusters", ["cluster_id"])

    # ------------------------------------------------------------------
    # clusteriq_decisions
    # ------------------------------------------------------------------
    op.create_table(
        "clusteriq_decisions",
        sa.Column("id",           sa.Integer(),      primary_key=True, autoincrement=True),
        sa.Column("cluster_id",   sa.Integer(),      sa.ForeignKey("clusteriq_clusters.id", ondelete="CASCADE"),  nullable=False),
        sa.Column("url_id",       sa.Integer(),      sa.ForeignKey("clusteriq_urls.id",     ondelete="SET NULL"), nullable=True),
        sa.Column("verdict",      sa.String(20),     nullable=False),
        sa.Column("evidence",     sa.JSON(),         nullable=True),
        sa.Column("target_url",   sa.String(2048),   nullable=True),
        sa.Column("confidence",   sa.Float(),        nullable=True),
        sa.Column("generated_by", sa.String(10),     server_default="kucd"),
        sa.Column("created_at",   sa.DateTime(),     server_default=sa.func.now()),
    )
    op.create_index("ix_clusteriq_decisions_cluster_id", "clusteriq_decisions", ["cluster_id"])
    op.create_index("ix_clusteriq_decisions_url_id",     "clusteriq_decisions", ["url_id"])
    op.create_index("ix_clusteriq_decisions_verdict",    "clusteriq_decisions", ["verdict"])

    # ------------------------------------------------------------------
    # clusteriq_duplicates
    # ------------------------------------------------------------------
    op.create_table(
        "clusteriq_duplicates",
        sa.Column("id",               sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id",       sa.Integer(), sa.ForeignKey("clusteriq_projects.id", ondelete="CASCADE"),  nullable=False),
        sa.Column("winner_url_id",    sa.Integer(), sa.ForeignKey("clusteriq_urls.id",     ondelete="SET NULL"), nullable=True),
        sa.Column("duplicate_url_id", sa.Integer(), sa.ForeignKey("clusteriq_urls.id",     ondelete="SET NULL"), nullable=True),
        sa.Column("similarity_score", sa.Float(),   nullable=True),
        sa.Column("overlap_keywords", sa.JSON(),    nullable=True),
        sa.Column("winner_reason",    sa.Text(),    nullable=True),
        sa.Column("action",           sa.String(20), nullable=True),
        sa.Column("dev_brief",        sa.Text(),    nullable=True),
    )
    op.create_index("ix_clusteriq_duplicates_project_id",       "clusteriq_duplicates", ["project_id"])
    op.create_index("ix_clusteriq_duplicates_winner_url_id",    "clusteriq_duplicates", ["winner_url_id"])
    op.create_index("ix_clusteriq_duplicates_duplicate_url_id", "clusteriq_duplicates", ["duplicate_url_id"])


def downgrade() -> None:
    op.drop_table("clusteriq_duplicates")
    op.drop_table("clusteriq_decisions")
    op.drop_table("clusteriq_url_clusters")
    op.drop_table("clusteriq_clusters")
    op.drop_table("clusteriq_serp_data")
    op.drop_table("clusteriq_urls")
    op.drop_table("clusteriq_projects")
