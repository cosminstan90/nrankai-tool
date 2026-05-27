"""SerpIQ page routes."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._shared import templates

router = APIRouter()


@router.get("/serpiq", response_class=HTMLResponse)
async def serpiq_page(request: Request):
    """SerpIQ — SERP Snapshot & Instant Analysis tool."""
    return templates.TemplateResponse("serpiq/index.html", {"request": request})
