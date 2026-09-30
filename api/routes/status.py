"""
Health/status panel -- Pasul 7 of docs/superpowers/plans/2026-09-30-next-steps.md.

GET /api/health is the existing, external, always-on liveness probe (DB
connectivity, provider key presence, optional deep provider probes) --
intentionally untouched here, since it's exempted from BasicAuthMiddleware and
used by outside monitoring.

GET /api/status is different in kind: an internal panel answering "is
anything quietly broken", the question this project kept answering the hard
way -- Claude failing every scan for months, Perplexity's key rejected,
Google OAuth silently disconnected, the GSC sync route broken for weeks,
DataForSEO credit running out, the dev machine's C: drive hitting 0 bytes
free. Every one of those was found by accident, not because anything said so.

Each check returns one of:
    ok       -- checked, working
    warn     -- checked, degraded (nearing a threshold)
    fail     -- checked, broken
    unknown  -- not checked (not configured, or the check itself couldn't run)
"unknown" is deliberately never folded into "ok" or "fail": a check that
never ran is not the same claim as a check that passed. See CLAUDE.md /
docs/IMPROVEMENTS_PLAN.md's "missing != zero" rule, applied here to status
rather than data.
"""
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from sqlalchemy import select

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/status", tags=["status"])

# Worker name -> (warn_after_hours, fail_after_hours). Only workers with a
# genuinely periodic, unattended cadence get a staleness alarm; workers that
# run on-demand or irregularly (audit, lead_audit, fanout_tracker, snapshot)
# just report their last outcome, since "hasn't run recently" is normal for
# them, not a sign of breakage.
WORKER_STALENESS_HOURS = {
    "scheduler":   (2, 6),      # heartbeat every ~1h
    "gsc_archive": (30, 72),    # runs once a day
}

_SEVERITY = {"ok": 0, "unknown": 0, "warn": 1, "fail": 2}


def _worst(statuses) -> str:
    """Overall = the worst status seen, but "unknown" never outranks "ok" --
    an unmeasured check must not make an otherwise-healthy system look sick."""
    worst = "ok"
    for s in statuses:
        if _SEVERITY.get(s, 0) > _SEVERITY.get(worst, 0):
            worst = s
    return worst


def _aware(dt) -> Optional[datetime]:
    """SQLite round-trips a datetime as naive text; treat it as UTC (everything here is written with datetime.now(timezone.utc))."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Individual checks -- each is independent and never raises.
# ---------------------------------------------------------------------------

async def check_providers() -> dict:
    """Anthropic/OpenAI/Mistral/Google via the same free list-models probes GET /api/health?deep=true uses.

    Perplexity is deliberately excluded: it has no free connectivity probe,
    only paid completions, and the plan explicitly decided the daily recurring
    cost of a live check isn't worth it -- see the plan's Pasul 7 notes.
    """
    from api.routes.health import _run_provider_checks, _PROVIDER_PROBES

    try:
        raw = await _run_provider_checks()
    except Exception as exc:
        logger.exception("check_providers failed")
        return {p: {"status": "unknown", "detail": f"check failed: {exc}"} for p in _PROVIDER_PROBES}

    out = {}
    for provider, result in raw.items():
        if result.get("ok"):
            out[provider] = {"status": "ok", "detail": f"{result.get('response_ms')} ms"}
        else:
            out[provider] = {"status": "fail", "detail": result.get("error") or "probe failed"}
    for provider, (env_key, _probe) in _PROVIDER_PROBES.items():
        if provider not in out:
            out[provider] = {"status": "unknown", "detail": f"{env_key} not set"}
    return out


async def check_dataforseo() -> dict:
    """Account balance via GET /v3/appendix/user_data -- DataForSEO's own free (uncharged) endpoint."""
    from core.serp_client import dfs_configured, _dfs_auth

    if not dfs_configured():
        return {"status": "unknown", "detail": "DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD not set"}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.dataforseo.com/v3/appendix/user_data",
                headers={"Authorization": _dfs_auth()},
            )
        data = resp.json()
        balance = data["tasks"][0]["result"][0]["money"]["balance"]
    except Exception as exc:
        return {"status": "unknown", "detail": f"balance check failed: {exc}"}

    warn_at = float(os.getenv("DATAFORSEO_BALANCE_WARN_USD", "5"))
    status = "warn" if balance < warn_at else "ok"
    return {"status": status, "detail": f"balance ${balance:.2f}", "balance_usd": round(balance, 2)}


