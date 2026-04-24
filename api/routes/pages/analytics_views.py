"""Analytics and monitoring page routes — insights, geo-monitor, benchmarks, schedules."""

import os

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
from api.utils.errors import raise_not_found
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, Audit
from core.prompt_loader import list_available_audits
from ._shared import templates

router = APIRouter()


@router.get("/insights", response_class=HTMLResponse)
async def insights_list_page(request: Request):
    """AI Insights runs list page."""
    return templates.TemplateResponse("insights.html", {"request": request})


@router.get("/insights/{run_id}", response_class=HTMLResponse)
async def insights_detail_page(
    request: Request,
    run_id: str,
    db: AsyncSession = Depends(get_db),
):
    """AI Insights run detail page."""
    from api.models.database import InsightRun
    run = await db.get(InsightRun, run_id)
    if not run:
        raise_not_found("Insights run")
    return templates.TemplateResponse("insights.html", {
        "request": request,
        "run":     run,
    })


@router.get("/fanout", response_class=HTMLResponse)
async def fanout_page(request: Request):
    """WLA Fan-Out Analyzer page."""
    providers = {
        "openai":    bool(os.getenv("OPENAI_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
    }
    return templates.TemplateResponse("fanout.html", {
        "request":   request,
        "providers": providers,
    })


@router.get("/geo-monitor", response_class=HTMLResponse)
async def geo_monitor_page(request: Request):
    """GEO Visibility Monitor page - track AI visibility of websites."""
    providers = {
        "chatgpt": bool(os.getenv("OPENAI_API_KEY")),
        "claude": bool(os.getenv("ANTHROPIC_API_KEY")),
        "perplexity": bool(os.getenv("PERPLEXITY_API_KEY"))
    }
    from api.provider_registry import get_available_providers
    available = get_available_providers()
    return templates.TemplateResponse("geo_monitor.html", {
        "request": request,
        "providers": providers,
        "gemini_available": available.get("google", False),
    })


@router.get("/benchmarks", response_class=HTMLResponse)
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


@router.get("/schedules", response_class=HTMLResponse)
async def schedules_page(request: Request):
    """Scheduled audits page with history tracking."""
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
