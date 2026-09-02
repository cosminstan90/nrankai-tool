"""Add geo_monitor-distinctive columns to citation_trackers/citation_scans.

Etapa 3 of the consolidation (docs/CONSOLIDATION_PLAN.md): citation_tracker.py
and geo_monitor.py are the same feature written twice -- same lifecycle
(create target -> generate queries -> query LLM providers -> analyze
mentions/citations -> trend -> alert on drop), different vocabulary
("tracker"/"citation" vs "project"/"visibility"). ai_visibility.py exists
solely to merge them back together, which was the tell.

geo_monitor_projects/geo_monitor_scans have 0 rows in production (confirmed
via docs/audit/FINDINGS.md data audit) vs. citation_trackers/citation_scans'
1 tracker + 5 scans -- so citation_trackers/citation_scans become the single
source of truth, extended with the two things geo_monitor had that citation
tracking didn't:

  citation_trackers:
    - brand_keywords   (JSON array, broader keyword-mention matching --
                         url_patterns alone only catches URL citations)
    - language         (was hardcoded English-only for citation tracking)
    - competitors      (JSON list of {name, brand_keywords, website} --
                         geo_monitor's competitor-mention tracking, which
                         citation tracking had no equivalent of at all)

  citation_scans:
    - visibility_score   (mention rate -- citation_rate only counted URL
                           citations, a narrower/harder bar than a plain
                           brand mention)
    - competitor_scores  (JSON: {website: {name, mention_rate}})

Not duplicated: citation_scans.total_mentions and .total_queries already
serve the same role as geo_monitor_scans.mentioned_count/.total_queries --
no new columns needed for those (and no data to migrate, since geo_monitor
tables are empty).

geo_monitor_projects/geo_monitor_scans and their model classes are left in
place for now (not dropped) -- the route module built on top of them is
being replaced, not the tables themselves; dropping them is a separate,
lower-risk cleanup once the new unified module has run in production for a
while. See api/routes/visibility.py for the replacement.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0010"
down_revision: Union[str, None]                = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("citation_trackers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("brand_keywords", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("language", sa.String(length=50), nullable=True, server_default="English")
        )
        batch_op.add_column(sa.Column("competitors", sa.JSON(), nullable=True))

    with op.batch_alter_table("citation_scans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("visibility_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("competitor_scores", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("citation_scans", schema=None) as batch_op:
        batch_op.drop_column("competitor_scores")
        batch_op.drop_column("visibility_score")

    with op.batch_alter_table("citation_trackers", schema=None) as batch_op:
        batch_op.drop_column("competitors")
        batch_op.drop_column("language")
        batch_op.drop_column("brand_keywords")
