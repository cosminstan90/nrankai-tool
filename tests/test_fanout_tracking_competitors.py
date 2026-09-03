"""
Etapa 7 of the consolidation (docs/CONSOLIDATION_PLAN.md): "Competitori in
raspunsuri AI" -- FanoutTrackingRun.top_competitors was computed and stored
by api/workers/fanout_tracker_worker.py but never read by any endpoint or
rendered anywhere (confirmed by grep + reading every consumer before writing
this). GET /api/fanout/tracking/{config_id}/competitors aggregates it across
a config's completed runs into one ranked view.
"""
import unittest

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.content import FanoutTrackingConfig, FanoutTrackingRun
from api.routes.fanout import get_tracking_competitors


class TestGetTrackingCompetitors(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            configs = (await db.execute(
                select(FanoutTrackingConfig).where(FanoutTrackingConfig.name == "test-competitors-agg")
            )).scalars().all()
            for c in configs:
                await db.delete(c)  # cascades to FanoutTrackingRun
            await db.commit()

    async def _make_config_with_runs(self, db):
        config = FanoutTrackingConfig(
            name="test-competitors-agg", target_domain="mysite.example.com",
            prompts=["p"], engines=["openai"],
        )
        db.add(config)
        await db.flush()
        db.add_all([
            FanoutTrackingRun(
                config_id=config.id, run_date="2026-08-01", status="completed", mention_rate=0.5,
                top_competitors=[{"domain": "rival-a.com", "appearances": 3}, {"domain": "rival-b.com", "appearances": 1}],
            ),
            FanoutTrackingRun(
                config_id=config.id, run_date="2026-08-08", status="completed", mention_rate=0.4,
                top_competitors=[{"domain": "rival-a.com", "appearances": 2}],
            ),
            FanoutTrackingRun(
                config_id=config.id, run_date="2026-08-15", status="failed", mention_rate=None,
                top_competitors=None,
            ),
        ])
        await db.commit()
        return config.id

    async def test_aggregates_across_completed_runs_only(self):
        async with AsyncSessionLocal() as db:
            config_id = await self._make_config_with_runs(db)

        async with AsyncSessionLocal() as db:
            result = await get_tracking_competitors(config_id, "all", db)

        self.assertEqual(result["run_count"], 2)  # failed run excluded
        by_domain = {c["domain"]: c for c in result["competitors"]}
        self.assertEqual(by_domain["rival-a.com"]["total_appearances"], 5)
        self.assertEqual(by_domain["rival-a.com"]["runs_appeared_in"], 2)
        self.assertEqual(by_domain["rival-a.com"]["last_seen_date"], "2026-08-08")
        self.assertEqual(by_domain["rival-b.com"]["total_appearances"], 1)

    async def test_sorted_by_total_appearances_descending(self):
        async with AsyncSessionLocal() as db:
            config_id = await self._make_config_with_runs(db)

        async with AsyncSessionLocal() as db:
            result = await get_tracking_competitors(config_id, "all", db)

        domains_in_order = [c["domain"] for c in result["competitors"]]
        self.assertEqual(domains_in_order, ["rival-a.com", "rival-b.com"])

    async def test_no_runs_returns_empty_competitors(self):
        async with AsyncSessionLocal() as db:
            config = FanoutTrackingConfig(
                name="test-competitors-agg", target_domain="empty.example.com",
                prompts=["p"], engines=["openai"],
            )
            db.add(config)
            await db.flush()
            config_id = config.id
            await db.commit()

        async with AsyncSessionLocal() as db:
            result = await get_tracking_competitors(config_id, "all", db)

        self.assertEqual(result["competitors"], [])
        self.assertEqual(result["run_count"], 0)


if __name__ == "__main__":
    unittest.main()
