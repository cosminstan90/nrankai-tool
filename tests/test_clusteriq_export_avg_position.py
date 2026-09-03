"""
Pins the fix from Etapa 5.4 of the consolidation (docs/CONSOLIDATION_PLAN.md):
app/modules/clusteriq/router.py's export_decisions_csv() and
export_prune_list_csv() referenced CluCluster.avg_position directly inside
a select(...) call -- but CluCluster has no such column (confirmed against
api/models/clusteriq.py: its real fields are total_impressions,
total_clicks, url_count, search_demand_confirmed, louvain_community_id).
avg_position only ever existed as a per-URL, on-the-fly aggregate joining
CluSerpData (see get_cluster_detail()). Referencing it as a class attribute
raised AttributeError before the query could even execute -- both CSV
export endpoints crashed on every single call since the feature was
written.

_avg_position_by_cluster() fixes this by aggregating CluSerpData.position
across every URL belonging to each cluster (via CluUrlCluster -> CluUrl ->
CluSerpData), reusing the same join pattern get_cluster_detail() already
used at the per-URL granularity, one level up.
"""
import unittest

from api.models._base import AsyncSessionLocal
from api.models.clusteriq import (
    CluCluster, CluDecision, CluProject, CluSerpData, CluUrl, CluUrlCluster,
)
from app.modules.clusteriq.router import CluCluster as RouterCluCluster, _avg_position_by_cluster


class TestCluClusterHasNoAvgPositionColumn(unittest.TestCase):
    def test_avg_position_is_not_a_real_column(self):
        """Documents why the fix computes this via a join instead of a
        direct column reference -- if this ever starts passing, the model
        gained the column and the join-based helper may be simplifiable."""
        with self.assertRaises(AttributeError):
            _ = RouterCluCluster.avg_position


class TestAvgPositionByCluster(unittest.IsolatedAsyncioTestCase):
    async def test_averages_position_across_cluster_member_urls(self):
        async with AsyncSessionLocal() as db:
            project = CluProject(domain="test-avgpos.example.com", status="completed")
            db.add(project)
            await db.flush()

            url1 = CluUrl(project_id=project.id, url="https://test-avgpos.example.com/a")
            url2 = CluUrl(project_id=project.id, url="https://test-avgpos.example.com/b")
            db.add_all([url1, url2])
            await db.flush()

            cluster = CluCluster(
                project_id=project.id, cluster_label="c", total_impressions=100, url_count=2,
            )
            db.add(cluster)
            await db.flush()

            db.add_all([
                CluUrlCluster(url_id=url1.id, cluster_id=cluster.id, role="primary"),
                CluUrlCluster(url_id=url2.id, cluster_id=cluster.id, role="collateral"),
            ])
            db.add_all([
                CluSerpData(project_id=project.id, keyword="k1", url=url1.url, position=4.0, data_source="gsc"),
                CluSerpData(project_id=project.id, keyword="k2", url=url2.url, position=10.0, data_source="gsc"),
            ])
            await db.commit()

            try:
                result = await _avg_position_by_cluster(db, project.id, [cluster.id])
                self.assertAlmostEqual(result[cluster.id], 7.0)
            finally:
                await db.execute(CluSerpData.__table__.delete().where(CluSerpData.project_id == project.id))
                await db.execute(CluUrlCluster.__table__.delete().where(CluUrlCluster.cluster_id == cluster.id))
                await db.execute(CluCluster.__table__.delete().where(CluCluster.id == cluster.id))
                await db.execute(CluUrl.__table__.delete().where(CluUrl.project_id == project.id))
                await db.execute(CluProject.__table__.delete().where(CluProject.id == project.id))
                await db.commit()

    async def test_empty_cluster_ids_returns_empty_dict(self):
        async with AsyncSessionLocal() as db:
            result = await _avg_position_by_cluster(db, 999999, [])
            self.assertEqual(result, {})

    async def test_cluster_with_no_serp_data_omitted_not_crashed(self):
        async with AsyncSessionLocal() as db:
            project = CluProject(domain="test-avgpos2.example.com", status="completed")
            db.add(project)
            await db.flush()

            cluster = CluCluster(project_id=project.id, cluster_label="empty", url_count=0)
            db.add(cluster)
            await db.flush()
            await db.commit()

            try:
                result = await _avg_position_by_cluster(db, project.id, [cluster.id])
                # No matching CluSerpData rows -> no row in the aggregate result at all
                self.assertNotIn(cluster.id, result)
            finally:
                await db.execute(CluCluster.__table__.delete().where(CluCluster.id == cluster.id))
                await db.execute(CluProject.__table__.delete().where(CluProject.id == project.id))
                await db.commit()


if __name__ == "__main__":
    unittest.main()
