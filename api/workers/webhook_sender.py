"""
Webhook Sender (Prompt 20)
==========================
Sends signed JSON payloads to registered webhook endpoints for fan-out events.

Supported events:
  tracking_run_completed | mention_rate_drop | mention_rate_spike
  new_competitor_detected | tracking_run_failed | discovery_completed
  entity_check_completed

HMAC-SHA256 signature:
    X-Webhook-Signature: sha256=<hex>   (keyed on secret_key)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from api.utils.url_validator import validate_external_url

logger = logging.getLogger("webhook_sender")

_TOOL = "nrankai-fanout-analyzer"

ALL_EVENTS: List[str] = [
    "tracking_run_completed",
    "mention_rate_drop",
    "mention_rate_spike",
    "new_competitor_detected",
    "tracking_run_failed",
    "discovery_completed",
    "entity_check_completed",
]


async def send(
    webhook_url: str,
    event_type: str,
    payload: dict,
    secret_key: Optional[str] = None,
) -> bool:
    """
    POST a signed event payload to *webhook_url*.
    Returns True on success, False on failure. Never raises.
    """
    try:
        validate_external_url(webhook_url, "webhook_url")
    except ValueError as e:
        logger.warning("Webhook blocked (SSRF protection): %s", e)
        return False

    try:
        import httpx
    except ImportError:
        logger.error("httpx not installed — cannot send webhook")
        return False

    body = {
        "event":     event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool":      _TOOL,
        "data":      payload,
    }
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if secret_key:
        sig = hmac.new(secret_key.encode(), body_bytes, hashlib.sha256).hexdigest()  # type: ignore[attr-defined]
        headers["X-Webhook-Signature"] = f"sha256={sig}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, content=body_bytes, headers=headers)
            ok = 200 <= resp.status_code < 300
            if not ok:
                logger.warning("Webhook %s returned HTTP %d", webhook_url, resp.status_code)
            return ok
    except Exception as exc:
        logger.warning("Webhook delivery failed for %s: %s", webhook_url, exc)
        return False


async def send_to_all(
    db,
    event_type: str,
    payload: dict,
) -> None:
    """
    Send *event_type* to all active, subscribed webhooks.
    Errors are logged but never propagate — callers must not await failures.
    """
    from sqlalchemy import select
    from api.models.database import FanoutWebhook, FanoutWebhookLog

    try:
        stmt = select(FanoutWebhook).where(FanoutWebhook.is_active == True)  # noqa: E712
        rows = (await db.execute(stmt)).scalars().all()
    except Exception as exc:
        logger.error("Failed to load webhooks from DB: %s", exc)
        return

    for wh in rows:
        events = wh.events or []
        if event_type not in events:
            continue

        body_preview = json.dumps(payload)
        ok = await send(wh.webhook_url, event_type, payload, wh.secret_key)

        try:
            log = FanoutWebhookLog(
                webhook_id   = wh.id,
                event_type   = event_type,
                status       = "success" if ok else "failure",
                payload_size = len(body_preview),
            )
            db.add(log)
            await db.commit()
        except Exception as exc:
            logger.warning("Failed to log webhook delivery: %s", exc)
            try:
                await db.rollback()
            except Exception:
                pass


# ── Convenience fire-and-forget helpers ──────────────────────────────────────

async def fire_tracking_completed(db, config_id: str, run: dict) -> None:
    await send_to_all(db, "tracking_run_completed", {
        "config_id":       config_id,
        "mention_rate":    run.get("mention_rate"),
        "composite_score": run.get("composite_score"),
        "run_date":        run.get("run_date"),
    })


async def fire_mention_rate_drop(db, config_id: str, current: float, previous: float) -> None:
    drop_pct = round((previous - current) / previous * 100, 1) if previous else 0.0
    await send_to_all(db, "mention_rate_drop", {
        "config_id":   config_id,
        "current":     current,
        "previous":    previous,
        "drop_pct":    drop_pct,
        "alert_level": "warning",
    })


async def fire_mention_rate_spike(db, config_id: str, current: float, previous: float) -> None:
    spike_pct = round((current - previous) / previous * 100, 1) if previous else 0.0
    await send_to_all(db, "mention_rate_spike", {
        "config_id": config_id,
        "current":   current,
        "previous":  previous,
        "spike_pct": spike_pct,
    })


async def fire_tracking_failed(db, config_id: str, run_id: str, reason: str, retry_count: int) -> None:
    await send_to_all(db, "tracking_run_failed", {
        "config_id":    config_id,
        "run_id":       run_id,
        "failure_reason": reason,
        "retry_count":  retry_count,
    })


async def fire_discovery_completed(db, result: dict, domain: str) -> None:
    await send_to_all(db, "discovery_completed", {
        "target_domain": domain,
        "mention_rate":  result.get("mention_rate"),
        "total_prompts": result.get("total_prompts"),
    })
