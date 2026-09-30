"""
Pasul 7 of docs/superpowers/plans/2026-09-30-next-steps.md.

The status worker must alert only when something's state actually changes --
not every day, which nobody would keep reading past the first week.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from api.workers import status_worker


def _checks(overall_map: dict) -> dict:
    """Build a run_all_checks()-shaped dict from {check_name: status}."""
    return {"overall": "ok", "checks": {name: {"status": s} for name, s in overall_map.items()}}


class TestStatusWorkerTransitions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".pytest_tmp"))
        self.state_path = os.path.join(self.tmp, "status_worker_state.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_first_run_ever_sends_no_alert(self):
        """No previous state to compare against -- everything is new, not a transition."""
        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=_checks({"dataforseo": "ok"}))), \
             patch.object(status_worker, "_send_n8n_alert", AsyncMock(return_value=True)) as send, \
             patch("api.workers.worker_run.record_worker_run", AsyncMock()):
            result = await status_worker.run_status_check_once(self.state_path)

        send.assert_not_called()
        self.assertEqual(result["transitions"], {})
        self.assertTrue(os.path.exists(self.state_path))

    async def test_unchanged_state_sends_no_alert(self):
        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=_checks({"dataforseo": "ok"}))), \
             patch.object(status_worker, "_send_n8n_alert", AsyncMock(return_value=True)) as send, \
             patch("api.workers.worker_run.record_worker_run", AsyncMock()):
            await status_worker.run_status_check_once(self.state_path)   # first run: seeds state
            result = await status_worker.run_status_check_once(self.state_path)   # second: unchanged

        send.assert_not_called()
        self.assertEqual(result["transitions"], {})

    async def test_ok_to_fail_is_a_transition_that_alerts(self):
        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=_checks({"dataforseo": "ok"}))), \
             patch("api.workers.worker_run.record_worker_run", AsyncMock()):
            await status_worker.run_status_check_once(self.state_path)

        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=_checks({"dataforseo": "fail"}))), \
             patch.object(status_worker, "_send_n8n_alert", AsyncMock(return_value=True)) as send, \
             patch("api.workers.worker_run.record_worker_run", AsyncMock()):
            result = await status_worker.run_status_check_once(self.state_path)

        send.assert_called_once()
        self.assertEqual(result["transitions"]["dataforseo"], {"from": "ok", "to": "fail"})

    async def test_fail_to_ok_recovery_is_also_a_transition_that_alerts(self):
        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=_checks({"gsc_oauth": "fail"}))), \
             patch("api.workers.worker_run.record_worker_run", AsyncMock()):
            await status_worker.run_status_check_once(self.state_path)

        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=_checks({"gsc_oauth": "ok"}))), \
             patch.object(status_worker, "_send_n8n_alert", AsyncMock(return_value=True)) as send, \
             patch("api.workers.worker_run.record_worker_run", AsyncMock()):
            result = await status_worker.run_status_check_once(self.state_path)

        send.assert_called_once()
        self.assertEqual(result["transitions"]["gsc_oauth"], {"from": "fail", "to": "ok"})

    async def test_transition_into_or_out_of_unknown_still_alerts(self):
        """unknown -> fail (a check that started running and immediately failed) must not be silent."""
        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=_checks({"backup": "unknown"}))), \
             patch("api.workers.worker_run.record_worker_run", AsyncMock()):
            await status_worker.run_status_check_once(self.state_path)

        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=_checks({"backup": "fail"}))), \
             patch.object(status_worker, "_send_n8n_alert", AsyncMock(return_value=True)) as send, \
             patch("api.workers.worker_run.record_worker_run", AsyncMock()):
            result = await status_worker.run_status_check_once(self.state_path)

        send.assert_called_once()

    async def test_no_webhook_url_configured_is_not_an_error(self):
        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=_checks({"dataforseo": "ok"}))), \
             patch("api.workers.worker_run.record_worker_run", AsyncMock()):
            await status_worker.run_status_check_once(self.state_path)
        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=_checks({"dataforseo": "fail"}))), \
             patch.dict(os.environ, {}, clear=False), \
             patch("api.workers.worker_run.record_worker_run", AsyncMock()):
            os.environ.pop("N8N_WEBHOOK_URL", None)
            result = await status_worker.run_status_check_once(self.state_path)   # must not raise
        self.assertFalse(result["webhook_sent"])

    async def test_a_failing_system_still_records_a_successful_worker_run(self):
        """
        Regression: recording overall==fail as a failed run was self-latching --
        the next check_workers() saw status_check itself failing, so /status
        could never return to ok even after every real problem was fixed.
        """
        failing = {"overall": "fail", "checks": {"gsc_oauth": {"status": "fail"}}}
        record = AsyncMock()
        with patch("api.routes.status.run_all_checks", AsyncMock(return_value=failing)), \
             patch.object(status_worker, "_send_n8n_alert", AsyncMock(return_value=False)), \
             patch("api.workers.worker_run.record_worker_run", record):
            await status_worker.run_status_check_once(self.state_path)
        self.assertTrue(record.call_args.args[2])   # ok=True: the worker ran fine
        self.assertIn("overall=fail", record.call_args.args[3])

    async def test_a_check_run_that_raises_records_a_failed_run(self):
        record = AsyncMock()
        with patch("api.routes.status.run_all_checks", AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("api.workers.worker_run.record_worker_run", record):
            result = await status_worker.run_status_check_once(self.state_path)   # must not raise
        self.assertFalse(record.call_args.args[2])
        self.assertEqual(result["transitions"], {})


class TestSendN8nAlert(unittest.IsolatedAsyncioTestCase):
    async def test_no_webhook_url_returns_false_without_calling_httpx(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("N8N_WEBHOOK_URL", None)
            with patch("httpx.AsyncClient") as mock_client:
                sent = await status_worker._send_n8n_alert({"x": {"from": "ok", "to": "fail"}}, "fail")
        mock_client.assert_not_called()
        self.assertFalse(sent)

    async def test_ssrf_blocked_url_returns_false(self):
        with patch.dict(os.environ, {"N8N_WEBHOOK_URL": "http://169.254.169.254/latest/meta-data/"}):
            sent = await status_worker._send_n8n_alert({"x": {"from": "ok", "to": "fail"}}, "fail")
        self.assertFalse(sent)

    async def test_successful_post_returns_true(self):
        with patch.dict(os.environ, {"N8N_WEBHOOK_URL": "https://n8n.example.com/webhook/status"}), \
             patch("api.utils.url_validator.validate_external_url"), \
             patch("httpx.AsyncClient") as mock_client:
            resp = AsyncMock()
            resp.status_code = 200
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            sent = await status_worker._send_n8n_alert({"x": {"from": "ok", "to": "fail"}}, "fail")
        self.assertTrue(sent)


if __name__ == "__main__":
    unittest.main()
