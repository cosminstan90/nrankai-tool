"""
WLA Fan-Out Analyzer — FastAPI routes.

Endpoints:
    POST   /api/fanout/analyze                        — single prompt
    POST   /api/fanout/analyze-batch                  — up to 10 prompts (background)
    GET    /api/fanout/sessions                        — paginated history
    GET    /api/fanout/sessions/{id}                   — full session + children
    GET    /api/fanout/sessions/{id}/coverage          — domain coverage report
    DELETE /api/fanout/sessions/{id}
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.models.database import (
    AsyncSessionLocal, get_db,
    FanoutSession, FanoutQuery, FanoutSource,
    FanoutTrackingConfig, FanoutTrackingRun, FanoutTrackingDetail,
)
from api.routes.costs import track_cost
from api.utils.errors import raise_not_found, raise_bad_request
from api.workers.fanout_analyzer import (
    analyze_prompt, analyze_batch, analyze_multi_engine,
    FanoutResult, MultiEngineResult, PROVIDER_DEFAULTS, SUPPORTED_PROVIDERS,
)
from api.workers.prompt_discovery import (
    PromptDiscovery, discovery_result_to_dict, TEMPLATES as DISCOVERY_TEMPLATES,
)

logger = logging.getLogger("fanout.routes")
router = APIRouter(prefix="/api/fanout", tags=["fanout"])

# Max concurrent LLM calls from this router
_SEMAPHORE = asyncio.Semaphore(2)

SUPPORTED_MODELS = {
    "openai":      ["gpt-4o", "gpt-4o-mini"],
    "anthropic":   ["claude-opus-4-5-20251101", "claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
    "gemini":      ["gemini-2.5-flash", "gemini-2.0-flash"],
    "perplexity":  ["sonar-pro", "sonar"],
}


# ============================================================================
# PYDANTIC SCHEMAS
# ============================================================================

class AnalyzeRequest(BaseModel):
    prompt: str
    provider: str = "openai"
    model: Optional[str] = None
    target_url: Optional[str] = None
    user_location: Optional[str] = None
    audit_id: Optional[str] = None

    @field_validator("prompt")
    @classmethod
    def prompt_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Prompt cannot be empty")
        if len(v) > 1000:
            raise ValueError("Prompt must be under 1000 characters")
        return v

    @field_validator("provider")
    @classmethod
    def provider_valid(cls, v: str) -> str:
        v = v.lower()
        if v not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Provider must be one of: {', '.join(SUPPORTED_PROVIDERS)}")
        return v


class BatchAnalyzeRequest(BaseModel):
    prompts: List[str]
    provider: str = "openai"
    model: Optional[str] = None
    target_url: Optional[str] = None
    user_location: Optional[str] = None
    audit_id: Optional[str] = None

    @field_validator("prompts")
    @classmethod
    def prompts_valid(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("At least one prompt required")
        if len(v) > 10:
            raise ValueError("Maximum 10 prompts per batch")
        return [p.strip() for p in v if p.strip()]


class MultiEngineRequest(BaseModel):
    prompt: str
    providers: List[str] = ["openai", "gemini"]
    models: Optional[dict] = None
    target_url: Optional[str] = None
    user_location: Optional[str] = None

    @field_validator("prompt")
    @classmethod
    def prompt_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Prompt cannot be empty")
        if len(v) > 1000:
            raise ValueError("Prompt must be under 1000 characters")
        return v

    @field_validator("providers")
    @classmethod
    def providers_valid(cls, v: List[str]) -> List[str]:
        v = [p.lower() for p in v]
        invalid = [p for p in v if p not in SUPPORTED_PROVIDERS]
        if invalid:
            raise ValueError(f"Unknown providers: {invalid}. Valid: {list(SUPPORTED_PROVIDERS)}")
        if len(v) < 2:
            raise ValueError("At least 2 providers required for multi-engine analysis")
        return v


# ============================================================================
# HELPERS
# ============================================================================

def _extract_domain(url: str) -> str:
    """Return bare domain (no www.) from a URL string."""
    try:
        return urlparse(url).netloc.lstrip("www.") or ""
    except Exception:
        return ""


def _target_domain(target_url: Optional[str]) -> str:
    return _extract_domain(target_url) if target_url else ""


async def _save_fanout_result(
    db: AsyncSession,
    result: FanoutResult,
    target_url: Optional[str] = None,
    audit_id: Optional[str] = None,
) -> str:
    """
    Persist a FanoutResult to the three DB tables.
    Returns the new session_id (UUID string).
    """
    session_id = str(uuid.uuid4())
    tgt_domain = _target_domain(target_url)

    # Determine if / where the target appears in sources
    target_found = False
    target_position: Optional[int] = None
    for pos, src in enumerate(result.sources, start=1):
        if tgt_domain and tgt_domain in src.domain:
            target_found = True
            target_position = pos
            break

    session = FanoutSession(
        id=session_id,
        prompt=result.prompt,
        provider=result.provider,
        model=result.model,
        user_location=None,            # stored on result but not in dataclass yet — fine
        total_fanout_queries=result.total_fanout_queries,
        total_sources=result.total_sources,
        total_search_calls=result.search_call_count,
        target_url=target_url,
        target_found=target_found,
        target_position=target_position,
        audit_id=audit_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(session)

    for pos, q_text in enumerate(result.fanout_queries, start=1):
        db.add(FanoutQuery(
            session_id=session_id,
            query_text=q_text,
            query_position=pos,
        ))

    for pos, src in enumerate(result.sources, start=1):
        db.add(FanoutSource(
            session_id=session_id,
            url=src.url,
            title=src.title,
            domain=src.domain,
            is_target=(tgt_domain != "" and tgt_domain in src.domain),
            source_position=pos,
        ))

    await db.commit()
    return session_id


def _session_to_response(session: FanoutSession, include_children: bool = False) -> dict:
    data = session.to_dict(include_children=include_children)
    data["stats"] = {
        "total_queries":  session.total_fanout_queries,
        "total_sources":  session.total_sources,
        "search_calls":   session.total_search_calls,
        "target_found":   session.target_found,
        "target_position": session.target_position,
    }
    return data


# ============================================================================
# BACKGROUND: batch runner
# ============================================================================

async def _run_batch(
    prompts: List[str],
    provider: str,
    model: Optional[str],
    target_url: Optional[str],
    user_location: Optional[str],
    audit_id: Optional[str],
) -> None:
    """Run batch fan-out analysis and save each result to DB."""
    results = await analyze_batch(
        prompts,
        provider=provider,
        model=model,
        user_location=user_location,
        delay_seconds=2.0,
    )
    async with AsyncSessionLocal() as db:
        for result in results:
            if result.total_fanout_queries == 0 and result.total_sources == 0:
                continue  # skip empty/failed
            try:
                await _save_fanout_result(db, result, target_url=target_url, audit_id=audit_id)
                # Fire-and-forget cost tracking (best-effort)
                asyncio.create_task(track_cost(
                    source="fanout_batch",
                    provider=result.provider,
                    model=result.model,
                    input_tokens=0,   # token counts not surfaced by Responses API
                    output_tokens=0,
                ))
            except Exception as exc:
                logger.error("Failed to save batch result for prompt %r: %s", result.prompt, exc)


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/analyze")
async def analyze_single(
    req: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Run fan-out analysis on a single prompt.
    Blocks until the LLM call completes (10-40s typical).
    """
    async with _SEMAPHORE:
        try:
            result = await analyze_prompt(
                req.prompt,
                provider=req.provider,
                model=req.model,
                user_location=req.user_location,
            )
        except ValueError as exc:
            raise_bad_request(str(exc))
        except Exception as exc:
            logger.error("Fan-out analysis failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"LLM provider error: {exc}")

    session_id = await _save_fanout_result(
        db, result,
        target_url=req.target_url,
        audit_id=req.audit_id,
    )

    # Cost tracking — fire and forget
    background_tasks.add_task(
        track_cost,
        source="fanout_analyze",
        provider=result.provider,
        model=result.model,
        input_tokens=0,
        output_tokens=0,
    )

    tgt_domain = _target_domain(req.target_url)
    target_found = any(tgt_domain in (s.domain or "") for s in result.sources) if tgt_domain else False

    return {
        "session_id":    session_id,
        "prompt":        result.prompt,
        "provider":      result.provider,
        "model":         result.model,
        "fanout_queries": result.fanout_queries,
        "sources": [
            {
                "url":       s.url,
                "title":     s.title,
                "domain":    s.domain,
                "is_target": tgt_domain != "" and tgt_domain in (s.domain or ""),
            }
            for s in result.sources
        ],
        "stats": {
            "total_queries":   result.total_fanout_queries,
            "total_sources":   result.total_sources,
            "search_calls":    result.search_call_count,
            "target_found":    target_found,
            "target_position": next(
                (i + 1 for i, s in enumerate(result.sources) if tgt_domain and tgt_domain in (s.domain or "")),
                None,
            ),
        },
    }


@router.post("/analyze-batch")
async def analyze_batch_endpoint(
    req: BatchAnalyzeRequest,
    background_tasks: BackgroundTasks,
):
    """
    Queue up to 10 prompts for background fan-out analysis.
    Returns immediately with a job identifier (the prompts list).
    Poll GET /api/fanout/sessions to see results as they arrive.
    """
    job_id = str(uuid.uuid4())
    background_tasks.add_task(
        _run_batch,
        req.prompts,
        req.provider,
        req.model,
        req.target_url,
        req.user_location,
        req.audit_id,
    )
    return {
        "job_id":        job_id,
        "total_prompts": len(req.prompts),
        "status":        "processing",
        "note":          "Poll GET /api/fanout/sessions to track results as they arrive.",
    }


@router.post("/analyze-multi")
async def analyze_multi_endpoint(req: MultiEngineRequest):
    """
    Run fan-out analysis on multiple AI engines in parallel for a single prompt.
    Returns combined results with per-engine breakdown and source overlap.
    """
    async with _SEMAPHORE:
        result = await analyze_multi_engine(
            req.prompt,
            providers=req.providers,
            models=req.models,
            user_location=req.user_location,
        )

    engines_out = {}
    for provider, r in result.engines.items():
        engines_out[provider] = {
            "provider":           r.provider,
            "model":              r.model,
            "fanout_queries":     r.fanout_queries,
            "sources":            [{"url": s.url, "title": s.title, "domain": s.domain} for s in r.sources],
            "total_queries":      r.total_fanout_queries,
            "total_sources":      r.total_sources,
            "search_call_count":  r.search_call_count,
        }

    return {
        "prompt":                 result.prompt,
        "engines":                engines_out,
        "combined_queries":       result.combined_queries,
        "combined_sources":       [{"url": s.url, "title": s.title, "domain": s.domain} for s in result.combined_sources],
        "source_overlap":         result.source_overlap,
        "engine_agreement_score": result.engine_agreement_score,
        "timestamp":              result.timestamp.isoformat(),
    }


class DiscoveryRequest(BaseModel):
    target_domain: str
    target_brand: str
    category: str = "generic"
    location: Optional[str] = None
    engines: List[str] = ["openai"]
    max_prompts: int = 20
    quick: bool = False

    @field_validator("engines")
    @classmethod
    def engines_valid(cls, v: List[str]) -> List[str]:
        v = [e.lower() for e in v]
        invalid = [e for e in v if e not in SUPPORTED_PROVIDERS]
        if invalid:
            raise ValueError(f"Unknown engines: {invalid}")
        return v

    @field_validator("max_prompts")
    @classmethod
    def max_prompts_range(cls, v: int) -> int:
        if not (1 <= v <= 50):
            raise ValueError("max_prompts must be between 1 and 50")
        return v

    @field_validator("category")
    @classmethod
    def category_valid(cls, v: str) -> str:
        if v not in DISCOVERY_TEMPLATES:
            return "generic"
        return v


@router.post("/discover")
async def discover_prompts(req: DiscoveryRequest):
    """
    Discover which prompts trigger AI engines to mention a target domain/brand.
    Returns mention rate, strongest/weakest prompts, and competitor dominance.
    """
    async with _SEMAPHORE:
        disc = PromptDiscovery(
            target_domain=req.target_domain,
            target_brand=req.target_brand,
            category=req.category,
            location=req.location,
        )
        if req.quick:
            result = await disc.quick_discover(engines=req.engines, count=min(req.max_prompts, 5))
        else:
            result = await disc.discover(engines=req.engines, max_prompts=req.max_prompts)

    return discovery_result_to_dict(result)


@router.get("/discover/categories")
async def list_discovery_categories():
    """List available business categories and their template counts."""
    return {
        cat: {"template_count": len(tpls)}
        for cat, tpls in DISCOVERY_TEMPLATES.items()
    }


@router.post("/discover/estimate")
async def estimate_discovery_cost(req: DiscoveryRequest):
    """Estimate the cost of a discovery run without running it."""
    disc = PromptDiscovery(
        target_domain=req.target_domain,
        target_brand=req.target_brand,
        category=req.category,
        location=req.location,
    )
    count = min(req.max_prompts, 5) if req.quick else req.max_prompts
    prompts = disc.generate_candidate_prompts(count)
    cost = disc.estimate_cost(len(prompts), req.engines)
    return {
        "estimated_cost_usd": round(cost, 4),
        "prompt_count":       len(prompts),
        "engines":            req.engines,
        "sample_prompts":     prompts[:5],
    }


@router.get("/sessions")
async def list_sessions(
    target_url: Optional[str] = None,
    provider: Optional[str] = None,
    audit_id: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Paginated list of fan-out sessions, newest first."""
    limit = min(limit, 100)

    stmt = select(FanoutSession).order_by(desc(FanoutSession.created_at))
    if target_url:
        stmt = stmt.where(FanoutSession.target_url == target_url)
    if provider:
        stmt = stmt.where(FanoutSession.provider == provider)
    if audit_id:
        stmt = stmt.where(FanoutSession.audit_id == audit_id)

    total_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(total_stmt)).scalar_one()

    rows = (await db.execute(stmt.offset(offset).limit(limit))).scalars().all()

    return {
        "total":   total,
        "offset":  offset,
        "limit":   limit,
        "sessions": [_session_to_response(s) for s in rows],
    }


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Full session with fanout_queries and sources."""
    stmt = (
        select(FanoutSession)
        .where(FanoutSession.id == session_id)
        .options(
            selectinload(FanoutSession.queries),
            selectinload(FanoutSession.sources),
        )
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise_not_found("Fanout session")

    return _session_to_response(session, include_children=True)


@router.get("/sessions/{session_id}/coverage")
async def get_coverage(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Domain coverage report for a session.

    Returns:
        - retrieval_coverage_pct  : % of sources that are the target domain
        - target_found / position
        - missing_queries         : queries where target domain is NOT in sources
        - competing_domains       : top domains by source count
    """
    stmt = (
        select(FanoutSession)
        .where(FanoutSession.id == session_id)
        .options(
            selectinload(FanoutSession.queries),
            selectinload(FanoutSession.sources),
        )
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise_not_found("Fanout session")

    sources = session.sources or []
    queries = session.queries or []
    tgt_domain = _target_domain(session.target_url)

    # Competing domains: count appearances
    domain_counts: dict[str, int] = {}
    for src in sources:
        d = src.domain or ""
        if d:
            domain_counts[d] = domain_counts.get(d, 0) + 1

    competing = sorted(
        [{"domain": d, "appearances": c} for d, c in domain_counts.items()],
        key=lambda x: x["appearances"],
        reverse=True,
    )[:10]

    # Retrieval coverage: % of total sources that are target domain
    target_appearances = domain_counts.get(tgt_domain, 0) if tgt_domain else 0
    coverage_pct = round(target_appearances / len(sources) * 100, 1) if sources else 0.0

    # "Missing queries" — all queries, since we don't track per-query source mapping
    # We flag all queries as potentially missing if target not found at all
    missing_queries = []
    if tgt_domain and not session.target_found:
        missing_queries = [q.query_text for q in queries]
    elif tgt_domain and session.target_found:
        # target appears in sources but we don't know for which query specifically
        # return empty list (no way to tell without per-query source mapping)
        missing_queries = []

    return {
        "session_id":            session_id,
        "target_url":            session.target_url,
        "target_found":          session.target_found,
        "target_position":       session.target_position,
        "retrieval_coverage_pct": coverage_pct,
        "total_sources":         len(sources),
        "target_appearances":    target_appearances,
        "missing_queries":       missing_queries,
        "competing_domains":     competing,
    }


@router.get("/sessions/{session_id}/action-cards")
async def get_action_cards(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate prioritised action cards for a fan-out session.

    Runs the full cross-reference analysis internally and extracts
    the action_cards block. Faster to call than /cross-reference when
    you only need the cards (e.g. for the UI summary panel).

    Priority levels: critical → high → medium → low
    Card types: fanout_coverage, competitor_dominance, quick_win,
                content_gap, citation_gap
    """
    from api.workers.fanout_cross_reference import full_cross_reference

    session = (
        await db.execute(select(FanoutSession).where(FanoutSession.id == session_id))
    ).scalar_one_or_none()
    if not session:
        raise_not_found("Fanout session")

    result = await full_cross_reference(session_id, db)
    return {
        "session_id":   session_id,
        "action_cards": result.get("action_cards", []),
        "total_cards":  len(result.get("action_cards", [])),
    }


@router.get("/sessions/{session_id}/cross-reference")
async def get_cross_reference(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Full cross-reference analysis for a fan-out session.

    Combines three analyses:
      - citations_overlap   : overlap between fan-out sources and Citation Tracker
      - content_gaps        : fan-out queries the target likely lacks content for
      - retrieval_coverage  : how much of the retrieval surface the target covers
                              (includes GEO Monitor query overlap if available)

    Returns null for any section whose prerequisites aren't met (e.g. no
    Citation Tracker configured for the target domain).
    """
    from api.workers.fanout_cross_reference import full_cross_reference

    # Verify session exists
    session = (
        await db.execute(select(FanoutSession).where(FanoutSession.id == session_id))
    ).scalar_one_or_none()
    if not session:
        raise_not_found("Fanout session")

    result = await full_cross_reference(session_id, db)
    return result


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a fan-out session and all its queries/sources (cascade)."""
    session = (
        await db.execute(select(FanoutSession).where(FanoutSession.id == session_id))
    ).scalar_one_or_none()
    if not session:
        raise_not_found("Fanout session")

    await db.delete(session)
    await db.commit()
    return {"deleted": session_id}


# ============================================================================
# TRACKING ENDPOINTS
# ============================================================================

class TrackingConfigCreate(BaseModel):
    name: str
    target_domain: str
    target_brand: Optional[str] = None
    prompts: List[str]
    engines: List[str] = ["openai"]
    schedule: str = "weekly"
    project_id: Optional[str] = None

    @field_validator("prompts")
    @classmethod
    def prompts_not_empty(cls, v):
        if not v:
            raise ValueError("At least one prompt required")
        if len(v) > 50:
            raise ValueError("Maximum 50 prompts per tracking config")
        return v

    @field_validator("schedule")
    @classmethod
    def schedule_valid(cls, v):
        if v not in ("daily", "weekly", "monthly"):
            raise ValueError("schedule must be daily, weekly, or monthly")
        return v

    @field_validator("engines")
    @classmethod
    def engines_valid(cls, v):
        invalid = [e for e in v if e not in SUPPORTED_PROVIDERS]
        if invalid:
            raise ValueError(f"Unknown engines: {invalid}")
        return v


@router.post("/tracking")
async def create_tracking_config(
    req: TrackingConfigCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new fan-out tracking config (scheduled recurring analysis)."""
    from datetime import datetime, timedelta

    schedule_delays = {"daily": 1, "weekly": 7, "monthly": 30}
    next_run = datetime.utcnow() + timedelta(days=schedule_delays[req.schedule])

    config = FanoutTrackingConfig(
        name=req.name,
        target_domain=req.target_domain.lower().lstrip("www."),
        target_brand=req.target_brand,
        prompts=req.prompts,
        engines=req.engines,
        schedule=req.schedule,
        project_id=req.project_id,
        next_run_at=next_run,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config.to_dict()


@router.get("/tracking")
async def list_tracking_configs(
    is_active: Optional[bool] = None,
    project_id: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """List all fan-out tracking configs."""
    q = select(FanoutTrackingConfig)
    if is_active is not None:
        q = q.where(FanoutTrackingConfig.is_active == is_active)
    if project_id:
        q = q.where(FanoutTrackingConfig.project_id == project_id)
    q = q.order_by(FanoutTrackingConfig.created_at.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [r.to_dict() for r in rows]


@router.get("/tracking/{config_id}/timeline")
async def get_tracking_timeline(
    config_id: str,
    period: str = "30d",
    db: AsyncSession = Depends(get_db),
):
    """
    Return time-series mention_rate data for a tracking config.
    period: 7d | 30d | 90d | all
    """
    config = await db.get(FanoutTrackingConfig, config_id)
    if not config:
        raise_not_found("Tracking config")

    from datetime import datetime, timedelta
    period_days = {"7d": 7, "30d": 30, "90d": 90}.get(period, None)

    q = select(FanoutTrackingRun).where(
        and_(
            FanoutTrackingRun.config_id == config_id,
            FanoutTrackingRun.status == "completed",
        )
    ).order_by(FanoutTrackingRun.run_date)

    if period_days:
        cutoff = (datetime.utcnow() - timedelta(days=period_days)).strftime("%Y-%m-%d")
        q = q.where(FanoutTrackingRun.run_date >= cutoff)

    runs = (await db.execute(q)).scalars().all()

    timeline = [
        {
            "date":            r.run_date,
            "mention_rate":    r.mention_rate,
            "composite_score": r.composite_score,
            "model_version":   r.model_version,
            "cost_usd":        r.cost_usd,
        }
        for r in runs
    ]

    # Trend: compare last vs first
    trend = None
    change_vs_first = None
    model_drift_detected = False

    if len(runs) >= 2:
        first_rate = runs[0].mention_rate or 0
        last_rate = runs[-1].mention_rate or 0
        change_vs_first = round(last_rate - first_rate, 4)
        trend = "up" if change_vs_first > 0.01 else ("down" if change_vs_first < -0.01 else "stable")

        # Model drift: check if model_version changed between consecutive runs
        versions = [r.model_version for r in runs if r.model_version]
        if len(set(versions)) > 1:
            model_drift_detected = True

    return {
        "config":                config.to_dict(),
        "timeline":              timeline,
        "trend":                 trend,
        "change_vs_first":       change_vs_first,
        "model_drift_detected":  model_drift_detected,
        "period":                period,
        "run_count":             len(runs),
    }


@router.post("/tracking/{config_id}/run-now")
async def run_tracking_now(
    config_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Trigger an immediate tracking run (runs in background)."""
    config = await db.get(FanoutTrackingConfig, config_id)
    if not config:
        raise_not_found("Tracking config")

    from api.workers.fanout_tracker_worker import run_tracking
    background_tasks.add_task(run_tracking, config_id)
    return {"status": "queued", "config_id": config_id}


@router.get("/tracking/dead-letters")
async def list_dead_letters(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """List failed tracking runs that exceeded max retries."""
    rows = (await db.execute(
        select(FanoutTrackingRun)
        .where(FanoutTrackingRun.is_dead_letter == True)
        .order_by(FanoutTrackingRun.created_at.desc())
        .limit(limit)
    )).scalars().all()
    return [r.to_dict() for r in rows]


@router.post("/tracking/dead-letters/{run_id}/retry")
async def retry_dead_letter(
    run_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Manually re-queue a dead-letter run."""
    run = await db.get(FanoutTrackingRun, run_id)
    if not run:
        raise_not_found("Tracking run")

    run.is_dead_letter = False
    run.status = "pending"
    run.retry_count = 0
    run.next_retry_at = None
    run.failure_reason = None
    await db.commit()
    return {"status": "requeued", "run_id": run_id}


@router.post("/tracking/dead-letters/{run_id}/dismiss")
async def dismiss_dead_letter(
    run_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Dismiss a dead-letter run (keeps it but marks failure_reason as dismissed)."""
    run = await db.get(FanoutTrackingRun, run_id)
    if not run:
        raise_not_found("Tracking run")

    run.failure_reason = "dismissed"
    await db.commit()
    return {"status": "dismissed", "run_id": run_id}
