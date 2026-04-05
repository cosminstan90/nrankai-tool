"""
Website LLM Analyzer - FastAPI Web Application

A web interface for running website audits using LLMs.
Wraps the existing CLI pipeline for non-technical users.

Author: Refactored for web by Claude
Created: 2026-02-11
"""

import os
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime
import json

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

# Add parent directory to path for imports
parent_dir = Path(__file__).parent.parent
sys.path.insert(0, str(parent_dir))

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
from api.routes import audits_router, results_router, health_router, compare_router, summary_router, benchmarks_router, schedules_router, geo_monitor_router, content_briefs_router, pdf_reports_router, schema_gen_router, citation_tracker_router, portfolio_router, costs_router, gap_analysis_router, content_gaps_router, action_cards_router, templates_manager_router, tracking_router, cross_reference_router, settings_router, notes_router, keyword_research_router, gsc_router, ga4_router, ads_router, insights_router, llms_txt_router, guide_router
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

    # Reset any audits stuck in an active state from a previous server run
    async with AsyncSessionLocal() as _db:
        stale_audits = await _db.execute(
            _sa_update(Audit)
            .where(Audit.status.in_(["pending", "scraping", "converting", "analyzing"]))
            .values(
                status="failed",
                error_message="Server restarted while audit was running — use Retry to resume (existing files will be reused)"
            )
        )
        await _db.commit()
        if stale_audits.rowcount:
            print(f"[OK] Reset {stale_audits.rowcount} stale audit(s) to failed — retry to resume")
    
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

# Setup templates
templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=templates_dir)

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


# ============================================================================
# TEMPLATE ROUTES (Server-rendered HTML)
# ============================================================================

