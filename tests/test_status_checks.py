"""
Pasul 7 of docs/superpowers/plans/2026-09-30-next-steps.md.

Every real failure so far (Claude failing every scan for months, Perplexity's
key rejected, GSC OAuth silently disconnected, the sync route broken for
weeks, DataForSEO credit running out, the dev machine's C: drive at 0 bytes
free) failed silently. These test that GET /api/status actually surfaces each
one, with external calls mocked -- "unknown" (not measured) is never confused
with "ok" (measured, working).
"""
import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from api.models._base import AsyncSessionLocal
from api.models.database import WorkerRun
from api.routes import status as status_mod


class TestCheckProviders(unittest.IsolatedAsyncioTestCase):
    async def test_a_working_provider_is_ok(self):
        with patch("api.routes.health._run_provider_checks",
                   AsyncMock(return_value={"anthropic": {"ok": True, "response_ms": 120}})):
            result = await status_mod.check_providers()
        self.assertEqual(result["anthropic"]["status"], "ok")

    async def test_a_broken_provider_is_fail_not_unknown(self):
        """Perplexity-401-shaped case: key present, probe fails -- must not look unmeasured."""
        with patch("api.routes.health._run_provider_checks",
                   AsyncMock(return_value={"anthropic": {"ok": False, "error": "401 unauthorized"}})):
            result = await status_mod.check_providers()
        self.assertEqual(result["anthropic"]["status"], "fail")
        self.assertIn("401", result["anthropic"]["detail"])

    async def test_a_provider_with_no_key_is_unknown_not_ok(self):
        with patch("api.routes.health._run_provider_checks", AsyncMock(return_value={})):
            result = await status_mod.check_providers()
        for provider in ("anthropic", "openai", "mistral", "google"):
            self.assertEqual(result[provider]["status"], "unknown")


class TestCheckDataForSEO(unittest.IsolatedAsyncioTestCase):
    async def test_not_configured_is_unknown(self):
        with patch("core.serp_client.dfs_configured", return_value=False):
            result = await status_mod.check_dataforseo()
        self.assertEqual(result["status"], "unknown")

    async def test_healthy_balance_is_ok(self):
        body = {"tasks": [{"result": [{"money": {"balance": 42.0}}]}]}
        with patch("core.serp_client.dfs_configured", return_value=True), \
             patch.dict(os.environ, {"DATAFORSEO_BALANCE_WARN_USD": "5"}), \
             patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=AsyncMock(json=lambda: body))
            result = await status_mod.check_dataforseo()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["balance_usd"], 42.0)

    async def test_low_balance_is_warn(self):
        """The account had $0.89 left in an earlier session -- this is exactly that case."""
        body = {"tasks": [{"result": [{"money": {"balance": 0.89}}]}]}
        with patch("core.serp_client.dfs_configured", return_value=True), \
             patch.dict(os.environ, {"DATAFORSEO_BALANCE_WARN_USD": "5"}), \
             patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=AsyncMock(json=lambda: body))
            result = await status_mod.check_dataforseo()
        self.assertEqual(result["status"], "warn")

    async def test_request_failure_is_unknown_not_fail(self):
        with patch("core.serp_client.dfs_configured", return_value=True), \
             patch("httpx.AsyncClient", side_effect=RuntimeError("network down")):
            result = await status_mod.check_dataforseo()
        self.assertEqual(result["status"], "unknown")


class TestCheckGscOauth(unittest.IsolatedAsyncioTestCase):
    async def test_not_configured_is_unknown(self):
        with patch("api.routes.gsc._shared._oauth_available", return_value=False):
            result = await status_mod.check_gsc_oauth()
        self.assertEqual(result["status"], "unknown")

    async def test_configured_but_not_connected_is_fail(self):
        """The real case on 2026-09-30: google_oauth_tokens empty since March."""
        with patch("api.routes.gsc._shared._oauth_available", return_value=True), \
             patch("api.routes.gsc._shared._get_gsc_credentials", AsyncMock(return_value=None)):
            result = await status_mod.check_gsc_oauth()
        self.assertEqual(result["status"], "fail")

    async def test_connected_is_ok(self):
        with patch("api.routes.gsc._shared._oauth_available", return_value=True), \
             patch("api.routes.gsc._shared._get_gsc_credentials", AsyncMock(return_value=object())):
            result = await status_mod.check_gsc_oauth()
        self.assertEqual(result["status"], "ok")

    async def test_refresh_error_is_fail(self):
        with patch("api.routes.gsc._shared._oauth_available", return_value=True), \
             patch("api.routes.gsc._shared._get_gsc_credentials", AsyncMock(side_effect=RuntimeError("token revoked"))):
            result = await status_mod.check_gsc_oauth()
        self.assertEqual(result["status"], "fail")


