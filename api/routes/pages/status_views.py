"""System status page -- Pasul 7 of docs/superpowers/plans/2026-09-30-next-steps.md."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._shared import templates

router = APIRouter()


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    return templates.TemplateResponse(request, "status.html", {"request": request})
