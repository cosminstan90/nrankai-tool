"""Settings and content management page routes."""

import os
import json as _json


from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, Audit, AuditWeightConfig
from api.provider_registry import get_providers_for_ui
from ._shared import templates, _AUDIT_TYPE_LABELS

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Score weights configuration page."""
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


@router.get("/branding", response_class=HTMLResponse)
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


@router.get("/briefs", response_class=HTMLResponse)
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


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    return templates.TemplateResponse("portfolio.html", {"request": request})


@router.get("/costs", response_class=HTMLResponse)
async def costs_page(request: Request):
    return templates.TemplateResponse("costs.html", {"request": request})


@router.get("/gap-analysis", response_class=HTMLResponse)
async def gap_analysis_page(request: Request):
    return templates.TemplateResponse("gap_analysis.html", {"request": request})


@router.get("/content-gaps", response_class=HTMLResponse)
async def content_gaps_page(request: Request):
    return templates.TemplateResponse("content_gaps.html", {"request": request})


@router.get("/action-cards", response_class=HTMLResponse)
async def action_cards_page(request: Request):
    return templates.TemplateResponse("action_cards.html", {"request": request})


@router.get("/templates", response_class=HTMLResponse)
async def audit_templates_page(request: Request):
    return templates.TemplateResponse("templates.html", {"request": request})


@router.get("/tracking", response_class=HTMLResponse)
async def tracking_page(request: Request):
    return templates.TemplateResponse("tracking.html", {"request": request})


@router.get("/cross-reference", response_class=HTMLResponse)
async def cross_reference_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Cross-Reference Analysis dashboard — trigger and browse site-wide analyses."""
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
