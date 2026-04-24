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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from api.limiter import limiter
from pydantic import BaseModel, field_validator
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.models.database import (
    AsyncSessionLocal, get_db,
    FanoutSession, FanoutQuery, FanoutSource,
    FanoutTrackingConfig, FanoutTrackingRun, FanoutTrackingDetail,
    FanoutCompetitiveReport,
    FanoutCacheEntry,
    FanoutSerpValidation,
    FanoutWebhook, FanoutWebhookLog,
    FanoutCrossRefResult,
    FanoutPromptLibrary,
)
from api.routes.costs import track_cost
from api.utils.errors import raise_not_found, raise_bad_request
from api.workers.fanout_analyzer import (
    analyze_prompt, analyze_batch, analyze_multi_engine,
    FanoutResult, MultiEngineResult, PROVIDER_DEFAULTS, SUPPORTED_PROVIDERS,
    estimate_run_cost,
)
from api.workers.prompt_discovery import (
    PromptDiscovery, discovery_result_to_dict, TEMPLATES as DISCOVERY_TEMPLATES,
    classify_prompt_cluster,
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

    # Prompt 15: classify cluster + estimate cost
    _cluster = classify_prompt_cluster(result.prompt)
    _cost    = estimate_run_cost(result.model)

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
        # Enrichment (Prompt 15)
        prompt_cluster=_cluster,
        run_cost_usd=_cost,
        engine=result.provider,
        model_version=result.model,
        from_cache=getattr(result, "from_cache", False),
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
@limiter.limit("20/hour")
async def analyze_single(
    request: Request,
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
@limiter.limit("10/hour")
async def analyze_batch_endpoint(
    request: Request,
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
@limiter.limit("20/hour")
async def analyze_multi_endpoint(request: Request, req: MultiEngineRequest):
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
@limiter.limit("20/hour")
async def discover_prompts(request: Request, req: DiscoveryRequest):
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
    target_url: Optional[str]   = None,
    provider: Optional[str]     = None,
    audit_id: Optional[str]     = None,
    cluster: Optional[str]      = None,
    engine: Optional[str]       = None,
    locale: Optional[str]       = None,
    query_origin: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Paginated list of fan-out sessions, newest first. Supports Prompt 15 filters."""
    limit = min(limit, 100)

    stmt = select(FanoutSession).order_by(desc(FanoutSession.created_at))
    if target_url:
        stmt = stmt.where(FanoutSession.target_url == target_url)
    if provider:
        stmt = stmt.where(FanoutSession.provider == provider)
    if audit_id:
        stmt = stmt.where(FanoutSession.audit_id == audit_id)
    if cluster:
        stmt = stmt.where(FanoutSession.prompt_cluster == cluster)
    if engine:
        stmt = stmt.where(FanoutSession.engine == engine)
    if locale:
        stmt = stmt.where(FanoutSession.locale == locale)
    if query_origin:
        stmt = stmt.where(FanoutSession.query_origin == query_origin)

    total_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await db.execute(total_stmt)).scalar_one()

    rows = (await db.execute(stmt.offset(offset).limit(limit))).scalars().all()

    # Prompt 15 aggregation on filtered set
    all_stmt  = select(FanoutSession)
    all_rows  = (await db.execute(all_stmt)).scalars().all()
    by_cluster: dict = {}
    by_engine:  dict = {}
    total_cost = 0.0
    for s in all_rows:
        c = s.prompt_cluster or "unknown"
        by_cluster[c] = by_cluster.get(c, 0) + 1
        e = s.engine or s.provider or "unknown"
        by_engine[e]  = by_engine.get(e, 0) + 1
        total_cost    += s.run_cost_usd or 0.0

    return {
        "total":          total,
        "offset":         offset,
        "limit":          limit,
        "sessions":       [_session_to_response(s) for s in rows],
        "aggregation": {
            "by_cluster":     by_cluster,
            "by_engine":      by_engine,
            "total_cost_usd": round(total_cost, 4),
        },
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
    next_run = datetime.now(timezone.utc) + timedelta(days=schedule_delays[req.schedule])

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
        cutoff = (datetime.now(timezone.utc) - timedelta(days=period_days)).strftime("%Y-%m-%d")
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


# ============================================================================
# COMPETITIVE COMPARISON ENDPOINTS
# ============================================================================

class CompetitiveRequest(BaseModel):
    prompts: List[str]
    competitors: List[str]
    engines: List[str] = ["openai"]
    target_domain: str
    project_id: Optional[str] = None

    @field_validator("prompts")
    @classmethod
    def prompts_not_empty(cls, v: List[str]) -> List[str]:
        v = [p.strip() for p in v if p.strip()]
        if not v:
            raise ValueError("At least one prompt is required")
        if len(v) > 50:
            raise ValueError("Maximum 50 prompts per competitive run")
        return v

    @field_validator("competitors")
    @classmethod
    def competitors_valid(cls, v: List[str]) -> List[str]:
        v = [c.strip() for c in v if c.strip()]
        if not v:
            raise ValueError("At least one competitor domain is required")
        if len(v) > 5:
            raise ValueError("Maximum 5 competitors per run")
        return v

    @field_validator("engines")
    @classmethod
    def engines_valid(cls, v: List[str]) -> List[str]:
        v = [e.lower() for e in v]
        invalid = [e for e in v if e not in SUPPORTED_PROVIDERS]
        if invalid:
            raise ValueError(f"Unknown engines: {invalid}. Valid: {list(SUPPORTED_PROVIDERS)}")
        return v

    @field_validator("target_domain")
    @classmethod
    def target_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("target_domain cannot be empty")
        return v


async def _run_competitive(
    report_id: str,
    prompts: List[str],
    competitors: List[str],
    engines: List[str],
    target_domain: str,
    project_id: Optional[str],
) -> None:
    """Background task: run competitive analysis and persist result."""
    from api.workers.fanout_competitive import compare_competitors

    try:
        report = await compare_competitors(
            prompts=prompts,
            competitors=competitors,
            engines=engines,
            target_domain=target_domain,
        )
    except Exception as exc:
        logger.error("Competitive analysis failed for report %s: %s", report_id, exc)
        return

    async with AsyncSessionLocal() as db:
        try:
            record = await db.get(FanoutCompetitiveReport, report_id)
            if record is None:
                logger.error("Competitive report record %s not found in DB", report_id)
                return
            record.report = report.to_dict()
            await db.commit()
            logger.info("Competitive report %s saved", report_id)
        except Exception as exc:
            logger.error("Failed to save competitive report %s: %s", report_id, exc)


@router.post("/competitive")
@limiter.limit("20/hour")
async def create_competitive_report(
    request: Request,
    req: CompetitiveRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Start a competitive fan-out comparison.

    Analyzes each prompt × engine combination and measures the presence of
    the target domain vs. up to 5 competitors in AI-generated answers.

    The analysis runs in the background; this endpoint returns immediately
    with a cost estimate and the report_id for polling.
    """
    report_id = str(uuid.uuid4())
    cost_estimate = len(req.prompts) * len(req.engines) * 0.005

    record = FanoutCompetitiveReport(
        id=report_id,
        target_domain=req.target_domain,
        project_id=req.project_id,
        competitors=req.competitors,
        report=None,
    )
    db.add(record)
    await db.commit()

    background_tasks.add_task(
        _run_competitive,
        report_id=report_id,
        prompts=req.prompts,
        competitors=req.competitors,
        engines=req.engines,
        target_domain=req.target_domain,
        project_id=req.project_id,
    )

    return {
        "report_id":      report_id,
        "status":         "running",
        "cost_estimate":  round(cost_estimate, 4),
        "prompts_count":  len(req.prompts),
        "engines":        req.engines,
        "competitors":    req.competitors,
        "target_domain":  req.target_domain,
    }


@router.get("/competitive/{report_id}")
async def get_competitive_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Retrieve a saved competitive fan-out comparison report."""
    record = await db.get(FanoutCompetitiveReport, report_id)
    if not record:
        raise_not_found("Competitive report")

    status = "completed" if record.report is not None else "running"
    return {
        "report_id":     record.id,
        "target_domain": record.target_domain,
        "project_id":    record.project_id,
        "competitors":   record.competitors,
        "status":        status,
        "report":        record.report,
        "created_at":    record.created_at.isoformat() if record.created_at else None,
    }


# ============================================================================
# CACHE ENDPOINTS  (Prompt 16)
# ============================================================================

@router.get("/cache/stats")
async def cache_stats(db: AsyncSession = Depends(get_db)):
    """Return fanout cache statistics."""
    from api.workers.fanout_cache import FanoutCache
    return await FanoutCache.get_stats(db)


@router.delete("/cache")
async def clear_cache(
    engine: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Clear cache entries.
    - engine=<name>  → clear only that engine
    - no params      → cleanup expired entries only
    """
    from api.workers.fanout_cache import FanoutCache
    if engine:
        count = await FanoutCache.clear_by_engine(db, engine)
        return {"deleted": count, "engine": engine}
    else:
        count = await FanoutCache.cleanup_expired(db)
        return {"deleted": count, "action": "cleanup_expired"}


# ============================================================================
# EXPORT ENDPOINTS  (Prompt 17)
# ============================================================================

@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    format: str = "json",
    db: AsyncSession = Depends(get_db),
):
    """Export a session as JSON or CSV (StreamingResponse with attachment header)."""
    from fastapi.responses import StreamingResponse
    from api.workers.fanout_export import FanoutExporter

    stmt = (
        select(FanoutSession)
        .where(FanoutSession.id == session_id)
        .options(selectinload(FanoutSession.queries), selectinload(FanoutSession.sources))
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise_not_found("Session")

    exporter = FanoutExporter()
    date_str = (session.created_at or datetime.now(timezone.utc)).strftime("%Y%m%d")

    if format == "csv":
        content  = exporter.session_to_csv(session)
        media    = "text/csv; charset=utf-8-sig"
        filename = f"fanout_{session_id[:8]}_{date_str}.csv"
        return StreamingResponse(
            iter([content]),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    else:
        import json as _json
        data     = exporter.session_to_json(session)
        content  = _json.dumps(data, ensure_ascii=False, indent=2)
        filename = f"fanout_{session_id[:8]}_{date_str}.json"
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


@router.get("/tracking/{config_id}/export")
async def export_timeline(
    config_id: str,
    format: str = "csv",
    period: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Export a tracking timeline as CSV."""
    from fastapi.responses import StreamingResponse
    from api.workers.fanout_export import FanoutExporter

    config = await db.get(FanoutTrackingConfig, config_id)
    if not config:
        raise_not_found("Tracking config")

    stmt = select(FanoutTrackingRun).where(
        FanoutTrackingRun.config_id == config_id,
        FanoutTrackingRun.status == "completed",
    ).order_by(FanoutTrackingRun.created_at)
    runs = (await db.execute(stmt)).scalars().all()

    exporter  = FanoutExporter()
    content   = exporter.tracking_timeline_to_csv(config, list(runs))
    date_str  = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename  = f"timeline_{config_id[:8]}_{date_str}.csv"

    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/competitive/{report_id}/export")
async def export_competitive(
    report_id: str,
    format: str = "csv",
    db: AsyncSession = Depends(get_db),
):
    """Export a competitive report as CSV."""
    from fastapi.responses import StreamingResponse
    from api.workers.fanout_export import FanoutExporter

    record = await db.get(FanoutCompetitiveReport, report_id)
    if not record:
        raise_not_found("Competitive report")

    exporter = FanoutExporter()
    content  = exporter.competitive_report_to_csv(record.report or {})
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"competitive_{report_id[:8]}_{date_str}.csv"

    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ClientReportRequest(BaseModel):
    brand: str
    domain: str
    discovery_id: Optional[str] = None
    tracking_config_id: Optional[str] = None
    competitive_report_id: Optional[str] = None


@router.post("/export/client-report")
async def export_client_report(
    req: ClientReportRequest,
    db: AsyncSession = Depends(get_db),
):
    """Generate a plain-text client report combining discovery + timeline + competitive data."""
    from fastapi.responses import StreamingResponse
    from api.workers.fanout_export import FanoutExporter

    discovery   = None
    timeline    = None
    competitive = None

    if req.tracking_config_id:
        cfg = await db.get(FanoutTrackingConfig, req.tracking_config_id)
        if cfg:
            stmt = select(FanoutTrackingRun).where(
                FanoutTrackingRun.config_id == req.tracking_config_id,
                FanoutTrackingRun.status == "completed",
            ).order_by(FanoutTrackingRun.created_at)
            runs = (await db.execute(stmt)).scalars().all()
            timeline = {"config": cfg.to_dict(), "runs": [r.to_dict() for r in runs]}

    if req.competitive_report_id:
        rec = await db.get(FanoutCompetitiveReport, req.competitive_report_id)
        if rec:
            competitive = rec.report

    exporter = FanoutExporter()
    text     = exporter.generate_client_report_text(req.brand, req.domain, discovery, timeline, competitive)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"client_report_{req.domain.replace('.','_')}_{date_str}.txt"

    return StreamingResponse(
        iter([text]),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================================
# SERP VALIDATION ENDPOINTS  (Prompt 19)
# ============================================================================

class SerpValidateRequest(BaseModel):
    target_domain: str
    gl: str = "us"
    hl: str = "en"
    max_queries: int = 20


@router.post("/sessions/{session_id}/validate-serp")
@limiter.limit("20/hour")
async def validate_serp(
    request: Request,
    session_id: str,
    req: SerpValidateRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Validate fan-out queries against real SERP results via Serper.dev.
    Requires SERPER_API_KEY in .env.
    """
    import os
    from api.workers.serp_validator import SERPValidator, SERP_COST_PER_QUERY

    api_key = os.getenv("SERPER_API_KEY")
    if not api_key:
        raise_bad_request("SERPER_API_KEY not configured")

    stmt = (
        select(FanoutSession)
        .where(FanoutSession.id == session_id)
        .options(selectinload(FanoutSession.queries))
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise_not_found("Session")

    queries = [q.query_text for q in (session.queries or [])]
    if not queries:
        raise_bad_request("Session has no fan-out queries to validate")

    queries_to_check = queries[: req.max_queries]
    total_cost = len(queries_to_check) * SERP_COST_PER_QUERY

    validator = SERPValidator(api_key=api_key)
    results   = await validator.validate_batch(queries_to_check, req.target_domain, req.gl, req.hl)

    # Categorise
    synced = []; ai_gap = []; ai_only = []; double_gap = []
    paa_all: list = []
    saved   = []

    for q_text, sr in zip(queries_to_check, results):
        # Did the AI include this query? (all session queries = AI found)
        ai_found   = True   # by definition — these are fan-out queries
        serp_found = sr.target_found

        row = FanoutSerpValidation(
            session_id             = session_id,
            query_text             = q_text,
            target_domain          = req.target_domain,
            target_found           = serp_found,
            target_position        = sr.target_position,
            has_featured_snippet   = sr.featured_snippet_domain is not None,
            featured_snippet_domain= sr.featured_snippet_domain,
            top_10_domains         = sr.top_10_domains,
            people_also_ask        = sr.people_also_ask,
            gl                     = req.gl,
            hl                     = req.hl,
        )
        db.add(row)
        paa_all.extend(sr.people_also_ask or [])

        entry = {"query": q_text, "target_position": sr.target_position, "top_10": sr.top_10_domains}
        if ai_found and serp_found:
            synced.append(entry)
        elif not ai_found and serp_found:
            ai_gap.append(entry)
        elif ai_found and not serp_found:
            ai_only.append(entry)
        else:
            double_gap.append(entry)

    await db.commit()

    return {
        "session_id":     session_id,
        "target_domain":  req.target_domain,
        "total_queries":  len(queries_to_check),
        "total_cost_usd": round(total_cost, 4),
        "synced":         synced,
        "ai_gap":         ai_gap,
        "ai_only":        ai_only,
        "double_gap":     double_gap,
        "people_also_ask": list(dict.fromkeys(paa_all))[:20],
        "traffic_at_risk": len(ai_gap),
    }


# ============================================================================
# CROSS-REFERENCE ENDPOINTS  (Prompt 18)
# ============================================================================

class CrossRefRequest(BaseModel):
    session_id: str
    audit_id: str
    target_domain: Optional[str] = None
    project_id: Optional[str] = None


@router.post("/crossref")
@limiter.limit("20/hour")
async def create_crossref(
    request: Request,
    req: CrossRefRequest,
    db: AsyncSession = Depends(get_db),
):
    """Cross-reference a fan-out session against a WLA audit."""
    from api.workers.fanout_wla_crossref import analyze as crossref_analyze
    import json as _json

    stmt = (
        select(FanoutSession)
        .where(FanoutSession.id == req.session_id)
        .options(selectinload(FanoutSession.queries))
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise_not_found("Fan-out session")

    result = await crossref_analyze(
        fanout_session_id=req.session_id,
        wla_audit_id=req.audit_id,
        target_domain=req.target_domain or session.target_url or "",
        db=db,
    )

    record = FanoutCrossRefResult(
        session_id    = req.session_id,
        audit_id      = req.audit_id,
        project_id    = req.project_id,
        target_domain = req.target_domain or session.target_url,
        result_json   = _json.dumps(result),
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return record.to_dict()


@router.get("/crossref/{crossref_id}")
async def get_crossref(
    crossref_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Retrieve a saved cross-reference result."""
    record = await db.get(FanoutCrossRefResult, crossref_id)
    if not record:
        raise_not_found("Cross-reference result")
    return record.to_dict()


# ============================================================================
# WEBHOOK ENDPOINTS  (Prompt 20)
# ============================================================================

class WebhookCreate(BaseModel):
    name: str
    webhook_url: str
    events: List[str]
    secret_key: Optional[str] = None


@router.post("/webhooks")
async def create_webhook(req: WebhookCreate, db: AsyncSession = Depends(get_db)):
    """Register a new webhook endpoint."""
    wh = FanoutWebhook(
        name        = req.name,
        webhook_url = req.webhook_url,
        events      = req.events,
        secret_key  = req.secret_key,
    )
    db.add(wh)
    await db.commit()
    await db.refresh(wh)
    return wh.to_dict()


@router.get("/webhooks")
async def list_webhooks(db: AsyncSession = Depends(get_db)):
    """List all registered webhooks."""
    rows = (await db.execute(select(FanoutWebhook).order_by(FanoutWebhook.id))).scalars().all()
    return [r.to_dict() for r in rows]


@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: int, db: AsyncSession = Depends(get_db)):
    """Soft-delete (deactivate) a webhook."""
    wh = await db.get(FanoutWebhook, webhook_id)
    if not wh:
        raise_not_found("Webhook")
    wh.is_active = False
    await db.commit()
    return {"ok": True, "id": webhook_id}


@router.post("/webhooks/{webhook_id}/test")
async def test_webhook(webhook_id: int, db: AsyncSession = Depends(get_db)):
    """Send a test payload to a webhook."""
    from api.workers.webhook_sender import send as wh_send

    wh = await db.get(FanoutWebhook, webhook_id)
    if not wh:
        raise_not_found("Webhook")

    ok = await wh_send(
        wh.webhook_url,
        "test_event",
        {"message": "nrankai fan-out webhook test", "webhook_id": webhook_id},
        secret_key=wh.secret_key,
    )
    return {"ok": ok, "webhook_id": webhook_id}


# ============================================================================
# PROMPT LIBRARY ENDPOINTS  (Prompt 21)
# ============================================================================

class PromptLibraryCreate(BaseModel):
    prompt_text: str
    vertical: str = "generic"
    cluster: Optional[str] = None
    language: str = "en"
    locale: str = "en-US"
    tags: Optional[List[str]] = None
    is_template: bool = False
    template_vars: Optional[List[str]] = None


@router.get("/prompt-library")
async def list_prompt_library(
    vertical: Optional[str]     = None,
    cluster: Optional[str]      = None,
    performance: Optional[str]  = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """List prompts from the library with optional filters."""
    from api.workers.prompt_library import PromptLibrary
    results = await PromptLibrary.get_for_display(db, vertical=vertical, cluster=cluster, performance=performance, limit=limit)
    return results


@router.get("/prompt-library/stats")
async def prompt_library_stats(db: AsyncSession = Depends(get_db)):
    """Aggregate stats for the prompt library."""
    from api.workers.prompt_library import PromptLibrary
    return await PromptLibrary.get_stats(db)


@router.get("/prompt-library/suggest")
async def suggest_prompts(
    vertical: str,
    existing: Optional[str] = None,   # comma-separated prompt texts
    db: AsyncSession = Depends(get_db),
):
    """Suggest prompts from the library not yet in existing_prompts."""
    from api.workers.prompt_library import PromptLibrary
    existing_list = [e.strip() for e in (existing or "").split(",") if e.strip()]
    suggestions = await PromptLibrary.suggest_gaps(db, vertical, existing_list)
    return {"suggestions": suggestions}


@router.post("/prompt-library")
async def add_prompt(req: PromptLibraryCreate, db: AsyncSession = Depends(get_db)):
    """Add a custom prompt to the library."""
    from api.workers.prompt_library import PromptLibrary
    result = await PromptLibrary.add_prompt(db, req.prompt_text, req.vertical, req.cluster,
                                             req.language, req.locale, req.tags,
                                             req.is_template, req.template_vars)
    return result


# ============================================================================
# COMPOSITE SCORE ENDPOINTS  (Prompt 22)
# ============================================================================

@router.get("/sessions/{session_id}/composite-score")
async def session_composite_score(session_id: str, db: AsyncSession = Depends(get_db)):
    """Compute on-the-fly GEO composite score for a single fan-out session."""
    from api.workers.geo_composite_score import calculate

    stmt = (
        select(FanoutSession)
        .where(FanoutSession.id == session_id)
        .options(selectinload(FanoutSession.queries), selectinload(FanoutSession.sources))
    )
    session = (await db.execute(stmt)).scalar_one_or_none()
    if not session:
        raise_not_found("Session")

    mention_rate = 1.0 if session.target_found else 0.0
    avg_pos      = float(session.target_position) if session.target_position else None
    breakdown    = calculate(
        mention_rate          = mention_rate,
        avg_position          = avg_pos,
        engines_with_mention  = 1 if session.target_found else 0,
        total_engines         = 1,
        clusters_with_mention = 1 if session.target_found else 0,
        clusters_tested       = 1,
    )
    return breakdown.to_dict()


@router.get("/tracking/{config_id}/score-history")
async def tracking_score_history(config_id: str, db: AsyncSession = Depends(get_db)):
    """Return composite_score history for a tracking config."""
    runs = (await db.execute(
        select(FanoutTrackingRun)
        .where(FanoutTrackingRun.config_id == config_id, FanoutTrackingRun.status == "completed")
        .order_by(FanoutTrackingRun.created_at)
    )).scalars().all()

    return {
        "config_id": config_id,
        "history": [
            {
                "run_date":        r.run_date,
                "composite_score": r.composite_score,
                "score_breakdown": r.score_breakdown,
                "mention_rate":    r.mention_rate,
                "created_at":      r.created_at.isoformat() if r.created_at else None,
            }
            for r in runs
        ],
    }


# ============================================================================
# SENTIMENT ENDPOINTS  (Prompt 23)
# ============================================================================

@router.post("/sessions/{session_id}/analyze-sentiment")
async def analyze_session_sentiment(
    session_id: str,
    target_brand: str,
    db: AsyncSession = Depends(get_db),
):
    """Run sentiment analysis on the AI response for a fan-out session."""
    from api.workers.sentiment_analyzer import analyze_sentiment
    from api.models.database import FanoutSentiment

    session = await db.get(FanoutSession, session_id)
    if not session:
        raise_not_found("Session")

    # Use the prompt as proxy for AI response text (we don't store raw response)
    ai_text = session.prompt  # fallback; ideally raw_output would be stored
    result  = await analyze_sentiment(ai_text, target_brand, session.target_url)

    # Upsert
    existing = (await db.execute(
        select(FanoutSentiment).where(FanoutSentiment.session_id == session_id)
    )).scalar_one_or_none()

    if existing:
        existing.overall_sentiment   = result.overall_sentiment
        existing.confidence          = result.confidence
        existing.brand_mention_count = len(result.brand_mentions)
        existing.mentions_json       = [m.to_dict() if hasattr(m, "to_dict") else vars(m) for m in result.brand_mentions]
        existing.summary             = result.summary
    else:
        row = FanoutSentiment(
            session_id          = session_id,
            overall_sentiment   = result.overall_sentiment,
            confidence          = result.confidence,
            brand_mention_count = len(result.brand_mentions),
            mentions_json       = [m.to_dict() if hasattr(m, "to_dict") else vars(m) for m in result.brand_mentions],
            summary             = result.summary,
        )
        db.add(row)

    await db.commit()
    return result.to_dict()


@router.get("/sessions/{session_id}/sentiment")
async def get_session_sentiment(session_id: str, db: AsyncSession = Depends(get_db)):
    """Return stored sentiment analysis for a session."""
    from api.models.database import FanoutSentiment

    row = (await db.execute(
        select(FanoutSentiment).where(FanoutSentiment.session_id == session_id)
    )).scalar_one_or_none()
    if not row:
        raise_not_found("Sentiment analysis")
    return row.to_dict()


@router.get("/tracking/{config_id}/sentiment-trend")
async def sentiment_trend(config_id: str, db: AsyncSession = Depends(get_db)):
    """Return sentiment breakdown across tracking runs."""
    from api.models.database import FanoutSentiment

    runs = (await db.execute(
        select(FanoutTrackingRun)
        .where(FanoutTrackingRun.config_id == config_id, FanoutTrackingRun.status == "completed")
        .order_by(FanoutTrackingRun.created_at)
        .limit(30)
    )).scalars().all()

    return {
        "config_id": config_id,
        "trend": [
            {
                "run_date":           r.run_date,
                "sentiment_breakdown": r.sentiment_breakdown,
                "dominant_sentiment":  getattr(r, "dominant_sentiment", None),
            }
            for r in runs
        ],
    }


# ============================================================================
# VELOCITYCMS ENDPOINTS  (Prompt 28)
# ============================================================================

@router.post("/crossref/{crossref_id}/gaps/{gap_idx}/create-draft")
async def create_cms_draft(
    crossref_id: str,
    gap_idx: int,
    db: AsyncSession = Depends(get_db),
):
    """Create a VelocityCMS draft for a single content gap."""
    from api.workers.velocitycms_bridge import create_draft_from_gap

    record = await db.get(FanoutCrossRefResult, crossref_id)
    if not record:
        raise_not_found("Cross-reference result")

    result_data = record.to_dict().get("result", {})
    gap_queries = result_data.get("gap_queries", [])
    if gap_idx >= len(gap_queries):
        raise_bad_request(f"Gap index {gap_idx} out of range (total: {len(gap_queries)})")

    gap     = gap_queries[gap_idx]
    project = {"target_brand": "", "target_domain": record.target_domain or "", "vertical": "generic", "language": "en"}

    draft_result = await create_draft_from_gap(gap, project)
    return draft_result.to_dict()


@router.post("/crossref/{crossref_id}/gaps/create-all-drafts")
async def create_all_cms_drafts(
    crossref_id: str,
    priority: str = "high",
    db: AsyncSession = Depends(get_db),
):
    """Create VelocityCMS drafts for all high-priority content gaps."""
    from api.workers.velocitycms_bridge import create_draft_from_gap

    record = await db.get(FanoutCrossRefResult, crossref_id)
    if not record:
        raise_not_found("Cross-reference result")

    result_data = record.to_dict().get("result", {})
    gap_queries = [g for g in result_data.get("gap_queries", []) if g.get("priority") == priority]

    project     = {"target_brand": "", "target_domain": record.target_domain or "", "vertical": "generic", "language": "en"}
    results     = []
    for gap in gap_queries[:10]:  # cap at 10 to avoid runaway costs
        dr = await create_draft_from_gap(gap, project)
        results.append(dr.to_dict())

    return {"created": len(results), "drafts": results}