class TestCheckWorkers(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        async with AsyncSessionLocal() as db:
            from sqlalchemy import delete
            await db.execute(delete(WorkerRun).where(WorkerRun.worker.like("test_%")))
            await db.commit()

    async def _add_run(self, worker, finished_at, ok, detail="x"):
        async with AsyncSessionLocal() as db:
            db.add(WorkerRun(worker=worker, started_at=finished_at, finished_at=finished_at, ok=ok, detail=detail))
            await db.commit()

    async def test_a_recent_successful_run_is_ok(self):
        with patch.object(status_mod, "WORKER_STALENESS_HOURS", {"test_w": (30, 72)}):
            await self._add_run("test_w", datetime.now(timezone.utc) - timedelta(minutes=5), True)
            result = await status_mod.check_workers()
        self.assertEqual(result["test_w"]["status"], "ok")

    async def test_a_failed_run_is_fail_regardless_of_age(self):
        with patch.object(status_mod, "WORKER_STALENESS_HOURS", {"test_w": (30, 72)}):
            await self._add_run("test_w", datetime.now(timezone.utc) - timedelta(minutes=1), False)
            result = await status_mod.check_workers()
        self.assertEqual(result["test_w"]["status"], "fail")

    async def test_a_stale_worker_is_warn_then_fail(self):
        with patch.object(status_mod, "WORKER_STALENESS_HOURS", {"test_w": (30, 72)}):
            await self._add_run("test_w", datetime.now(timezone.utc) - timedelta(hours=40), True)
            warn_result = await status_mod.check_workers()
            self.assertEqual(warn_result["test_w"]["status"], "warn")

            await self._add_run("test_w", datetime.now(timezone.utc) - timedelta(hours=80), True)
            # the 40h-old row is still the *older* one; add a fresher 80h fail-threshold row instead
        async with AsyncSessionLocal() as db:
            from sqlalchemy import delete
            await db.execute(delete(WorkerRun).where(WorkerRun.worker == "test_w"))
            await db.commit()
        with patch.object(status_mod, "WORKER_STALENESS_HOURS", {"test_w": (30, 72)}):
            await self._add_run("test_w", datetime.now(timezone.utc) - timedelta(hours=80), True)
            fail_result = await status_mod.check_workers()
        self.assertEqual(fail_result["test_w"]["status"], "fail")

    async def test_a_worker_with_a_staleness_threshold_but_no_runs_is_unknown(self):
        with patch.object(status_mod, "WORKER_STALENESS_HOURS", {"test_never_ran": (30, 72)}):
            result = await status_mod.check_workers()
        self.assertEqual(result["test_never_ran"]["status"], "unknown")


class TestCheckBackup(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(dir=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".pytest_tmp"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, data):
        with open(os.path.join(self.tmp, "last_run.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_no_backup_ever_run_is_unknown(self):
        with patch("scripts.backup_db.DEFAULT_BACKUP_ROOT", self.tmp):
            result = status_mod.check_backup()
        self.assertEqual(result["status"], "unknown")

    def test_recent_verified_backup_is_ok(self):
        self._write({"ok": True, "ran_at": datetime.now(timezone.utc).isoformat(), "daily_detail": "ok"})
        with patch("scripts.backup_db.DEFAULT_BACKUP_ROOT", self.tmp):
            result = status_mod.check_backup()
        self.assertEqual(result["status"], "ok")

    def test_failed_backup_is_fail(self):
        self._write({"ok": False, "ran_at": datetime.now(timezone.utc).isoformat(), "daily_detail": "integrity_check failed"})
        with patch("scripts.backup_db.DEFAULT_BACKUP_ROOT", self.tmp):
            result = status_mod.check_backup()
        self.assertEqual(result["status"], "fail")

    def test_stale_backup_is_warn_then_fail(self):
        self._write({"ok": True, "ran_at": (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat(), "daily_detail": "ok"})
        with patch("scripts.backup_db.DEFAULT_BACKUP_ROOT", self.tmp):
            result = status_mod.check_backup()
        self.assertEqual(result["status"], "warn")

        self._write({"ok": True, "ran_at": (datetime.now(timezone.utc) - timedelta(hours=80)).isoformat(), "daily_detail": "ok"})
        with patch("scripts.backup_db.DEFAULT_BACKUP_ROOT", self.tmp):
            result = status_mod.check_backup()
        self.assertEqual(result["status"], "fail")


class TestCheckDisk(unittest.TestCase):
    def test_returns_a_status_for_both_drives(self):
        result = status_mod.check_disk()
        self.assertIn("db_drive", result)
        self.assertIn("c_drive", result)
        for check in result.values():
            self.assertIn(check["status"], ("ok", "warn", "fail", "unknown"))


class TestOverallSeverity(unittest.TestCase):
    def test_worst_status_wins_but_unknown_never_outranks_ok(self):
        self.assertEqual(status_mod._worst(["ok", "unknown"]), "ok")
        self.assertEqual(status_mod._worst(["ok", "warn"]), "warn")
        self.assertEqual(status_mod._worst(["ok", "warn", "fail"]), "fail")
        self.assertEqual(status_mod._worst(["unknown", "unknown"]), "ok")


class TestStatusEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_get_status_returns_the_expected_shape(self):
        with patch("api.routes.health._run_provider_checks", AsyncMock(return_value={})), \
             patch("core.serp_client.dfs_configured", return_value=False), \
             patch("api.routes.gsc._shared._oauth_available", return_value=False):
            result = await status_mod.get_status()
        self.assertIn("overall", result)
        self.assertIn("checked_at", result)
        for key in ("providers", "dataforseo", "gsc_oauth", "workers", "backup", "disk"):
            self.assertIn(key, result["checks"])


if __name__ == "__main__":
    unittest.main()
