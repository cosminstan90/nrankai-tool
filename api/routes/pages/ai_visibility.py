"""AI Visibility Dashboard page route."""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db
from ._shared import templates

router = APIRouter()


@router.get("/ai-visibility", response_class=HTMLResponse)
async def ai_visibility_page(request: Request, db: AsyncSession = Depends(get_db)):
    """AI Visibility Dashboard — combines GeoMonitor + CitationTracker data."""
    return templates.TemplateResponse("ai_visibility.html", {
        "request": request,
    })
