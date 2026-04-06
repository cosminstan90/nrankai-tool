"""Dashboard and HTMX partial routes."""

import os

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from api.models.database import get_db, Audit
from api.provider_registry import get_providers_for_ui, get_tier_presets
from core.prompt_loader import list_available_audits
from ._shared import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    type: Optional[str] = Query(None, description="Filter by audit type")
):
    """Dashboard page showing recent audits and stats."""
    # Build base query - optionally filter by audit type
    audits_query = select(Audit).where(~Audit.audit_type.startswith('SINGLE_'))
    stats_base = select(func.count(Audit.id)).where(~Audit.audit_type.startswith('SINGLE_'))

    if type:
        audits_query = audits_query.where(Audit.audit_type == type)
        stats_base = stats_base.where(Audit.audit_type == type)

    # Get recent audits (filtered or all)
    result = await db.execute(
        audits_query.order_by(desc(Audit.created_at)).limit(10)
    )
    audits = result.scalars().all()

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
        "audits": audits,
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


@router.get("/new", response_class=HTMLResponse)
async def new_audit_form(request: Request):
    """New audit form page."""
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


# ============================================================================
# HTMX PARTIAL ROUTES
# ============================================================================

@router.get("/partials/audit-row/{audit_id}", response_class=HTMLResponse)
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


@router.get("/partials/stats", response_class=HTMLResponse)
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
