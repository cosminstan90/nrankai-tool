"""Google integration page routes — GSC, GA4, Ads."""

from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from api.utils.errors import raise_not_found
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db
from ._shared import templates, _repair_guide_json

router = APIRouter()


@router.get("/gsc", response_class=HTMLResponse)
async def gsc_list_page(request: Request):
    """GSC properties list page."""
    return templates.TemplateResponse("gsc.html", {"request": request})


@router.get("/gsc/{property_id}/page-optimize", response_class=HTMLResponse)
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
        raise_not_found("GSC property")
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


@router.get("/gsc/{property_id}", response_class=HTMLResponse)
async def gsc_detail_page(
    request: Request,
    property_id: str,
    db: AsyncSession = Depends(get_db),
):
    """GSC property detail — queries, pages, and cross-reference tabs."""
    from api.models.database import GscProperty as GscProp
    prop = await db.get(GscProp, property_id)
    if not prop:
        raise_not_found("GSC property")
    return templates.TemplateResponse("gsc_detail.html", {
        "request":  request,
        "property": prop,
    })


@router.get("/ga4", response_class=HTMLResponse)
async def ga4_list_page(request: Request):
    """GA4 properties list page."""
    return templates.TemplateResponse("ga4.html", {"request": request})


@router.get("/ga4/{property_id}", response_class=HTMLResponse)
async def ga4_detail_page(
    request: Request,
    property_id: str,
    db: AsyncSession = Depends(get_db),
):
    """GA4 property detail — pages, channels, and cross-reference tabs."""
    from api.models.database import Ga4Property as Ga4Prop
    prop = await db.get(Ga4Prop, property_id)
    if not prop:
        raise_not_found("GA4 property")
    return templates.TemplateResponse("ga4_detail.html", {
        "request":  request,
        "property": prop,
    })


@router.get("/ads", response_class=HTMLResponse)
async def ads_list_page(request: Request):
    """Google Ads accounts list page."""
    return templates.TemplateResponse("ads.html", {"request": request})


@router.get("/ads/{account_id}", response_class=HTMLResponse)
async def ads_detail_page(
    request: Request,
    account_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Google Ads account detail — search terms, campaigns, and cross-reference tabs."""
    from api.models.database import AdsAccount as AdsAcc
    acc = await db.get(AdsAcc, account_id)
    if not acc:
        raise_not_found("Ads account")
    return templates.TemplateResponse("ads_detail.html", {
        "request": request,
        "account": acc,
    })