@app.get("/presentation", response_class=HTMLResponse)
async def presentation_page():
    """Slide deck for the presentation."""
    return FileResponse("presentation.html")

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    type: Optional[str] = Query(None, description="Filter by audit type")
):
    """Dashboard page showing websites with aggregated audit data."""
    # Build base query - optionally filter by audit type
    stats_base = select(func.count(Audit.id)).where(~Audit.audit_type.startswith('SINGLE_'))

    if type:
        stats_base = stats_base.where(Audit.audit_type == type)

    # Websites grouped query (replaces flat audit list)
    websites_q = (
        select(
            Audit.website,
            func.count(Audit.id).label("audit_count"),
            func.sum(Audit.pages_analyzed).label("total_pages"),
            func.avg(Audit.average_score).label("avg_score"),
            func.max(Audit.created_at).label("last_run"),
            func.sum(case((Audit.status.in_(["pending", "scraping", "converting", "analyzing"]), 1), else_=0)).label("running_count"),
            func.sum(case((Audit.status == "failed", 1), else_=0)).label("failed_count"),
        )
        .where(~Audit.audit_type.startswith("SINGLE_"))
        .group_by(Audit.website)
        .order_by(func.max(Audit.created_at).desc())
        .limit(50)
    )
    websites_rows = (await db.execute(websites_q)).fetchall()

    # Costs per website
    costs_q = (
        select(CostRecord.website, func.sum(CostRecord.estimated_cost_usd).label("total_cost"))
        .where(CostRecord.website.isnot(None))
        .group_by(CostRecord.website)
    )
    costs_by_website = {row[0]: row[1] for row in (await db.execute(costs_q)).fetchall()}

    websites = [
        {
            "website": row.website,
            "audit_count": row.audit_count,
            "total_pages": int(row.total_pages or 0),
            "avg_score": round(row.avg_score, 1) if row.avg_score else None,
            "last_run": row.last_run,
            "running_count": row.running_count or 0,
            "failed_count": row.failed_count or 0,
            "total_cost_usd": costs_by_website.get(row.website, 0.0),
        }
        for row in websites_rows
    ]

    # Get recent single page audits
    single_audits_query = select(Audit).where(Audit.audit_type.startswith('SINGLE_'))
    if type:
        single_audits_query = single_audits_query.where(Audit.audit_type == type)
    
    if request.query_params.get('all_single') == '1':
        single_result = await db.execute(
            single_audits_query.order_by(desc(Audit.created_at)).limit(100)
        )
    else:
        single_result = await db.execute(
            single_audits_query.order_by(desc(Audit.created_at)).limit(10)
        )
    single_audits = single_result.scalars().all()

    # Get stats (scoped to filter)
    total_result = await db.execute(stats_base)
    total_audits = total_result.scalar()

    running_filter = select(func.count(Audit.id)).where(
        Audit.status.in_(["pending", "scraping", "converting", "analyzing"])
    ).where(~Audit.audit_type.startswith('SINGLE_'))
    
    completed_filter = select(func.count(Audit.id)).where(
        Audit.status == "completed"
    ).where(~Audit.audit_type.startswith('SINGLE_'))
    
    pages_filter = select(func.sum(Audit.pages_analyzed)).where(
        ~Audit.audit_type.startswith('SINGLE_')
    )
    
    avg_filter = select(func.avg(Audit.average_score)).where(
        Audit.average_score.isnot(None)
    ).where(~Audit.audit_type.startswith('SINGLE_'))

    if type:
        running_filter = running_filter.where(Audit.audit_type == type)
        completed_filter = completed_filter.where(Audit.audit_type == type)
        pages_filter = pages_filter.where(Audit.audit_type == type)
        avg_filter = avg_filter.where(Audit.audit_type == type)

    running_result = await db.execute(running_filter)
    running_audits = running_result.scalar()

    completed_result = await db.execute(completed_filter)
    completed_audits = completed_result.scalar()

    pages_result = await db.execute(pages_filter)
    total_pages = pages_result.scalar() or 0

    avg_result = await db.execute(avg_filter)
    average_score = avg_result.scalar()

    # Check configured providers
    providers = {
        "google": bool(os.getenv("GEMINI_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "mistral": bool(os.getenv("MISTRAL_API_KEY"))
    }
    providers_ui = get_providers_for_ui()

    # ── Sites Needing Attention: lowest avg-scoring sites (score < 70) ────────
    attention_q = (
        select(Audit.website, func.avg(Audit.average_score).label("avg_score"))
        .where(
            Audit.status == "completed",
            Audit.average_score.isnot(None),
            ~Audit.audit_type.startswith("SINGLE_"),
        )
        .group_by(Audit.website)
        .having(func.avg(Audit.average_score) < 70)
        .order_by(func.avg(Audit.average_score))
        .limit(8)
    )
    attention_rows = (await db.execute(attention_q)).fetchall()
    sites_needing_attention = [
        {"website": row[0], "avg_score": round(row[1], 1)}
        for row in attention_rows
    ]

    return templates.TemplateResponse("index.html", {
        "request": request,
        "websites": websites,
        "single_audits": single_audits,
        "all_single": request.query_params.get("all_single") == "1",
        "active_type": type,
        "stats": {
            "total_audits": total_audits,
            "running_audits": running_audits,
            "completed_audits": completed_audits,
            "total_pages": total_pages,
            "average_score": round(average_score, 1) if average_score else None
        },
        "providers": providers,
        "providers_ui": providers_ui,
        "tier_presets": get_tier_presets(),
        "perplexity_available": bool(os.getenv("PERPLEXITY_API_KEY")),
        "sites_needing_attention": sites_needing_attention,
    })


@app.get("/new", response_class=HTMLResponse)
async def new_audit_form(request: Request):
    """New audit form page."""
    from prompt_loader import list_available_audits
    
    audit_types = list_available_audits()
    providers_ui = get_providers_for_ui()
    
    # Check configured providers
    providers = {
        "google": bool(os.getenv("GEMINI_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "mistral": bool(os.getenv("MISTRAL_API_KEY"))
    }
    
    # Check Perplexity availability
    perplexity_available = bool(os.getenv("PERPLEXITY_API_KEY"))
    
    return templates.TemplateResponse("new_audit.html", {
        "request": request,
        "audit_types": audit_types,
        "providers": providers,
        "providers_ui": providers_ui,
        "tier_presets": get_tier_presets(),
        "perplexity_available": perplexity_available
    })


@app.get("/audits/{audit_id}", response_class=HTMLResponse)
async def audit_detail(
    request: Request,
    audit_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Audit detail page with real-time updates."""
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()
    
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    
    # Get results summary if completed
    results_summary = None
    if audit.status == "completed":
        # Get score distribution
        dist_query = select(
            AuditResult.classification,
            func.count(AuditResult.id)
        ).where(
            AuditResult.audit_id == audit_id
        ).group_by(AuditResult.classification)
        
        dist_result = await db.execute(dist_query)
        score_distribution = dict(dist_result.fetchall())
        
        # Get top and bottom results
        top_query = select(AuditResult).where(
            AuditResult.audit_id == audit_id
        ).order_by(desc(AuditResult.score)).limit(5)
        top_result = await db.execute(top_query)
        top_results = top_result.scalars().all()
        
        bottom_query = select(AuditResult).where(
            AuditResult.audit_id == audit_id,
            AuditResult.score.isnot(None)
        ).order_by(AuditResult.score).limit(5)
        bottom_result = await db.execute(bottom_query)
        bottom_results = bottom_result.scalars().all()
        
        results_summary = {
            "distribution": score_distribution,
            "top_results": top_results,
            "bottom_results": bottom_results
        }
        
    # Pre-load logs for completed/failed audits (SSE won't fire for these)
    audit_logs = []
    if audit.status in ("completed", "failed"):
        logs_query = select(AuditLog).where(
            AuditLog.audit_id == audit_id
        ).order_by(AuditLog.created_at).limit(500)
        logs_result = await db.execute(logs_query)
        audit_logs = logs_result.scalars().all()

    if audit.audit_type.startswith('SINGLE_'):
        # Get all results for this single page audit
        results_query = select(AuditResult).where(AuditResult.audit_id == audit_id)
        res = await db.execute(results_query)
        all_results = res.scalars().all()
        
        single_results_dict = {}
        for r in all_results:
            a_type = r.filename.replace('.json', '')
            try:
                single_results_dict[a_type] = json.loads(r.result_json)
            except Exception as e:
                import traceback
                print(f"Failed to parse JSON for {a_type} in {audit_id}:\n{traceback.format_exc()}")
                single_results_dict[a_type] = {"error": "Invalid JSON"}
                
        return templates.TemplateResponse("single_audit_detail.html", {
            "request": request,
            "audit": audit,
            "audit_logs": audit_logs,
            "single_results_dict": single_results_dict
        })

    return templates.TemplateResponse("audit_detail.html", {
        "request": request,
        "audit": audit,
        "results_summary": results_summary,
        "audit_logs": audit_logs,
    })


@app.get("/audits/{audit_id}/results", response_class=HTMLResponse)
async def audit_results_page(
    request: Request,
    audit_id: str,
    page: int = 1,
    min_score: Optional[int] = Query(None, ge=0, le=100),
    max_score: Optional[int] = Query(None, ge=0, le=100),
    classification: Optional[str] = Query(None),
    url_search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """Full results table page with optional score/classification/URL filters."""
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()

    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    # ── Score distribution (always unfiltered, for the summary chart) ─────────
    dist_q = (
        select(AuditResult.score, func.count(AuditResult.id))
        .where(AuditResult.audit_id == audit_id, AuditResult.score.isnot(None))
        .group_by(AuditResult.score)
    )
    dist_rows = dict((await db.execute(dist_q)).fetchall())
    score_distribution = {
        "0-49":   sum(v for k, v in dist_rows.items() if 0  <= k <= 49),
        "50-69":  sum(v for k, v in dist_rows.items() if 50 <= k <= 69),
        "70-84":  sum(v for k, v in dist_rows.items() if 70 <= k <= 84),
        "85-100": sum(v for k, v in dist_rows.items() if 85 <= k <= 100),
    }

    # ── Build filter conditions ───────────────────────────────────────────────
    results_query = select(AuditResult).where(AuditResult.audit_id == audit_id)
    count_query   = select(func.count(AuditResult.id)).where(AuditResult.audit_id == audit_id)

    if min_score is not None:
        results_query = results_query.where(AuditResult.score >= min_score)
        count_query   = count_query.where(AuditResult.score >= min_score)
    if max_score is not None:
        results_query = results_query.where(AuditResult.score <= max_score)
        count_query   = count_query.where(AuditResult.score <= max_score)
    if classification:
        results_query = results_query.where(AuditResult.classification == classification)
        count_query   = count_query.where(AuditResult.classification == classification)
    if url_search:
        results_query = results_query.where(AuditResult.page_url.ilike(f"%{url_search}%"))
        count_query   = count_query.where(AuditResult.page_url.ilike(f"%{url_search}%"))

    # ── Pagination ────────────────────────────────────────────────────────────
    page_size = 50
    offset = (page - 1) * page_size

    results_query = results_query.order_by(desc(AuditResult.score)).offset(offset).limit(page_size)
    results_result = await db.execute(results_query)
    results = results_result.scalars().all()

    count_result = await db.execute(count_query)
    total = count_result.scalar()
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    # ── Build filter query-string prefix for pagination links ─────────────────
    from urllib.parse import quote as _quote
    _qs_parts = []
    if min_score is not None:
        _qs_parts.append(f"min_score={min_score}")
    if max_score is not None:
        _qs_parts.append(f"max_score={max_score}")
    if classification:
        _qs_parts.append(f"classification={_quote(classification)}")
    if url_search:
        _qs_parts.append(f"url_search={_quote(url_search)}")
    filter_qs = ("&".join(_qs_parts) + "&") if _qs_parts else ""

    return templates.TemplateResponse("results.html", {
        "request": request,
        "audit": audit,
        "results": results,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "score_distribution": score_distribution,
        "min_score": min_score if min_score is not None else "",
        "max_score": max_score if max_score is not None else "",
        "classification": classification or "",
        "url_search": url_search or "",
        "filter_qs": filter_qs,
    })


# ============================================================================
# COMPOSITE SCORE WEIGHTS — used by per-URL and per-site views
# ============================================================================
_COMPOSITE_WEIGHTS = {
    'SEO_AUDIT': 0.20,
    'GEO_AUDIT': 0.15,
    'CONTENT_QUALITY': 0.12,
    'TECHNICAL_SEO': 0.12,
    'UX_CONTENT': 0.10,
    'ACCESSIBILITY_AUDIT': 0.08,
    'BRAND_VOICE': 0.07,
    'LEGAL_GDPR': 0.06,
    'INTERNAL_LINKING': 0.05,
    'READABILITY_AUDIT': 0.05,
    'COMPETITOR_ANALYSIS': 0.04,
    'CONTENT_FRESHNESS': 0.04,
    'AI_OVERVIEW_OPTIMIZATION': 0.04,
    'SPELLING_GRAMMAR': 0.03,
    'TRANSLATION_QUALITY': 0.03,
    'LOCAL_SEO': 0.03,
    'SECURITY_CONTENT_AUDIT': 0.03,
    'E_COMMERCE': 0.03,
}

_AUDIT_TYPE_LABELS = {
    'SEO_AUDIT': 'SEO', 'GEO_AUDIT': 'GEO', 'CONTENT_QUALITY': 'Content Quality',
    'TECHNICAL_SEO': 'Technical SEO', 'UX_CONTENT': 'UX Content',
    'ACCESSIBILITY_AUDIT': 'Accessibility', 'BRAND_VOICE': 'Brand Voice',
    'LEGAL_GDPR': 'Legal / GDPR', 'INTERNAL_LINKING': 'Internal Linking',
    'READABILITY_AUDIT': 'Readability', 'COMPETITOR_ANALYSIS': 'Competitors',
    'CONTENT_FRESHNESS': 'Content Freshness', 'AI_OVERVIEW_OPTIMIZATION': 'AI Overview',
    'SPELLING_GRAMMAR': 'Spelling & Grammar', 'TRANSLATION_QUALITY': 'Translation',
    'LOCAL_SEO': 'Local SEO', 'SECURITY_CONTENT_AUDIT': 'Security Content',
    'E_COMMERCE': 'E-Commerce',
}

async def _load_weights(db: AsyncSession) -> dict:
    """Return effective weight dict — DB rows override hardcoded defaults.

    Falls back to _COMPOSITE_WEIGHTS when the audit_weight_configs table
    is empty (fresh install or after a reset).
    """
    result = await db.execute(select(AuditWeightConfig))
    rows = result.scalars().all()
    if not rows:
        return _COMPOSITE_WEIGHTS
    # Merge: start from defaults, overlay DB values
    merged = dict(_COMPOSITE_WEIGHTS)
    for row in rows:
        merged[row.audit_type] = row.weight
    return merged


def _compute_composite(scored_map: dict, weights: Optional[dict] = None) -> Optional[int]:
    """Compute weighted composite score from {audit_type: score} dict.

    Args:
        scored_map: Mapping of audit_type → score (None scores are skipped).
        weights: Override weight dict; defaults to _COMPOSITE_WEIGHTS.
    """
    w_map = weights if weights is not None else _COMPOSITE_WEIGHTS
    weighted_sum = 0.0
    weight_sum = 0.0
    for atype, score in scored_map.items():
        if score is not None:
            w = w_map.get(atype.upper(), 0.02)
            weighted_sum += score * w
            weight_sum += w
    if weight_sum == 0:
        return None
    return round(weighted_sum / weight_sum)


@app.get("/pages", response_class=HTMLResponse)
async def page_view(
    request: Request,
    url: str = Query(..., description="Page URL to view across all audit types"),
    db: AsyncSession = Depends(get_db)
):
    """Unified per-URL view — shows every audit type result for a single page."""
    import json as _json
    from urllib.parse import unquote

    decoded_url = unquote(url)

    # Fetch all AuditResults for this URL, joined with Audit, most-recent-per-type
    stmt = (
        select(AuditResult, Audit)
        .join(Audit, AuditResult.audit_id == Audit.id)
        .where(AuditResult.page_url == decoded_url)
        .where(Audit.status == "completed")
        .order_by(Audit.audit_type, desc(Audit.created_at))
    )
    rows = (await db.execute(stmt)).fetchall()

    # Deduplicate: keep most-recent per audit_type
    seen: set = set()
    audit_results = []
    for ar, audit in rows:
        if audit.audit_type not in seen:
            seen.add(audit.audit_type)
            try:
                rdata = _json.loads(ar.result_json) if ar.result_json else {}
            except Exception:
                rdata = {}
            raw_issues = rdata.get("issues", [])
            audit_results.append({
                "audit_type": audit.audit_type,
                "audit_type_label": _AUDIT_TYPE_LABELS.get(audit.audit_type, audit.audit_type),
                "audit_id": audit.id,
                "result_id": ar.id,
                "score": ar.score,
                "classification": ar.classification,
                "provider": audit.provider,
                "model": audit.model,
                "completed_at": audit.completed_at.strftime("%Y-%m-%d") if audit.completed_at else None,
                "top_issues": raw_issues[:3],
                "website": audit.website,
            })

    if not audit_results:
        raise HTTPException(status_code=404, detail=f"No completed audit results found for: {decoded_url}")

    # Load effective weights (DB override or hardcoded defaults)
    weights = await _load_weights(db)

    # Sort by weight descending so most important audits appear first
    audit_results.sort(key=lambda x: weights.get(x["audit_type"], 0), reverse=True)

    # Composite score
    composite = _compute_composite({r["audit_type"]: r["score"] for r in audit_results}, weights)

    # Chart data (radar)
    chart_labels = [r["audit_type_label"] for r in audit_results if r["score"] is not None]
    chart_scores = [r["score"] for r in audit_results if r["score"] is not None]

    # ── Score history per audit_type for this specific URL (oldest → newest) ─
    # Reuses the same pattern as site_health() but scoped to one page_url.
    # Each history entry is a single score (no GROUP BY / avg needed).
    history_data: dict = {}
    for atype in seen:
        hist_stmt = (
            select(Audit.completed_at, AuditResult.score)
            .join(Audit, AuditResult.audit_id == Audit.id)
            .where(
                AuditResult.page_url == decoded_url,
                Audit.audit_type == atype,
                Audit.status == "completed",
                AuditResult.score.isnot(None),
            )
            .order_by(desc(Audit.completed_at))
            .limit(10)
        )
        hist_rows = (await db.execute(hist_stmt)).fetchall()
        if len(hist_rows) >= 2:
            history_data[atype] = [
                {
                    "date": row[0].strftime("%Y-%m-%d") if row[0] else None,
                    "score": row[1],
                    "label": _AUDIT_TYPE_LABELS.get(atype, atype),
                }
                for row in reversed(hist_rows)   # oldest first for chart
            ]

    return templates.TemplateResponse("page_view.html", {
        "request": request,
        "page_url": decoded_url,
        "website": audit_results[0]["website"] if audit_results else "",
        "audit_results": audit_results,
        "composite_score": composite,
        "chart_labels": _json.dumps(chart_labels),
        "chart_scores": _json.dumps(chart_scores),
        "history_data": _json.dumps(history_data),
        "total_audits": len(audit_results),
    })


@app.get("/sites/{website:path}", response_class=HTMLResponse)
async def site_health(
    request: Request,
    website: str,
    db: AsyncSession = Depends(get_db)
):
    """Site health overview — composite score + latest audit per type for a whole domain."""
    import json as _json

    # For each audit_type, get the most recent completed audit for this website
    stmt = (
        select(Audit)
        .where(Audit.website == website, Audit.status == "completed")
        .order_by(Audit.audit_type, desc(Audit.created_at))
    )
    all_audits = (await db.execute(stmt)).scalars().all()

    # Deduplicate by audit_type — most recent per type
    seen: set = set()
    latest_by_type: dict = {}
    for audit in all_audits:
        if audit.audit_type not in seen:
            seen.add(audit.audit_type)
            latest_by_type[audit.audit_type] = audit

    if not latest_by_type:
        raise HTTPException(status_code=404, detail=f"No completed audits found for: {website}")

    # Load effective weights (DB override or hardcoded defaults)
    weights = await _load_weights(db)

    # Costs per audit_id (single query for all audits of this website)
    all_audit_ids = [a.id for a in latest_by_type.values()]
    costs_q = (
        select(CostRecord.audit_id, func.sum(CostRecord.estimated_cost_usd).label("cost"))
        .where(CostRecord.audit_id.in_(all_audit_ids))
        .group_by(CostRecord.audit_id)
    )
    costs_by_audit = {row[0]: row[1] for row in (await db.execute(costs_q)).fetchall()}
    total_cost_usd = sum(costs_by_audit.values())

    # For each latest audit, get the site-average score from AuditResult
    audit_summaries = []
    for atype, audit in sorted(latest_by_type.items(),
                                key=lambda x: weights.get(x[0], 0), reverse=True):
        avg_q = select(func.avg(AuditResult.score)).where(
            AuditResult.audit_id == audit.id,
            AuditResult.score.isnot(None)
        )
        avg_score = (await db.execute(avg_q)).scalar()
        avg_score = round(avg_score) if avg_score else None

        count_q = select(func.count(AuditResult.id)).where(AuditResult.audit_id == audit.id)
        page_count = (await db.execute(count_q)).scalar() or 0

        cost = costs_by_audit.get(audit.id, 0.0)
        audit_summaries.append({
            "audit_type": atype,
            "audit_type_label": _AUDIT_TYPE_LABELS.get(atype, atype),
            "audit_id": audit.id,
            "avg_score": avg_score,
            "page_count": page_count,
            "provider": audit.provider,
            "model": audit.model,
            "completed_at": audit.completed_at.strftime("%Y-%m-%d") if audit.completed_at else None,
            "weight_pct": round(weights.get(atype, 0.02) * 100),
            "cost_usd": cost,
        })

    # Composite health score
    composite = _compute_composite({s["audit_type"]: s["avg_score"] for s in audit_summaries}, weights)

    # Chart data (radar)
    chart_labels = [s["audit_type_label"] for s in audit_summaries if s["avg_score"] is not None]
    chart_scores = [s["avg_score"] for s in audit_summaries if s["avg_score"] is not None]

    # ── Score history (last 10 runs per audit type, oldest→newest) ────────────
    # Only include types that have ≥2 historical runs (needed for a trend line)
    history_data: dict = {}
    for atype in latest_by_type:
        hist_stmt = (
            select(Audit.completed_at, func.avg(AuditResult.score))
            .join(AuditResult, AuditResult.audit_id == Audit.id)
            .where(
                Audit.website == website,
                Audit.audit_type == atype,
                Audit.status == "completed",
                AuditResult.score.isnot(None),
            )
            .group_by(Audit.id, Audit.completed_at)
            .order_by(desc(Audit.completed_at))
            .limit(10)
        )
        hist_rows = (await db.execute(hist_stmt)).fetchall()
        if len(hist_rows) >= 2:
            history_data[atype] = [
                {
                    "date": row[0].strftime("%Y-%m-%d") if row[0] else None,
                    "score": round(row[1]) if row[1] else None,
                    "label": _AUDIT_TYPE_LABELS.get(atype, atype),
                }
                for row in reversed(hist_rows)  # oldest first for chart
            ]

    # ── Worst 5 pages per audit type (for drill-down section) ────────────────
    worst_pages_by_type: dict = {}
    for atype, audit in latest_by_type.items():
        worst_q = (
            select(AuditResult.page_url, AuditResult.score, AuditResult.classification)
            .where(AuditResult.audit_id == audit.id, AuditResult.score.isnot(None))
            .order_by(AuditResult.score)
            .limit(5)
        )
        worst_rows = (await db.execute(worst_q)).fetchall()
        if worst_rows:
            worst_pages_by_type[atype] = [
                {
                    "url": row[0],
                    "score": int(row[1]),
                    "classification": row[2] or "",
                    "audit_id": audit.id,
                }
                for row in worst_rows
            ]

    return templates.TemplateResponse("site_health.html", {
        "request": request,
        "website": website,
        "audit_summaries": audit_summaries,
        "composite_score": composite,
        "chart_labels": _json.dumps(chart_labels),
        "chart_scores": _json.dumps(chart_scores),
        "history_data": _json.dumps(history_data),
        "total_audit_types": len(audit_summaries),
        "total_audits_run": len(all_audits),
        "worst_pages_by_type": worst_pages_by_type,
        "total_cost_usd": total_cost_usd,
    })


@app.get("/sites/{website:path}/export/csv")
async def site_health_csv(website: str, db: AsyncSession = Depends(get_db)):
    """Export site health summary as a CSV file."""
    import csv as _csv
    import io as _io
    from fastapi.responses import StreamingResponse as _SR

    stmt = (
        select(Audit)
        .where(Audit.website == website, Audit.status == "completed")
        .order_by(Audit.audit_type, desc(Audit.created_at))
    )
    all_audits = (await db.execute(stmt)).scalars().all()

    seen: set = set()
    latest_by_type: dict = {}
    for audit in all_audits:
        if audit.audit_type not in seen:
            seen.add(audit.audit_type)
            latest_by_type[audit.audit_type] = audit

    if not latest_by_type:
        raise HTTPException(status_code=404, detail=f"No completed audits for: {website}")

    weights = await _load_weights(db)

    output = _io.StringIO()
    writer = _csv.writer(output)
    writer.writerow(["Audit Type", "Label", "Avg Score", "Pages Analyzed", "Weight %", "Completed", "Provider", "Model"])

    for atype, audit in sorted(latest_by_type.items(), key=lambda x: weights.get(x[0], 0), reverse=True):
        avg_q = select(func.avg(AuditResult.score)).where(
            AuditResult.audit_id == audit.id, AuditResult.score.isnot(None)
        )
        avg_score = (await db.execute(avg_q)).scalar()
        avg_score = round(avg_score, 1) if avg_score else ""

        count_q = select(func.count(AuditResult.id)).where(AuditResult.audit_id == audit.id)
        page_count = (await db.execute(count_q)).scalar() or 0

        writer.writerow([
            atype,
            _AUDIT_TYPE_LABELS.get(atype, atype),
            avg_score,
            page_count,
            round(weights.get(atype, 0.02) * 100),
            audit.completed_at.strftime("%Y-%m-%d") if audit.completed_at else "",
            audit.provider or "",
            audit.model or "",
        ])

    content = output.getvalue()
    safe_site = website.replace("https://", "").replace("http://", "").replace("/", "_")
    filename = f"site_health_{safe_site}.csv"

    return _SR(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/compare", response_class=HTMLResponse)
async def compare_page(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Compare audits page."""
    # Get all completed audits for the selector
    result = await db.execute(
        select(Audit).where(Audit.status == "completed").order_by(desc(Audit.created_at))
    )
    audits = result.scalars().all()
    
    return templates.TemplateResponse("compare.html", {
        "request": request,
        "audits": [a.to_dict() for a in audits]
    })


@app.get("/benchmarks", response_class=HTMLResponse)
async def benchmarks_page(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Benchmarks page for competitor analysis."""
    # Get all completed audits for dropdowns
    result = await db.execute(
        select(Audit).where(Audit.status == "completed").order_by(desc(Audit.created_at))
    )
    audits = result.scalars().all()
    
    return templates.TemplateResponse("benchmarks.html", {
        "request": request,
        "audits": [a.to_dict() for a in audits]
    })


@app.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request):
    """Scheduled audits page with history tracking."""
    from prompt_loader import list_available_audits
    
    audit_types = list_available_audits()
    
    # Provider configurations
    providers = []
    
    if os.getenv("ANTHROPIC_API_KEY"):
        providers.append({
            "name": "Anthropic",
            "models": ["claude-sonnet-4-20250514", "claude-opus-4-5-20251101", "claude-haiku-4-5-20251001"]
        })
    
    if os.getenv("OPENAI_API_KEY"):
        providers.append({
            "name": "OpenAI",
            "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"]
        })
    
    if os.getenv("MISTRAL_API_KEY"):
        providers.append({
            "name": "Mistral",
            "models": ["mistral-large-latest", "mistral-small-latest"]
        })
    
    return templates.TemplateResponse("schedules.html", {
        "request": request,
        "audit_types": audit_types,
        "providers": providers
    })


@app.get("/geo-monitor", response_class=HTMLResponse)
async def geo_monitor_page(request: Request):
    """GEO Visibility Monitor page - track AI visibility of websites."""
    providers = {
        "chatgpt": bool(os.getenv("OPENAI_API_KEY")),
        "claude": bool(os.getenv("ANTHROPIC_API_KEY")),
        "perplexity": bool(os.getenv("PERPLEXITY_API_KEY"))
    }
    return templates.TemplateResponse("geo_monitor.html", {
        "request": request,
        "providers": providers
    })


@app.get("/audits/{audit_id}/report", response_class=HTMLResponse)
async def audit_report_page(
    request: Request,
    audit_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Full audit report page (printable/PDF-friendly)."""
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()
    
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    
    if audit.status != "completed":
        raise HTTPException(status_code=400, detail="Audit is not completed yet")
    
    # Get all results
    results_result = await db.execute(
        select(AuditResult).where(
            AuditResult.audit_id == audit_id
        ).order_by(desc(AuditResult.score))
    )
    results = results_result.scalars().all()
    
    # Score distribution
    dist_query = select(
        AuditResult.classification,
        func.count(AuditResult.id)
    ).where(
        AuditResult.audit_id == audit_id
    ).group_by(AuditResult.classification)
    
    dist_result = await db.execute(dist_query)
    score_distribution = dict(dist_result.fetchall())
    
    # Parse result_json for top issues
    import json
    top_issues = []
    for r in results[:20]:
        if r.result_json:
            try:
                data = json.loads(r.result_json)
                # Extract optimization opportunities
                opps = data.get("optimization_opportunities", [])
                for opp in opps[:3]:
                    if isinstance(opp, dict) and opp.get("priority") == "high":
                        top_issues.append({
                            "page_url": r.page_url,
                            "category": opp.get("category", ""),
                            "recommendation": opp.get("recommendation", ""),
                            "current_state": opp.get("current_state", "")
                        })
            except (json.JSONDecodeError, TypeError):
                pass
    
    # Get AI Summary if exists
    summary_result = await db.execute(
        select(AuditSummary).where(AuditSummary.audit_id == audit_id)
    )
    summary = summary_result.scalar_one_or_none()
    
    return templates.TemplateResponse("report.html", {
        "request": request,
        "audit": audit,
        "results": results,
        "score_distribution": score_distribution,
        "top_issues": top_issues[:20],
        "summary": summary.to_dict() if summary else None
    })


@app.get("/briefs", response_class=HTMLResponse)
async def briefs_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Content briefs management page."""
    # Get all completed audits for the dropdown
    result = await db.execute(
        select(Audit).where(Audit.status == "completed").order_by(desc(Audit.created_at))
    )
    audits = result.scalars().all()

    return templates.TemplateResponse("briefs.html", {
        "request":      request,
        "audits":       audits,
        "providers_ui": get_providers_for_ui(),
    })


@app.get("/branding", response_class=HTMLResponse)
async def branding_page(request: Request, db: AsyncSession = Depends(get_db)):
    """White-label branding management page."""
    from api.models.database import BrandingConfig
    
    # Get all branding configs
    result = await db.execute(
        select(BrandingConfig).order_by(BrandingConfig.is_default.desc())
    )
    brandings = result.scalars().all()
    
    return templates.TemplateResponse("branding.html", {
        "request": request,
        "brandings": brandings
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Score weights configuration page."""
    import json as _json
    from api.routes.settings import _DEFAULTS, _LABELS

    # Fetch current DB weights (may be empty → using defaults)
    db_rows = (await db.execute(select(AuditWeightConfig))).scalars().all()
    db_weights = {row.audit_type: row.weight for row in db_rows}
    using_defaults = not bool(db_weights)

    weights_out = []
    for atype, default_w in _DEFAULTS.items():
        current_w = db_weights.get(atype, default_w)
        weights_out.append({
            "audit_type": atype,
            "label": _LABELS.get(atype, atype),
            "default_weight": default_w,
            "current_weight": current_w,
            "current_pct": round(current_w * 100, 1),
            "is_custom": atype in db_weights and abs(db_weights[atype] - default_w) > 1e-6,
        })

    weights_payload = {
        "weights": weights_out,
        "using_defaults": using_defaults,
        "total": round(sum(db_weights.get(a, d) for a, d in _DEFAULTS.items()), 4),
    }

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "weights_json": _json.dumps(weights_payload),
    })


@app.get("/schema", response_class=HTMLResponse)
async def schema_generator_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Schema generator page."""
    result = await db.execute(
        select(Audit).where(Audit.status == "completed").order_by(desc(Audit.created_at)).limit(100)
    )
    audits = result.scalars().all()
    providers = {
        "google":     bool(os.getenv("GEMINI_API_KEY")),
        "anthropic":  bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai":     bool(os.getenv("OPENAI_API_KEY")),
        "mistral":    bool(os.getenv("MISTRAL_API_KEY")),
        "perplexity": bool(os.getenv("PERPLEXITY_API_KEY")),
    }
    return templates.TemplateResponse("schema_gen.html", {"request": request, "audits": audits, "providers": providers})


@app.get("/keyword-research", response_class=HTMLResponse)
async def keyword_research_list_page(request: Request):
    """Keyword research sessions list."""
    from api.routes.keyword_research import LOCATION_PRESETS, LLM_DEFAULT_MODELS
    providers = {
        "google":     bool(os.getenv("GEMINI_API_KEY")),
        "anthropic":  bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai":     bool(os.getenv("OPENAI_API_KEY")),
        "mistral":    bool(os.getenv("MISTRAL_API_KEY")),
        "perplexity": bool(os.getenv("PERPLEXITY_API_KEY")),
    }
    return templates.TemplateResponse("keyword_research.html", {
        "request":           request,
        "locations":         LOCATION_PRESETS,
        "providers":         providers,
        "dataforseo_ready":  bool(os.getenv("DATAFORSEO_LOGIN")),
    })


@app.get("/keyword-research/{session_id}", response_class=HTMLResponse)
async def keyword_research_detail_page(
    request: Request, session_id: str, db: AsyncSession = Depends(get_db)
):
    """Keyword research detail — two-panel keyword + question viewer."""
    from api.models.database import KeywordSession, KeywordResult as KWResult
    session = await db.get(KeywordSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Load all keyword results for this session
    kw_rows = (await db.execute(
        select(KWResult)
        .where(KWResult.session_id == session_id)
        .order_by(KWResult.search_volume.desc().nullslast(), KWResult.keyword)
    )).scalars().all()

    keywords_json  = _json.dumps([
        {
            "id":            r.id,
            "keyword":       r.keyword,
            "search_volume": r.search_volume,
            "cpc":           round(r.cpc, 2) if r.cpc else None,
            "competition":   round(r.competition, 2) if r.competition else None,
            "is_question":   r.is_question,
            "pass_number":   r.pass_number,
        }
        for r in kw_rows
    ])

    return templates.TemplateResponse("keyword_research_detail.html", {
        "request":       request,
        "session":       session,
        "keywords_json": keywords_json,
        "total":         len(kw_rows),
    })


@app.get("/gsc", response_class=HTMLResponse)
async def gsc_list_page(request: Request):
    """GSC properties list page."""
    return templates.TemplateResponse("gsc.html", {"request": request})


@app.get("/gsc/{property_id}", response_class=HTMLResponse)
async def gsc_detail_page(
    request: Request,
    property_id: str,
    db: AsyncSession = Depends(get_db),
):
    """GSC property detail — queries, pages, and cross-reference tabs."""
    from api.models.database import GscProperty as GscProp
    prop = await db.get(GscProp, property_id)
    if not prop:
        raise HTTPException(status_code=404, detail="GSC property not found")
    return templates.TemplateResponse("gsc_detail.html", {
        "request":  request,
        "property": prop,
    })


def _repair_guide_json(raw_json_str: str):
    """Parse guide_json and repair any {"raw": "..."} audit entries using json_repair."""
    try:
        gj = json.loads(raw_json_str)
        if isinstance(gj, dict) and "results" in gj:
            from json_repair import repair_json
            for key, val in gj["results"].items():
                if isinstance(val, dict) and "raw" in val and isinstance(val["raw"], str):
                    try:
                        repaired = repair_json(val["raw"], return_objects=True)
                        if isinstance(repaired, dict) and repaired:
                            gj["results"][key] = repaired
                    except Exception:
                        pass
        return gj
    except Exception:
        return None


@app.get("/gsc/{property_id}/page-optimize", response_class=HTMLResponse)
async def gsc_page_optimize_view(
    request: Request,
    property_id: str,
    url: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Dedicated page-optimization view — shows GSC queries and LLM optimization panel."""
    from urllib.parse import unquote
    from sqlalchemy import desc as _desc
    from api.models.database import GscProperty as GscProp, GscPageRow, UrlGuide as _UrlGuide
    prop = await db.get(GscProp, property_id)
    if not prop:
        raise HTTPException(status_code=404, detail="GSC property not found")
    decoded_url = unquote(url)
    page_row = (await db.execute(
        select(GscPageRow)
        .where(GscPageRow.property_id == property_id, GscPageRow.page == decoded_url)
    )).scalar_one_or_none()

    # Load past optimization runs for this URL + property, newest first
    past_guides_rows = (await db.execute(
        select(_UrlGuide)
        .where(
            _UrlGuide.url == decoded_url,
            _UrlGuide.gsc_property_id == property_id,
            _UrlGuide.status == "completed",
        )
        .order_by(_desc(_UrlGuide.created_at))
        .limit(10)
    )).scalars().all()

    past_guides = []
    for g in past_guides_rows:
        gj = _repair_guide_json(g.guide_json) if g.guide_json else None
        past_guides.append({
            "id":         g.id,
            "provider":   g.provider,
            "model":      g.model,
            "reviewed":   bool(g.reviewed),
            "created_at": g.created_at.strftime("%Y-%m-%d %H:%M") if g.created_at else None,
            "guide_json": gj,
        })

    return templates.TemplateResponse("gsc_page_optimize.html", {
        "request":     request,
        "property":    prop,
        "page_url":    decoded_url,
        "page_row":    page_row,
        "past_guides": past_guides,
    })


@app.get("/optimize", response_class=HTMLResponse)
async def standalone_optimize_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Standalone page optimizer — enter any URL + keywords without needing a GSC property."""
    from sqlalchemy import desc as _desc
    from api.models.database import UrlGuide as _UrlGuide
    past_guides_rows = (await db.execute(
        select(_UrlGuide)
        .where(
            _UrlGuide.gsc_property_id.is_(None),
            _UrlGuide.status == "completed",
        )
        .order_by(_desc(_UrlGuide.created_at))
        .limit(50)
    )).scalars().all()

    past_guides = []
    for g in past_guides_rows:
        gj = _repair_guide_json(g.guide_json) if g.guide_json else None
        past_guides.append({
            "id":         g.id,
            "url":        g.url,
            "provider":   g.provider,
            "model":      g.model,
            "reviewed":   bool(g.reviewed),
            "created_at": g.created_at.strftime("%Y-%m-%d %H:%M") if g.created_at else None,
            "guide_json": gj,
        })

    return templates.TemplateResponse("optimize_standalone.html", {
        "request":     request,
        "past_guides": past_guides,
    })


@app.get("/ga4", response_class=HTMLResponse)
async def ga4_list_page(request: Request):
    """GA4 properties list page."""
    return templates.TemplateResponse("ga4.html", {"request": request})


@app.get("/ga4/{property_id}", response_class=HTMLResponse)
async def ga4_detail_page(
    request: Request,
    property_id: str,
    db: AsyncSession = Depends(get_db),
):
    """GA4 property detail — pages, channels, and cross-reference tabs."""
    from api.models.database import Ga4Property as Ga4Prop
    prop = await db.get(Ga4Prop, property_id)
    if not prop:
        raise HTTPException(status_code=404, detail="GA4 property not found")
    return templates.TemplateResponse("ga4_detail.html", {
        "request":  request,
        "property": prop,
    })


@app.get("/ads", response_class=HTMLResponse)
async def ads_list_page(request: Request):
    """Google Ads accounts list page."""
    return templates.TemplateResponse("ads.html", {"request": request})


@app.get("/ads/{account_id}", response_class=HTMLResponse)
async def ads_detail_page(
    request: Request,
    account_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Google Ads account detail — search terms, campaigns, and cross-reference tabs."""
    from api.models.database import AdsAccount as AdsAcc
    acc = await db.get(AdsAcc, account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Ads account not found")
    return templates.TemplateResponse("ads_detail.html", {
        "request": request,
        "account": acc,
    })


@app.get("/insights", response_class=HTMLResponse)
async def insights_list_page(request: Request):
    """AI Insights runs list page."""
    return templates.TemplateResponse("insights.html", {"request": request})


@app.get("/insights/{run_id}", response_class=HTMLResponse)
async def insights_detail_page(
    request: Request,
    run_id: str,
    db: AsyncSession = Depends(get_db),
):
    """AI Insights run detail page."""
    from api.models.database import InsightRun
    run = await db.get(InsightRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Insights run not found")
    return templates.TemplateResponse("insights.html", {
        "request": request,
        "run":     run,
    })


@app.get("/guide/{page_url:path}", response_class=HTMLResponse)
async def guide_page(
    request: Request,
    page_url: str,
    db: AsyncSession = Depends(get_db),
):
    """Per-URL GEO & SEO guide page."""
    from urllib.parse import unquote
    from api.models.database import GscProperty as GscProp

    decoded_url = unquote(page_url)

    # Load available GSC properties for the guide generation form
    gsc_props = (await db.execute(select(GscProp).order_by(GscProp.name))).scalars().all()
    gsc_properties = [{"id": p.id, "name": p.name, "site_url": p.site_url} for p in gsc_props]

    return templates.TemplateResponse("guide.html", {
        "request": request,
        "page_url": decoded_url,
        "gsc_properties": gsc_properties,
    })


@app.get("/llms-txt", response_class=HTMLResponse)
async def llms_txt_page(request: Request):
    """llms.txt generator page."""
    return templates.TemplateResponse("llms_txt.html", {"request": request})


@app.get("/citations", response_class=HTMLResponse)
async def citation_tracker_page(request: Request):
    return templates.TemplateResponse("citation_tracker.html", {"request": request})


@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    return templates.TemplateResponse("portfolio.html", {"request": request})


@app.get("/costs", response_class=HTMLResponse)
async def costs_page(request: Request):
    return templates.TemplateResponse("costs.html", {"request": request})


@app.get("/gap-analysis", response_class=HTMLResponse)
async def gap_analysis_page(request: Request):
    return templates.TemplateResponse("gap_analysis.html", {"request": request})


@app.get("/content-gaps", response_class=HTMLResponse)
async def content_gaps_page(request: Request):
    return templates.TemplateResponse("content_gaps.html", {"request": request})


@app.get("/action-cards", response_class=HTMLResponse)
async def action_cards_page(request: Request):
    return templates.TemplateResponse("action_cards.html", {"request": request})


@app.get("/templates", response_class=HTMLResponse)
async def audit_templates_page(request: Request):
    return templates.TemplateResponse("templates.html", {"request": request})


@app.get("/tracking", response_class=HTMLResponse)
async def tracking_page(request: Request):
    return templates.TemplateResponse("tracking.html", {"request": request})


@app.get("/cross-reference", response_class=HTMLResponse)
async def cross_reference_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Cross-Reference Analysis dashboard — trigger and browse site-wide analyses."""
    import json as _json

    # Distinct websites with at least one completed audit (for the run form)
    stmt = (
        select(Audit.website, func.count(Audit.id).label("cnt"))
        .where(Audit.status == "completed")
        .group_by(Audit.website)
        .order_by(Audit.website)
    )
    website_rows = (await db.execute(stmt)).fetchall()
    websites = [row[0] for row in website_rows]

    # Audit types available
    from api.routes.cross_reference import _result_path, _result_meta

    type_stmt = (
        select(
            Audit.website,
            Audit.audit_type,
            func.count(Audit.id).label("run_count"),
            func.max(Audit.completed_at).label("last_run"),
        )
        .where(Audit.status == "completed")
        .group_by(Audit.website, Audit.audit_type)
        .order_by(Audit.website, Audit.audit_type)
    )
    type_rows = (await db.execute(type_stmt)).fetchall()

    site_entries = []
    for website, audit_type, run_count, last_run in type_rows:
        full_meta = _result_meta(_result_path(website, audit_type, no_llm=False))
        lite_meta = _result_meta(_result_path(website, audit_type, no_llm=True))
        site_entries.append(
            {
                "website": website,
                "audit_type": audit_type,
                "audit_type_label": _AUDIT_TYPE_LABELS.get(audit_type, audit_type),
                "run_count": run_count,
                "last_run": last_run.strftime("%Y-%m-%d") if last_run else None,
                "full_analysis": full_meta,
                "lite_analysis": lite_meta,
            }
        )

    providers = {
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "mistral": bool(os.getenv("MISTRAL_API_KEY")),
    }

    return templates.TemplateResponse(
        "cross_reference.html",
        {
            "request": request,
            "websites": websites,
            "site_entries": site_entries,
            "audit_type_labels": _json.dumps(_AUDIT_TYPE_LABELS),
            "providers": providers,
        },
    )


# ============================================================================
# HTMX PARTIAL ROUTES
# ============================================================================

@app.get("/partials/audit-row/{audit_id}", response_class=HTMLResponse)
async def audit_row_partial(
    request: Request,
    audit_id: str,
    db: AsyncSession = Depends(get_db)
):
    """HTMX partial for updating a single audit row."""
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()
    
    if not audit:
        return HTMLResponse("")
    
    return templates.TemplateResponse("partials/audit_row.html", {
        "request": request,
        "audit": audit
    })


@app.get("/partials/stats", response_class=HTMLResponse)
async def stats_partial(request: Request, db: AsyncSession = Depends(get_db)):
    """HTMX partial for dashboard stats."""
    total_result = await db.execute(select(func.count(Audit.id)))
    total_audits = total_result.scalar()
    
    running_result = await db.execute(
        select(func.count(Audit.id)).where(
            Audit.status.in_(["pending", "scraping", "converting", "analyzing"])
        )
    )
    running_audits = running_result.scalar()
    
    completed_result = await db.execute(
        select(func.count(Audit.id)).where(Audit.status == "completed")
    )
    completed_audits = completed_result.scalar()
    
    pages_result = await db.execute(select(func.sum(Audit.pages_analyzed)))
    total_pages = pages_result.scalar() or 0
    
    avg_result = await db.execute(
        select(func.avg(Audit.average_score)).where(Audit.average_score.isnot(None))
    )
    average_score = avg_result.scalar()
    
    return templates.TemplateResponse("partials/stats.html", {
        "request": request,
        "stats": {
            "total_audits": total_audits,
            "running_audits": running_audits,
            "completed_audits": completed_audits,
            "total_pages": total_pages,
            "average_score": round(average_score, 1) if average_score else None
        }
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
