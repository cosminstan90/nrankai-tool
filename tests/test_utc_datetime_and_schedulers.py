"""
UTCDateTime (api/models/_base.py) and the two schedulers it unbroke.

SQLite has no timezone storage, so plain DateTime columns came back NAIVE,
and `datetime.now(timezone.utc) - row.last_run_at` raised TypeError. The
audit scheduler and the citation-scan scheduler both did exactly that, inside
a try wrapped around the whole loop: a schedule ran once, then every later
tick crashed on it and skipped every schedule after it too.
"""
import asyncio
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from api.models._base import AsyncSessionLocal, UTCDateTime
from api.models.database import Audit, CitationTracker, ScheduledAudit


def run(coro):
    return asyncio.run(coro)


class TestUTCDateTime(unittest.TestCase):
    def test_values_read_back_are_utc_aware(self):
        sid = str(uuid.uuid4())
        written = datetime(2026, 9, 30, 14, 40, 27, tzinfo=timezone.utc)

        async def go():
            async with AsyncSessionLocal() as db:
                db.add(ScheduledAudit(id=sid, name="t", website="x.example", audit_type="SEO_AUDIT",
                                      provider="anthropic", model="m", schedule_cron="0 9 * * 1",
                                      last_run_at=written))
                await db.commit()
            async with AsyncSessionLocal() as db:
                row = await db.get(ScheduledAudit, sid)
                await db.delete(row)
                await db.commit()
                return row.last_run_at

        got = run(go())
        self.assertEqual(got, written)
        self.assertEqual(got.utcoffset(), timedelta(0))
        # The browser-side half of the bug: isoformat now carries the offset,
        # so `new Date(...)` no longer parses it as local time.
        self.assertTrue(got.isoformat().endswith("+00:00"))
        # And the Python-side half: arithmetic against an aware "now" works.
        self.assertIsInstance(datetime.now(timezone.utc) - got, timedelta)

    def test_a_non_utc_aware_value_is_stored_as_utc(self):
        bucharest = timezone(timedelta(hours=3))
        bound = UTCDateTime().process_bind_param(datetime(2026, 10, 1, 12, 0, tzinfo=bucharest), None)
        self.assertEqual(bound, datetime(2026, 10, 1, 9, 0))   # naive UTC wall-clock, the existing storage format

    def test_storage_format_is_unchanged_for_naive_values(self):
        naive = datetime(2026, 10, 1, 9, 0)
        self.assertEqual(UTCDateTime().process_bind_param(naive, None), naive)
        self.assertIsNone(UTCDateTime().process_bind_param(None, None))
        self.assertIsNone(UTCDateTime().process_result_value(None, None))


class TestAuditScheduler(unittest.TestCase):
    """api.routes.schedules.check_and_run_schedules"""

    def setUp(self):
        self.ids = []

    def tearDown(self):
        async def clean():
            async with AsyncSessionLocal() as db:
                for sid in self.ids:
                    row = await db.get(ScheduledAudit, sid)
                    if row:
                        await db.delete(row)
                audits = (await db.execute(
                    select(Audit).where(Audit.current_step.like("scheduled_%"))
                )).scalars().all()
                for a in audits:
                    if a.current_step.removeprefix("scheduled_") in self.ids:
                        await db.delete(a)
                await db.commit()
        run(clean())

    def _seed(self, last_run_at, name="s"):
        sid = str(uuid.uuid4())
        self.ids.append(sid)

        async def go():
            async with AsyncSessionLocal() as db:
                db.add(ScheduledAudit(id=sid, name=name, website="sched.example", audit_type="SEO_AUDIT",
                                      provider="anthropic", model="m", schedule_cron="* * * * *",
                                      last_run_at=last_run_at, run_count=1))
                await db.commit()
        run(go())
        return sid

    def _run_scheduler(self):
        from api.routes import schedules
        tracked = MagicMock()
        with patch.object(schedules, "create_tracked_task", tracked), \
             patch.object(schedules, "start_audit_pipeline", MagicMock()):
            run(schedules.check_and_run_schedules())
        return tracked

    def test_a_schedule_that_already_ran_once_runs_again(self):
        """Regression: last_run_at set -> naive/aware TypeError -> never ran again."""
        sid = self._seed(datetime.now(timezone.utc) - timedelta(hours=2))
        tracked = self._run_scheduler()
        self.assertEqual(tracked.call_count, 1)

        async def check():
            async with AsyncSessionLocal() as db:
                return await db.get(ScheduledAudit, sid)
        row = run(check())
        self.assertEqual(row.run_count, 2)
        self.assertLess(datetime.now(timezone.utc) - row.last_run_at, timedelta(minutes=1))

    def test_the_30_minute_double_run_guard_still_holds(self):
        self._seed(datetime.now(timezone.utc) - timedelta(minutes=5))
        tracked = self._run_scheduler()
        tracked.assert_not_called()

    def test_one_failing_schedule_does_not_stop_the_others(self):
        from api.routes import schedules
        bad = self._seed(None, name="bad")
        good = self._seed(None, name="good")
        real = schedules._run_schedule_if_due
        seen = []

        async def flaky(db, schedule, now):
            seen.append(schedule.id)
            if schedule.id == bad:
                raise RuntimeError("boom")
            return await real(db, schedule, now)

        tracked = MagicMock()
        with patch.object(schedules, "_run_schedule_if_due", flaky), \
             patch.object(schedules, "create_tracked_task", tracked), \
             patch.object(schedules, "start_audit_pipeline", MagicMock()):
            run(schedules.check_and_run_schedules())
        self.assertIn(good, seen)
        self.assertEqual(tracked.call_count, 1)   # the good one still started


