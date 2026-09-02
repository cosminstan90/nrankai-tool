"""
Pins the fix from Etapa 4.6 of the consolidation (docs/CONSOLIDATION_PLAN.md):
api/workers/cocitation_analyzer.py's build_cocitation_map() read
FanoutSource.source_url, an attribute that has never existed on that model
(the real column is `url` -- see api/models/content.py:575). Every call
crashed with AttributeError, so /api/cocitation/analyze has never worked.
"""
import asyncio
import unittest
import uuid
from datetime import datetime, timezone

from api.models._base import AsyncSessionLocal
from api.models.database import FanoutSession, FanoutSource
from api.workers.cocitation_analyzer import build_cocitation_map, classify_relationship


class TestClassifyRelationship(unittest.TestCase):
    def test_directory_domain(self):
        self.assertEqual(classify_relationship("yelp.com"), "directory")

    def test_review_site_domain(self):
        self.assertEqual(classify_relationship("g2.com"), "review_site")

    def test_default_is_competitor(self):
        self.assertEqual(classify_relationship("monday.com"), "competitor")


class TestBuildCocitationMap(unittest.TestCase):
    def test_reads_fanout_source_url_column_without_crashing(self):
        """Regression test for the source_url -> url attribute bug."""

        async def _run():
            async with AsyncSessionLocal() as db:
                sid = str(uuid.uuid4())
                db.add(FanoutSession(
                    id=sid, prompt="best pm tools", provider="openai", model="gpt-4o",
                    target_url="asana.com", created_at=datetime.now(timezone.utc),
                ))
                db.add(FanoutSource(session_id=sid, url="https://asana.com/features", domain="asana.com"))
                db.add(FanoutSource(session_id=sid, url="https://monday.com/pricing", domain="monday.com"))
                await db.commit()

                try:
                    result = await build_cocitation_map(
                        target_brand="Asana", target_domain="asana.com",
                        fanout_session_ids=[sid], db=db, period_days=30,
                    )
                finally:
                    await db.execute(FanoutSource.__table__.delete().where(FanoutSource.session_id == sid))
                    await db.execute(FanoutSession.__table__.delete().where(FanoutSession.id == sid))
                    await db.commit()

                return result

        result = asyncio.run(_run())
        self.assertEqual(result["sessions_with_target"], 1)
        self.assertEqual(result["frequent_co_citations"][0]["domain"], "monday.com")
        self.assertEqual(result["frequent_co_citations"][0]["relationship"], "competitor")


if __name__ == "__main__":
    unittest.main()
