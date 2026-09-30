"""
Unified recommendations page -- Pasii 12-18 of docs/superpowers/plans/2026-09-30-next-steps.md.

One page rather than six separate ones (each of pasii 12-17's recommendation
engines has its own inputs and, until now, no UI at all): JS-visibility,
GSC opportunities (weak-CTR + striking-distance), internal links, content
decay, citation comparison, and Fan-Out coverage gaps -- plus the Pasul 18
learning loop (save a recommendation as an action card, mark it applied,
see the before/after report). All read-only analysis calls the existing
GET endpoints directly from the browser; only "save as action" and "mark
applied" write anything.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._shared import templates

router = APIRouter()


@router.get("/recommendations", response_class=HTMLResponse)
async def recommendations_page(request: Request):
    return templates.TemplateResponse(request, "recommendations.html", {"request": request})