class TestScheduleCrud(unittest.TestCase):
    """
    The schedules API had never worked end to end: the router wrote eight
    columns ScheduledAudit never had ('language' is an invalid keyword
    argument), and an omitted model hit the table's NOT NULL. Exercises every
    schedule route through the real app so a model/router mismatch can't
    hide again.
    """

    def setUp(self):
        from api.main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)

    def test_create_read_update_history_delete(self):
        resp = self.client.post("/api/schedules", json={
            "name": "crud", "website": "crud.example", "audit_type": "SEO_AUDIT",
            "provider": "anthropic", "schedule_cron": "0 3 1 1 *",
            "language": "Romanian", "concurrency": 3,
        })   # no "model": must default, not violate NOT NULL
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        sid = body["id"]
        try:
            self.assertEqual(body["model"], "default")
            self.assertEqual(body["language"], "Romanian")
            self.assertEqual(body["concurrency"], 3)
            self.assertEqual(body["run_count"], 0)
            self.assertTrue(body["created_at"].endswith("+00:00"))

            self.assertEqual(self.client.get(f"/api/schedules/{sid}").status_code, 200)
            self.assertEqual(self.client.get(f"/api/schedules/{sid}/history").status_code, 200)
            listed = self.client.get("/api/schedules").json()
            self.assertIn(sid, json.dumps(listed))

            upd = self.client.patch(f"/api/schedules/{sid}", json={"language": "English", "concurrency": 7})
            self.assertEqual(upd.status_code, 200, upd.text)
            after = self.client.get(f"/api/schedules/{sid}").json()
            self.assertIn('"concurrency": 7', json.dumps(after))
        finally:
            self.assertIn(self.client.delete(f"/api/schedules/{sid}").status_code, (200, 204))
        self.assertEqual(self.client.get(f"/api/schedules/{sid}").status_code, 404)


class TestCitationScanScheduler(unittest.TestCase):
    """api.routes.visibility.check_and_run_citation_scans"""

    def setUp(self):
        self.tid = str(uuid.uuid4())

        async def go():
            async with AsyncSessionLocal() as db:
                db.add(CitationTracker(
                    id=self.tid, name="sched", website="sched.example",
                    url_patterns=json.dumps(["sched.example"]), tracking_queries=json.dumps(["q"]),
                    providers_config=json.dumps({}), schedule_cron="* * * * *", is_active=1,
                    last_scan_at=datetime.now(timezone.utc) - timedelta(hours=2),
                ))
                await db.commit()
        run(go())

    def tearDown(self):
        async def clean():
            async with AsyncSessionLocal() as db:
                row = await db.get(CitationTracker, self.tid)
                if row:
                    await db.delete(row)
                await db.commit()
        run(clean())

    def test_a_tracker_scanned_before_is_scanned_again_via_a_tracked_task(self):
        from api.routes import visibility
        tracked = MagicMock()
        with patch.object(visibility, "create_tracked_task", tracked), \
             patch.object(visibility, "_run_visibility_scan", MagicMock()):
            run(visibility.check_and_run_citation_scans())
        names = [c.kwargs.get("name") for c in tracked.call_args_list]
        self.assertIn(f"scheduled-visibility-scan-{self.tid}", names)


if __name__ == "__main__":
    unittest.main()
