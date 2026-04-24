"""Meta & Headings Generator page route."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from api.provider_registry import get_providers_for_ui
from ._shared import templates

router = APIRouter()


@router.get("/meta-generator", response_class=HTMLResponse)
async def meta_generator_page(request: Request):
    providers_ui = get_providers_for_ui()
    return templates.TemplateResponse("meta_generator.html", {
        "request": request,
        "providers_ui": providers_ui,
    })
