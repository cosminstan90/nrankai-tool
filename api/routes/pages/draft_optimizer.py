"""Draft Optimizer page view."""

import os

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, DraftOptimization
from api.provider_registry import get_providers_for_ui
from ._shared import templates

router = APIRouter()


@router.get("/draft-optimizer", response_class=HTMLResponse)
async def draft_optimizer_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Draft Page Optimizer — analyze content before publishing."""
    result = await db.execute(
        select(DraftOptimization).order_by(desc(DraftOptimization.created_at)).limit(30)
    )
    drafts = result.scalars().all()

    providers = {
        "google":     bool(os.getenv("GEMINI_API_KEY")),
        "anthropic":  bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai":     bool(os.getenv("OPENAI_API_KEY")),
        "mistral":    bool(os.getenv("MISTRAL_API_KEY")),
    }

    return templates.TemplateResponse("draft_optimizer.html", {
        "request":  request,
        "drafts":   [d.to_dict() for d in drafts],
        "providers": providers,
        "providers_ui": get_providers_for_ui(),
    })
