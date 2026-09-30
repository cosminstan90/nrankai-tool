"""
Daily status-check worker -- Pasul 7 of docs/superpowers/plans/2026-09-30-next-steps.md.

Runs api.routes.status.run_all_checks() once a day and posts to N8N_WEBHOOK_URL
only when something's state actually changed since the last run (ok<->warn,
warn<->fail, etc.) -- not every day, which would just be noise nobody reads
after the first week. The previous run's per-check statuses are kept in a
small local JSON file (status_worker_state.json, next to analyzer.db, so it's
gitignored like the rest of api/data/) rather than a DB table: it's throwaway
comparison state, not data anyone queries.

This is a *different* webhook mechanism from api/workers/webhook_sender.py's
FanoutWebhook subscriptions (a fixed list of fan-out-specific event types,
HMAC-signed, per-project). A status change isn't a fan-out event and doesn't
fit that model; this posts directly to N8N_WEBHOOK_URL instead.

Disabled with STATUS_WORKER_ENABLED=0. On by default.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

STARTUP_DELAY_S = 90
POLL_INTERVAL_S = 24 * 3600


def _default_state_path() -> str:
    from api.models._base import DATABASE_PATH
    return os.path.join(os.path.dirname(DATABASE_PATH), "status_worker_state.json")


def _flatten(checks: dict) -> dict:
    """{"providers.anthropic": "ok", "dataforseo": "warn", "workers.gsc_archive": "ok", ...}"""
    flat = {}
    for name, check in checks.items():
        if "status" in check:
            flat[name] = check["status"]
        else:
            for sub, subcheck in check.items():
                flat[f"{name}.{sub}"] = subcheck.get("status", "unknown")
    return flat


def _load_previous(state_path: str) -> dict:
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, encoding="utf-8") as f:
            return json.load(f).get("statuses", {})
    except Exception:
        return {}


def _save_state(state_path: str, statuses: dict, overall: str) -> None:
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({
            "statuses": statuses, "overall": overall,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2)


async def _send_n8n_alert(transitions: dict, overall: str) -> bool:
    webhook_url = os.getenv("N8N_WEBHOOK_URL")
    if not webhook_url:
        return False

    from api.utils.url_validator import validate_external_url
    try:
        validate_external_url(webhook_url, "webhook_url")
    except ValueError as exc:
        logger.warning("Status webhook blocked (SSRF protection): %s", exc)
        return False

    body = {
        "event": "status_change",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": "nrankai-status",
        "overall": overall,
        "transitions": transitions,
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=body)
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning("Status webhook %s returned HTTP %d", webhook_url, resp.status_code)
        return ok
    except Exception as exc:
        logger.warning("Status webhook delivery failed for %s: %s", webhook_url, exc)
        return False


async def run_status_check_once(state_path: Optional[str] = None) -> dict:
    """Run every check once, alert only on a state transition. Never raises."""
    from api.routes.status import run_all_checks
    from api.workers.worker_run import record_worker_run

    state_path = state_path or _default_state_path()
    started_at = datetime.now(timezone.utc)

    try:
        result = await run_all_checks()
    except Exception as exc:
        logger.error("Status check run failed: %s", exc)
        await record_worker_run("status_check", started_at, False, f"check run raised: {exc}")
        return {"overall": "unknown", "transitions": {}, "webhook_sent": False}
    current = _flatten(result["checks"])
    previous = _load_previous(state_path)

    transitions = {
        check: {"from": previous[check], "to": status}
        for check, status in current.items()
        if check in previous and previous[check] != status
    }

    webhook_sent = await _send_n8n_alert(transitions, result["overall"]) if transitions else False
    _save_state(state_path, current, result["overall"])

    # ok means "this worker ran", not "the system is healthy". Recording
    # overall==fail as a failed run made it self-latching: the next run's
    # check_workers saw status_check itself failing, so overall stayed
    # "fail" forever even after every real problem was fixed.
    await record_worker_run(
        "status_check", started_at, True,
        f"overall={result['overall']}, {len(transitions)} transition(s), webhook_sent={webhook_sent}",
    )

    return {"overall": result["overall"], "transitions": transitions, "webhook_sent": webhook_sent}


async def status_worker_loop():
    """Background loop: checks system health once a day."""
    if os.getenv("STATUS_WORKER_ENABLED", "1") == "0":
        logger.info("STATUS_WORKER_ENABLED=0 — status worker disabled")
        return

    logger.info("Status worker started (daily)")
    await asyncio.sleep(STARTUP_DELAY_S)

    while True:
        try:
            await run_status_check_once()
        except Exception:
            logger.exception("Status worker: unhandled error in run")
        await asyncio.sleep(POLL_INTERVAL_S)
