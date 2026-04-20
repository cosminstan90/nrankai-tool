"""Projects page routes (Prompt 25)."""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db
from ._shared import templates

router = APIRouter()


@router.get("/projects", response_class=HTMLResponse)
async def projects_list_page(request: Request):
    """Fan-Out Projects grid page."""
    return templates.TemplateResponse("projects.html", {
        "request": request,
        "page": "list",
    })


@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_dashboard_page(project_id: str, request: Request):
    """Fan-Out Project dashboard page."""
    return templates.TemplateResponse("projects.html", {
        "request": request,
        "page": "dashboard",
        "project_id": project_id,
    })
