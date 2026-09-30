"""
GET /api/js-visibility -- Pasul 12 of docs/superpowers/plans/2026-09-30-next-steps.md.

Pages sorted by how much of their content is invisible to a non-rendering AI
crawler (GPTBot/ClaudeBot/PerplexityBot -- see core/js_visibility.py's module
docstring for the cited evidence). Backend only this session, matching the
Pasul 9/10 split: a UI table is separate follow-up work.
"""
import glob
import os

from fastapi import APIRouter, Query

from api.utils.url_validator import sanitize_website_for_path
from core.js_visibility import load_js_visibility_facts, raw_text_sidecar_path

router = APIRouter(prefix="/api/js-visibility", tags=["js-visibility"])


@router.get("/pages")
async def list_pages(website: str = Query(..., min_length=1)):
    """
    Every measured page for a site, worst (lowest text_visibility_ratio)
    first. Pages never measured (no sidecar yet -- run an audit first, or
    wait for the backfill step on a GEO_AUDIT/AI_OVERVIEW_OPTIMIZATION run)
    are not included here at all; there is no "unmeasured" placeholder row,
    since a ranked list has nothing meaningful to rank an unmeasured page by.
    """
    html_dir = os.path.join(sanitize_website_for_path(website), "input_html")

    pages = []
    if os.path.isdir(html_dir):
        for html_path in glob.glob(os.path.join(html_dir, "*.html")):
            facts = load_js_visibility_facts(html_path)
            if facts and facts.get("measured"):
                pages.append({
                    "file": os.path.basename(html_path),
                    **facts,
                })

    pages.sort(key=lambda p: (p.get("text_visibility_ratio") is None, p.get("text_visibility_ratio", 1.0)))

    return {
        "website": website,
        "pages_measured": len(pages),
        "pages": pages,
    }