async def check_gsc_oauth() -> dict:
    """Google account connected and its token can actually be refreshed."""
    from api.routes.gsc._shared import _oauth_available, _get_gsc_credentials

    if not _oauth_available():
        return {"status": "unknown", "detail": "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET not set"}
    try:
        creds = await _get_gsc_credentials()
    except Exception as exc:
        return {"status": "fail", "detail": f"credential load/refresh failed: {exc}"}
    if not creds:
        return {"status": "fail", "detail": "not connected -- visit /gsc to connect"}
    return {"status": "ok", "detail": "connected"}


async def check_workers() -> dict:
    """Latest WorkerRun per worker -- survives a server restart, unlike an in-memory dict."""
    from api.models._base import AsyncSessionLocal
    from api.models.database import WorkerRun

    out = {}
    try:
        async with AsyncSessionLocal() as db:
            worker_names = [r[0] for r in (await db.execute(select(WorkerRun.worker).distinct())).all()]
            for worker in worker_names:
                latest = (await db.execute(
                    select(WorkerRun).where(WorkerRun.worker == worker)
                    .order_by(WorkerRun.finished_at.desc()).limit(1)
                )).scalar_one_or_none()
                if not latest:
                    continue
                finished_at = _aware(latest.finished_at)
                age_hours = (datetime.now(timezone.utc) - finished_at).total_seconds() / 3600
                thresholds = WORKER_STALENESS_HOURS.get(worker)
                if not latest.ok:
                    status = "fail"
                elif thresholds:
                    warn_h, fail_h = thresholds
                    status = "fail" if age_hours > fail_h else ("warn" if age_hours > warn_h else "ok")
                else:
                    status = "ok"
                out[worker] = {
                    "status": status, "ok": latest.ok,
                    "finished_at": finished_at.isoformat(), "age_hours": round(age_hours, 1),
                    "detail": latest.detail,
                }
    except Exception as exc:
        logger.exception("check_workers failed")
        return {"_error": {"status": "unknown", "detail": str(exc)}}

    for worker in WORKER_STALENESS_HOURS:
        if worker not in out:
            out[worker] = {"status": "unknown", "detail": "never recorded a run"}
    return out


def check_backup() -> dict:
    """Reads scripts/backup_db.py's last_run.json (Pasul 6) -- doesn't run a backup itself."""
    from scripts.backup_db import DEFAULT_BACKUP_ROOT

    root = os.getenv("GEO_TOOL_BACKUP_ROOT", DEFAULT_BACKUP_ROOT)
    status_path = os.path.join(root, "last_run.json")
    if not os.path.exists(status_path):
        return {"status": "unknown", "detail": "no backup has ever run"}

    try:
        with open(status_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        return {"status": "unknown", "detail": f"could not read last_run.json: {exc}"}

    if not data.get("ok"):
        return {"status": "fail", "detail": data.get("daily_detail") or "last backup failed verification"}

    try:
        ran_at = datetime.fromisoformat(data["ran_at"])
    except Exception:
        return {"status": "unknown", "detail": "last_run.json has no valid ran_at"}
    age_hours = (datetime.now(timezone.utc) - _aware(ran_at)).total_seconds() / 3600
    status = "fail" if age_hours > 48 else ("warn" if age_hours > 30 else "ok")
    return {"status": status, "detail": data.get("daily_detail"),
            "ran_at": data["ran_at"], "age_hours": round(age_hours, 1)}


def check_disk() -> dict:
    """Free space on the DB's own drive and on C: (the dev machine hit 0 bytes free once already)."""
    from api.models._base import DATABASE_PATH

    out = {}
    targets = {"db_drive": os.path.dirname(DATABASE_PATH) or ".", "c_drive": "C:\\"}
    for label, path in targets.items():
        try:
            usage = shutil.disk_usage(path)
            free_gb = usage.free / (1024 ** 3)
            status = "fail" if free_gb < 1 else ("warn" if free_gb < 5 else "ok")
            out[label] = {"status": status, "free_gb": round(free_gb, 2), "path": path}
        except Exception as exc:
            out[label] = {"status": "unknown", "detail": str(exc)}
    return out


async def run_all_checks() -> dict:
    """Every check, run concurrently where independent. Never raises."""
    import asyncio

    providers, dataforseo, gsc_oauth, workers = await asyncio.gather(
        check_providers(), check_dataforseo(), check_gsc_oauth(), check_workers(),
    )
    checks = {
        "providers": providers,
        "dataforseo": dataforseo,
        "gsc_oauth": gsc_oauth,
        "workers": workers,
        "backup": check_backup(),
        "disk": check_disk(),
    }

    all_statuses = []
    for check in checks.values():
        if "status" in check:
            all_statuses.append(check["status"])
        else:
            all_statuses.extend(v.get("status", "unknown") for v in check.values())

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "overall": _worst(all_statuses),
        "checks": checks,
    }


@router.get("")
async def get_status():
    """Everything the health panel needs, as one JSON document."""
    return await run_all_checks()
