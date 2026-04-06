"""
Website LLM Analyzer - FastAPI Web Application

A web interface for running website audits using LLMs.
Wraps the existing CLI pipeline for non-technical users.

Author: Refactored for web by Claude
Created: 2026-02-11
"""

import logging
import os
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime
import json

logger = logging.getLogger(__name__)

from typing import Optional
from fastapi import FastAPI, Request, Depends, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from dotenv import load_dotenv, dotenv_values


parent_dir = Path(__file__).parent.parent

# Load environment variables.
# We use dotenv_values first so we can explicitly overwrite any shell-level
# empty stubs (e.g. ANTHROPIC_API_KEY='') that would otherwise block
# load_dotenv even with override=True in some process/spawn configurations.
_env_path = parent_dir / ".env"
load_dotenv(_env_path, override=True)
# Belt-and-braces: directly inject any non-empty .env value that the OS env
# currently has as an empty string (common when keys are exported but blank).
for _k, _v in dotenv_values(_env_path).items():
    if _v and not os.environ.get(_k):
        os.environ[_k] = _v

# Import database and models
from api.models.database import init_db, get_db, Audit, AuditLog, AuditResult, AuditSummary, BenchmarkProject, ScheduledAudit, GeoMonitorProject, GeoMonitorScan, ContentBrief, CrossReferenceJob, AuditWeightConfig, ResultNote, UrlGuide, CostRecord, AsyncSessionLocal
from api.routes import pages_router, audits_router, results_router, health_router, compare_router, summary_router, benchmarks_router, schedules_router, geo_monitor_router, content_briefs_router, pdf_reports_router, schema_gen_router, citation_tracker_router, portfolio_router, costs_router, gap_analysis_router, content_gaps_router, action_cards_router, templates_manager_router, tracking_router, cross_reference_router, settings_router, notes_router, keyword_research_router, gsc_router, ga4_router, ads_router, insights_router, llms_txt_router, guide_router
from api.middleware.auth import BasicAuthMiddleware
from api.provider_registry import get_providers_for_ui, get_tier_presets
from sqlalchemy import select, func, desc, case
from sqlalchemy.ext.asyncio import AsyncSession
import asyncio

# Rate limiter
limiter = Limiter(key_func=get_remote_address)

