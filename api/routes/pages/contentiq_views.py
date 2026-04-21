"""ContentIQ page routes."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from ._shared import templates

router = APIRouter()


@router.get("/contentiq", response_class=HTMLResponse)
async def contentiq_page(request: Request):
    """ContentIQ — content audit engine dashboard."""
    return templates.TemplateResponse("contentiq.html", {"request": request})


@router.get("/contentiq/demo", response_class=HTMLResponse)
async def contentiq_demo_page(request: Request):
    """ContentIQ public demo — no auth required."""
    return templates.TemplateResponse("contentiq_demo.html", {"request": request})
