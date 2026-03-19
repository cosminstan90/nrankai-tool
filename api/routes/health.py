"""
Health check and system status endpoints.

GET /api/health
    Lightweight ping: verifies DB connectivity and returns basic system info.
    Response includes db_response_ms so monitoring tools can track latency.

GET /api/health?deep=true
    Deep check: additionally tests each configured LLM provider by making a
    minimal tokenisation / model-list call.  Adds provider_checks dict with
    per-provider {ok, response_ms, error} entries.
    This call may take 1-5 seconds if providers are slow to respond.
"""

import os
import time
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import Audit, get_db
from api.models.schemas import HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

# Current application version
APP_VERSION = "2.1.0"


# ---------------------------------------------------------------------------
# Provider connectivity probes
# ---------------------------------------------------------------------------

async def _probe_anthropic(api_key: str) -> dict:
    """Minimal Anthropic probe — counts available models (no inference cost)."""
    start = time.perf_counter()
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        # model_list is cheap — just an HTTP GET to the models endpoint
        await asyncio.wait_for(client.models.list(), timeout=5.0)
        return {"ok": True, "response_ms": round((time.perf_counter() - start) * 1000, 1)}
    except asyncio.TimeoutError:
        return {"ok": False, "response_ms": None, "error": "timeout (5s)"}
    except Exception as exc:
        return {"ok": False, "response_ms": round((time.perf_counter() - start) * 1000, 1),
                "error": str(exc)[:120]}


async def _probe_openai(api_key: str) -> dict:
    """Minimal OpenAI probe — list models endpoint."""
    start = time.perf_counter()
    try:
        import openai
        client = openai.AsyncOpenAI(api_key=api_key)
        await asyncio.wait_for(client.models.list(), timeout=5.0)
        return {"ok": True, "response_ms": round((time.perf_counter() - start) * 1000, 1)}
    except asyncio.TimeoutError:
        return {"ok": False, "response_ms": None, "error": "timeout (5s)"}
    except Exception as exc:
        return {"ok": False, "response_ms": round((time.perf_counter() - start) * 1000, 1),
                "error": str(exc)[:120]}


async def _probe_mistral(api_key: str) -> dict:
    """Minimal Mistral probe — list models endpoint."""
    start = time.perf_counter()
    try:
        import aiohttp
        headers = {"Authorization": f"Bearer {api_key}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.mistral.ai/v1/models",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                resp.raise_for_status()
        return {"ok": True, "response_ms": round((time.perf_counter() - start) * 1000, 1)}
    except asyncio.TimeoutError:
        return {"ok": False, "response_ms": None, "error": "timeout (5s)"}
    except Exception as exc:
        return {"ok": False, "response_ms": round((time.perf_counter() - start) * 1000, 1),
                "error": str(exc)[:120]}


async def _probe_google(api_key: str) -> dict:
    """Minimal Google Gemini probe — list models."""
    start = time.perf_counter()
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                resp.raise_for_status()
        return {"ok": True, "response_ms": round((time.perf_counter() - start) * 1000, 1)}
    except asyncio.TimeoutError:
        return {"ok": False, "response_ms": None, "error": "timeout (5s)"}
    except Exception as exc:
        return {"ok": False, "response_ms": round((time.perf_counter() - start) * 1000, 1),
                "error": str(exc)[:120]}


_PROVIDER_PROBES = {
    "anthropic": ("ANTHROPIC_API_KEY", _probe_anthropic),
    "openai":    ("OPENAI_API_KEY",    _probe_openai),
    "mistral":   ("MISTRAL_API_KEY",   _probe_mistral),
    "google":    ("GEMINI_API_KEY",    _probe_google),
}


async def _run_provider_checks() -> dict:
    """Run all configured provider probes concurrently."""
    tasks = {}
    for provider, (env_key, probe_fn) in _PROVIDER_PROBES.items():
        api_key = os.getenv(env_key)
        if api_key:
            tasks[provider] = asyncio.create_task(probe_fn(api_key))

    results = {}
    for provider, task in tasks.items():
        try:
            results[provider] = await task
        except Exception as exc:
            results[provider] = {"ok": False, "error": str(exc)[:120]}

    return results


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@router.get("/api/health", response_model=HealthResponse)
async def health_check(
    deep: bool = Query(False, description="If true, test each configured LLM provider"),
    db: AsyncSession = Depends(get_db),
):
    """
    Health check endpoint.

    - **Fast path** (default): checks DB connectivity and returns active audit count.
    - **Deep path** (`?deep=true`): additionally probes each configured LLM provider.
      May add up to 5 s latency — intended for monitoring dashboards, not hot paths.
    """

    # ---- Database ping with timing ----------------------------------------
    db_status = "connected"
    db_response_ms: Optional[float] = None
    try:
        t0 = time.perf_counter()
        # Use a trivial SELECT 1 for the fastest possible round-trip
        await db.execute(text("SELECT 1"))
        db_response_ms = round((time.perf_counter() - t0) * 1000, 2)
    except Exception as exc:
        logger.warning("Health check DB ping failed: %s", exc)
        db_status = f"error: {str(exc)[:200]}"

    # ---- Provider configuration status (key presence only) ----------------
    providers = {
        "gemini":     bool(os.getenv("GEMINI_API_KEY")),
        "anthropic":  bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai":     bool(os.getenv("OPENAI_API_KEY")),
        "mistral":    bool(os.getenv("MISTRAL_API_KEY")),
        "perplexity": bool(os.getenv("PERPLEXITY_API_KEY")),
    }

    # ---- Active audit count -----------------------------------------------
    try:
        active_result = await db.execute(
            select(func.count(Audit.id)).where(
                Audit.status.in_(["pending", "scraping", "converting", "analyzing"])
            )
        )
        active_audits = active_result.scalar() or 0
    except Exception:
        active_audits = 0

    # ---- Optional deep provider checks ------------------------------------
    provider_checks: Optional[dict] = None
    if deep:
        provider_checks = await _run_provider_checks()

    # ---- Overall status determination -------------------------------------
    if db_status != "connected":
        overall = "unhealthy"
    elif deep and provider_checks and not any(v.get("ok") for v in provider_checks.values()):
        overall = "degraded"  # DB up but no providers reachable
    else:
        overall = "healthy"

    return HealthResponse(
        status=overall,
        version=APP_VERSION,
        database=db_status,
        db_response_ms=db_response_ms,
        providers=providers,
        active_audits=active_audits,
        provider_checks=provider_checks,
    )


# ---------------------------------------------------------------------------
# Audit types convenience endpoint
# ---------------------------------------------------------------------------

@router.get("/api/audit-types")
async def get_audit_types():
    """Get list of available audit types (reads YAML prompt definitions)."""
    from prompt_loader import list_available_audits
    return list_available_audits()
