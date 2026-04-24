"""Tool/generator page routes — schema, keyword research, optimize, llms.txt, citations, guide."""

import os
import json as _json

from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from api.utils.errors import raise_not_found
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, Audit, KeywordSession
from api.provider_registry import get_providers_for_ui
from ._shared import templates, _repair_guide_json

router = APIRouter()


@router.get("/schema", response_class=HTMLResponse)
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


@router.get("/keyword-research", response_class=HTMLResponse)
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


@router.get("/keyword-research/{session_id}", response_class=HTMLResponse)
async def keyword_research_detail_page(
    request: Request, session_id: str, db: AsyncSession = Depends(get_db)
):
    """Keyword research detail — two-panel keyword + question viewer."""
    from api.models.database import KeywordResult as KWResult
    session = await db.get(KeywordSession, session_id)
    if not session:
        raise_not_found("Session")

    # Load all keyword results for this session
    kw_rows = (await db.execute(
        select(KWResult)
        .where(KWResult.session_id == session_id)
        .order_by(KWResult.search_volume.desc().nullslast(), KWResult.keyword)
    )).scalars().all()

    keywords_json = _json.dumps([
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


@router.get("/optimize", response_class=HTMLResponse)
async def standalone_optimize_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Standalone page optimizer — enter any URL + keywords without needing a GSC property."""
    from api.models.database import UrlGuide as _UrlGuide
    past_guides_rows = (await db.execute(
        select(_UrlGuide)
        .where(
            _UrlGuide.gsc_property_id.is_(None),
            _UrlGuide.status == "completed",
        )
        .order_by(desc(_UrlGuide.created_at))
        .limit(50)
    )).scalars().all()

    past_guides = []
    for g in past_guides_rows:
        # Only include metadata — guide_json is fetched on demand via /api/guide/{id}
        past_guides.append({
            "id":         g.id,
            "url":        g.url,
            "provider":   g.provider,
            "model":      g.model,
            "reviewed":   bool(g.reviewed),
            "created_at": g.created_at.strftime("%Y-%m-%d %H:%M") if g.created_at else None,
            "guide_json": None,
        })

    return templates.TemplateResponse("optimize_standalone.html", {
        "request":     request,
        "past_guides": past_guides,
    })


@router.get("/llms-txt", response_class=HTMLResponse)
async def llms_txt_page(request: Request):
    """llms.txt generator page."""
    return templates.TemplateResponse("llms_txt.html", {"request": request})


@router.get("/citations", response_class=HTMLResponse)
async def citation_tracker_page(request: Request):
    from api.provider_registry import get_available_providers
    available = get_available_providers()
    return templates.TemplateResponse("citation_tracker.html", {
        "request": request,
        "gemini_available": available.get("google", False),
    })


@router.get("/guide/{page_url:path}", response_class=HTMLResponse)
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
