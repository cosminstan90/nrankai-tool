"""
Draft Page Optimizer — analyzes unpublished content before it goes live.

No URL or scraping required. The user pastes draft text (or uploads a file)
and receives SEO, GEO, content quality, readability, and E-E-A-T scores
with concrete improvement suggestions.

Endpoints
---------
POST   /api/draft-optimizer/analyze          submit draft text → start analysis
POST   /api/draft-optimizer/upload           submit via file upload (.txt, .md, .docx)
GET    /api/draft-optimizer/{id}             poll status / fetch completed result
GET    /api/draft-optimizer/                 list recent analyses
DELETE /api/draft-optimizer/{id}             delete an analysis
"""

import asyncio
import json
import os
import re
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, BackgroundTasks, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, desc

from api.models.database import AsyncSessionLocal, DraftOptimization
from api.routes.summary import call_llm_for_summary, clean_json_response
from api.routes.costs import track_cost

router = APIRouter(prefix="/api/draft-optimizer", tags=["draft_optimizer"])

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "draft_optimizer.yaml"

PAGE_TYPES = ["article", "landing", "product", "service", "faq", "guide", "category", "about", "other"]
FOCUS_AREAS = ["seo", "geo", "content_quality", "readability", "eeat"]

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class AnalyzeDraftRequest(BaseModel):
    content_text: str = Field(..., min_length=50, description="Draft content to analyze")
    title: Optional[str] = Field(None, max_length=255)
    target_keyword: Optional[str] = Field(None, max_length=255)
    page_type: str = Field("article")
    language: str = Field("Romanian")
    focus_areas: Optional[List[str]] = None
    provider: Optional[str] = None
    model: Optional[str] = None

    @field_validator("page_type")
    @classmethod
    def validate_page_type(cls, v):
        if v not in PAGE_TYPES:
            raise ValueError(f"page_type must be one of: {PAGE_TYPES}")
        return v

    @field_validator("focus_areas")
    @classmethod
    def validate_focus_areas(cls, v):
        if v:
            invalid = set(v) - set(FOCUS_AREAS)
            if invalid:
                raise ValueError(f"Invalid focus_areas: {invalid}. Allowed: {FOCUS_AREAS}")
        return v

    @field_validator("content_text")
    @classmethod
    def validate_content_length(cls, v):
        if len(v) > 100_000:
            raise ValueError("content_text exceeds 100,000 characters")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_prompt() -> dict:
    try:
        with open(_PROMPT_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return {"role": "You are a content optimization expert.", "task": "Analyze the draft and return JSON."}


def _count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _resolve_provider_model(provider: Optional[str], model: Optional[str]) -> tuple[str, str]:
    """Pick a sensible default provider/model if not specified."""
    if provider and model:
        return provider.upper(), model

    if os.getenv("ANTHROPIC_API_KEY"):
        return "ANTHROPIC", model or "claude-sonnet-4-6"
    if os.getenv("OPENAI_API_KEY"):
        return "OPENAI", model or "gpt-4o-mini"
    if os.getenv("GEMINI_API_KEY"):
        return "GOOGLE", model or "gemini-2.0-flash"
    if os.getenv("MISTRAL_API_KEY"):
        return "MISTRAL", model or "mistral-large-latest"
    raise HTTPException(status_code=503, detail="No LLM provider configured")


async def _extract_text_from_upload(file: UploadFile) -> str:
    """Extract plain text from .txt, .md, or .docx uploads."""
    filename = (file.filename or "").lower()
    content_bytes = await file.read()

    if filename.endswith(".docx"):
        try:
            import docx
            import io
            doc = docx.Document(io.BytesIO(content_bytes))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            raise HTTPException(status_code=422, detail="python-docx not installed — upload .txt or .md instead")
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not parse .docx: {e}")

    # .txt / .md — decode as UTF-8
    try:
        return content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return content_bytes.decode("latin-1")


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

async def _run_analysis(draft_id: str):
    """Run LLM analysis in the background and persist results."""
    async with AsyncSessionLocal() as db:
        draft = await db.get(DraftOptimization, draft_id)
        if not draft:
            return

        draft.status = "running"
        await db.commit()

        try:
            prompt_data = _load_prompt()
            system_prompt = f"{prompt_data.get('role', '')}\n\n{prompt_data.get('task', '')}"

            focus_label = ", ".join(draft.focus_areas) if draft.focus_areas else "all dimensions"
            user_content = (
                f"Analyze the following {draft.page_type} draft content.\n"
                f"Target keyword: {draft.target_keyword or 'not specified'}\n"
                f"Language: {draft.language}\n"
                f"Focus areas: {focus_label}\n\n"
                f"---DRAFT CONTENT START---\n{draft.content_text}\n---DRAFT CONTENT END---"
            )

            raw_text, in_tok, out_tok = await call_llm_for_summary(
                provider=draft.provider,
                model=draft.model,
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=6000,
            )

            cleaned = clean_json_response(raw_text)
            result = json.loads(cleaned)

            scores = result.get("scores", {})
            draft.score_overall    = scores.get("overall")
            draft.score_seo        = scores.get("seo")
            draft.score_geo        = scores.get("geo")
            draft.score_content    = scores.get("content_quality")
            draft.score_readability= scores.get("readability")
            draft.score_eeat       = scores.get("eeat")
            draft.result_json      = result
            draft.status           = "completed"
            draft.completed_at     = datetime.now(timezone.utc)

            await db.commit()

            await track_cost(
                source="draft_optimizer",
                provider=draft.provider,
                model=draft.model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                source_id=draft_id,
            )

        except Exception as e:
            async with AsyncSessionLocal() as err_db:
                err_draft = await err_db.get(DraftOptimization, draft_id)
                if err_draft:
                    err_draft.status = "failed"
                    err_draft.error_message = str(e)
                    await err_db.commit()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/analyze", status_code=202)
async def analyze_draft(payload: AnalyzeDraftRequest, background_tasks: BackgroundTasks):
    """Submit draft text for analysis. Returns immediately with an id to poll."""
    provider, model = _resolve_provider_model(payload.provider, payload.model)
    word_count = _count_words(payload.content_text)

    async with AsyncSessionLocal() as db:
        draft = DraftOptimization(
            title=payload.title or f"Draft — {(payload.target_keyword or payload.content_text[:40]).strip()}",
            content_text=payload.content_text,
            target_keyword=payload.target_keyword,
            page_type=payload.page_type,
            language=payload.language,
            focus_areas=payload.focus_areas or FOCUS_AREAS,
            provider=provider,
            model=model,
            word_count=word_count,
            status="pending",
        )
        db.add(draft)
        await db.commit()
        await db.refresh(draft)
        draft_id = draft.id

    background_tasks.add_task(_run_analysis, draft_id)
    return {"id": draft_id, "status": "pending", "word_count": word_count}


@router.post("/upload", status_code=202)
async def upload_and_analyze(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    target_keyword: Optional[str] = Form(None),
    page_type: str = Form("article"),
    language: str = Form("Romanian"),
    focus_areas: Optional[str] = Form(None),  # comma-separated
    provider: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
):
    """Upload a .txt, .md, or .docx file and run optimization analysis."""
    allowed_ext = {".txt", ".md", ".docx"}
    ext = Path(file.filename or "").suffix.lower()
    if ext not in allowed_ext:
        raise HTTPException(status_code=422, detail=f"Unsupported file type '{ext}'. Use .txt, .md, or .docx")

    content_text = await _extract_text_from_upload(file)
    if len(content_text.strip()) < 50:
        raise HTTPException(status_code=422, detail="Extracted content is too short (min 50 characters)")

    areas = [a.strip() for a in focus_areas.split(",")] if focus_areas else FOCUS_AREAS
    resolved_provider, resolved_model = _resolve_provider_model(provider, model)

    async with AsyncSessionLocal() as db:
        draft = DraftOptimization(
            title=file.filename or "Uploaded draft",
            content_text=content_text,
            target_keyword=target_keyword,
            page_type=page_type if page_type in PAGE_TYPES else "article",
            language=language,
            focus_areas=areas,
            provider=resolved_provider,
            model=resolved_model,
            word_count=_count_words(content_text),
            status="pending",
        )
        db.add(draft)
        await db.commit()
        await db.refresh(draft)
        draft_id = draft.id

    background_tasks.add_task(_run_analysis, draft_id)
    return {"id": draft_id, "status": "pending", "filename": file.filename}


@router.get("/{draft_id}")
async def get_draft_result(draft_id: str):
    """Poll status or fetch completed analysis result."""
    async with AsyncSessionLocal() as db:
        draft = await db.get(DraftOptimization, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft analysis not found")
    return draft.to_dict()


@router.get("/")
async def list_drafts(limit: int = 50):
    """List recent draft analyses."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(DraftOptimization).order_by(desc(DraftOptimization.created_at)).limit(min(limit, 200))
        )
        drafts = result.scalars().all()
    return [
        {
            "id": d.id,
            "title": d.title,
            "target_keyword": d.target_keyword,
            "page_type": d.page_type,
            "status": d.status,
            "score_overall": d.score_overall,
            "word_count": d.word_count,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in drafts
    ]


@router.delete("/{draft_id}", status_code=204)
async def delete_draft(draft_id: str):
    """Delete a draft analysis."""
    async with AsyncSessionLocal() as db:
        draft = await db.get(DraftOptimization, draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft analysis not found")
        await db.delete(draft)
        await db.commit()