# Scheduler task reference (global)
scheduler_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    global scheduler_task
    
    # Startup
    print("==> Starting Website LLM Analyzer API...")
    init_db()
    print("[OK] Database initialized")

    # Safe column migrations (ADD COLUMN IF NOT EXISTS — SQLite silently fails if already present)
    from sqlalchemy import text as _sa_text
    async with AsyncSessionLocal() as _mdb:
        for _stmt in [
            "ALTER TABLE keyword_results ADD COLUMN intent VARCHAR(30)",
            "ALTER TABLE keyword_results ADD COLUMN cluster VARCHAR(200)",
            "ALTER TABLE keyword_results ADD COLUMN priority_score FLOAT",
            "ALTER TABLE keyword_sessions ADD COLUMN source VARCHAR(20) DEFAULT 'dataforseo'",
        ]:
            try:
                await _mdb.execute(_sa_text(_stmt))
            except Exception:
                pass  # column already exists
        await _mdb.commit()
    print("[OK] Keyword research schema migration complete")

    # Reset any guides that were left in pending/running state from a previous server run
    from sqlalchemy import update as _sa_update
    async with AsyncSessionLocal() as _db:
        stale = await _db.execute(
            _sa_update(UrlGuide)
            .where(UrlGuide.status.in_(["pending", "running"]))
            .values(status="failed", error_message="Server restarted while task was running")
        )
        if stale.rowcount:
            print(f"[OK] Reset {stale.rowcount} stale guide(s) to failed")
    
    # Reset audits stuck in-progress from a previous server run
    from sqlalchemy import update as sa_update
    async with AsyncSessionLocal() as _session:
        stuck = await _session.execute(
            sa_update(Audit)
            .where(Audit.status.in_(["pending", "scraping", "converting", "analyzing", "scoring"]))
            .values(
                status="failed",
                error_message="Server restarted while audit was in progress",
            )
            .returning(Audit.id)
        )
        ids = stuck.fetchall()
        if ids:
            await _session.commit()
            logger.info(f"[startup] Reset {len(ids)} stuck audit(s): {[r[0] for r in ids]}")

    # Check for API keys
    providers = {
        "Google Gemini": bool(os.getenv("GEMINI_API_KEY")),
        "Anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "OpenAI": bool(os.getenv("OPENAI_API_KEY")),
        "Mistral": bool(os.getenv("MISTRAL_API_KEY"))
    }
    configured = [k for k, v in providers.items() if v]
    if configured:
        print(f"[OK] Configured providers: {', '.join(configured)}")
    else:
        print("[WARNING] No LLM API keys configured!")
    
    # Check auth
    from api.middleware.auth import get_auth_credentials
    if get_auth_credentials():
        print("[OK] Authentication enabled (Basic HTTP Auth)")
    else:
        print("[WARNING] Authentication disabled (set AUTH_USERNAME & AUTH_PASSWORD in .env to enable)")
    
    # Start scheduler loop
    from api.routes.schedules import check_and_run_schedules
    async def scheduler_loop():
        """Background scheduler that checks schedules every minute."""
        while True:
            try:
                # Hard timeout: if check_and_run_schedules hangs (DB lock, network
                # stall, etc.) we cancel it after 45 s so the next tick can run.
                await asyncio.wait_for(check_and_run_schedules(), timeout=45)
            except asyncio.TimeoutError:
                print("[WARNING] Scheduler: check_and_run_schedules timed out after 45 s -- skipping tick")
            except Exception as e:
                print(f"[ERROR] Scheduler error: {e}")
            await asyncio.sleep(60)  # Check every minute
    
    scheduler_task = asyncio.create_task(scheduler_loop())
    print("[OK] Scheduler started (checks every 60 seconds)")

    # Start lead audit worker (polls nrankai.com for public free-audit jobs)
    from api.workers.lead_audit_worker import lead_audit_worker_loop
    lead_worker_task = asyncio.create_task(lead_audit_worker_loop())
    if os.getenv("NRANKAI_WORKER_KEY"):
        print("[OK] Lead audit worker started (nrankai.com integration)")
    else:
        print("[INFO] Lead audit worker disabled (set NRANKAI_WORKER_KEY to enable)")

    yield

    # Shutdown lead worker
    lead_worker_task.cancel()
    try:
        await lead_worker_task
    except asyncio.CancelledError:
        pass
    
    # Shutdown
    print("Shutting down...")
    if scheduler_task:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        print("[OK] Scheduler stopped")


# Create FastAPI app
app = FastAPI(
    title="Website LLM Analyzer",
    description="Web interface for auditing websites using LLMs",
    version="1.0.0",
    lifespan=lifespan,
)

# Add rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Global exception handler: always logs to console, only exposes traceback when DEBUG=true
import traceback
from starlette.responses import PlainTextResponse
from fastapi.responses import JSONResponse

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    tb = traceback.format_exc()
    print(f"\n{'='*60}\n[ERROR] UNHANDLED EXCEPTION on {request.url}\n{'='*60}\n{tb}\n{'='*60}")
    if os.getenv('DEBUG', 'false').lower() == 'true':
        return PlainTextResponse(f"Internal Server Error:\n\n{tb}", status_code=500)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add authentication middleware (optional, only if AUTH_USERNAME/AUTH_PASSWORD set in .env)
app.add_middleware(BasicAuthMiddleware)

# Mount static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Include API routers
app.include_router(audits_router)
app.include_router(results_router)
app.include_router(health_router)
app.include_router(compare_router)
app.include_router(summary_router)
app.include_router(benchmarks_router)
app.include_router(schedules_router)
app.include_router(geo_monitor_router)
app.include_router(content_briefs_router)
app.include_router(pdf_reports_router)
app.include_router(schema_gen_router)
app.include_router(citation_tracker_router)
app.include_router(portfolio_router)
app.include_router(costs_router)
app.include_router(gap_analysis_router)
app.include_router(content_gaps_router)
app.include_router(action_cards_router)
app.include_router(templates_manager_router)
app.include_router(tracking_router)
app.include_router(cross_reference_router)
app.include_router(settings_router)
app.include_router(notes_router)
app.include_router(keyword_research_router)
app.include_router(gsc_router)
app.include_router(ga4_router)
app.include_router(ads_router)
app.include_router(insights_router)
app.include_router(llms_txt_router)
app.include_router(guide_router)
app.include_router(pages_router)
