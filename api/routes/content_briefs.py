"""
Content Brief Generator for Website LLM Analyzer.

Generates actionable content briefs for pages with low scores based on audit results.
Each brief provides specific content changes, SEO requirements, and GEO optimization recommendations.
"""

import asyncio
import json
import os
import yaml
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from api.utils.errors import raise_not_found
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import AsyncSessionLocal, Audit, AuditResult, ContentBrief, SchemaMarkup
from api.routes.summary import call_llm_for_summary, clean_json_response
from api.routes.costs import track_cost

router = APIRouter(prefix="/api/briefs", tags=["content_briefs"])


# ============================================================================
# Pydantic Schemas
# ============================================================================

class GenerateBriefsRequest(BaseModel):
    """Request to generate content briefs for an audit."""
    audit_id: str
    page_ids: Optional[List[int]] = None
    max_pages: int = 10
    score_threshold: int = 70
    provider: Optional[str] = None
    model: Optional[str] = None
    language: str = "Romanian"
    focus_areas: Optional[List[str]] = None

    @field_validator("max_pages")
    @classmethod
    def validate_max_pages(cls, v):
        if v < 1 or v > 50:
            raise ValueError("max_pages must be between 1 and 50")
        return v

    @field_validator("score_threshold")
    @classmethod
    def validate_score_threshold(cls, v):
        if not (1 <= v <= 100):
            raise ValueError("score_threshold must be between 1 and 100")
        return v

    @field_validator("focus_areas")
    @classmethod
    def validate_focus_areas(cls, v):
        if v is not None:
            allowed = {"seo", "content_quality", "geo_readiness", "technical", "ux"}
            invalid = set(v) - allowed
            if invalid:
                raise ValueError(f"Invalid focus areas: {invalid}")
        return v


class SinglePageBriefRequest(BaseModel):
    """Request to generate a brief for one specific page on demand."""
    audit_id: str
    page_url: str
    provider: Optional[str] = None
    model: Optional[str] = None
    language: str = "English"
    focus_areas: Optional[List[str]] = None


class UpdateBriefStatusRequest(BaseModel):
    """Request to update brief status."""
    status: str
    
    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        allowed = {"generated", "approved", "in_progress", "completed", "failed"}
        if v not in allowed:
            raise ValueError(f"Status must be one of: {allowed}")
        return v


class RegenerateBriefRequest(BaseModel):
    """Request to regenerate a brief with different parameters."""
    provider: Optional[str] = None
    model: Optional[str] = None
    language: Optional[str] = None
    focus_areas: Optional[List[str]] = None


class GenerateFAQRequest(BaseModel):
    """Request to generate FAQ suggestions for a brief page."""
    provider: Optional[str] = None
    model: Optional[str] = None
    language: str = "Romanian"
    num_questions: int = Field(default=5, ge=3, le=15)


# ============================================================================
# Helper Functions
# ============================================================================

# ── Content brief prompt: module-level YAML cache ────────────────────────────
_BRIEF_PROMPT_CFG: Dict = {}


def _load_brief_prompt_cfg() -> Dict:
    """Load (and cache) prompts/content_brief.yaml."""
    global _BRIEF_PROMPT_CFG
    if _BRIEF_PROMPT_CFG:
        return _BRIEF_PROMPT_CFG
    yaml_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "prompts", "content_brief.yaml",
    )
    with open(yaml_path, "r", encoding="utf-8") as fh:
        _BRIEF_PROMPT_CFG = yaml.safe_load(fh)
    return _BRIEF_PROMPT_CFG


def build_brief_system_prompt(audit_type: str, language: str, focus_areas: Optional[List[str]] = None) -> str:
    """
    Build system prompt for brief generation based on audit type.

    Prompt text is loaded from prompts/content_brief.yaml so it can be
    edited without touching Python code.

    Args:
        audit_type: Type of audit (SEO, GEO, CONTENT_QUALITY, etc.)
        language: Target language for recommendations
        focus_areas: Optional list of specific areas to focus on

    Returns:
        Assembled system prompt string
    """
    cfg = _load_brief_prompt_cfg()

    # ── Base prompt + JSON schema ─────────────────────────────────────────────
    prompt = cfg["base_prompt"].rstrip().format(audit_type=audit_type)
    prompt += "\n" + cfg["json_schema"].rstrip()

    # ── Audit-type specific FOCUS ON section ──────────────────────────────────
    atype_upper = audit_type.upper()
    if atype_upper in ("SEO", "SEO_AUDIT"):
        focus_key = "SEO"
    elif "GEO" in atype_upper:
        focus_key = "GEO"
    elif atype_upper == "CONTENT_QUALITY":
        focus_key = "CONTENT_QUALITY"
    else:
        focus_key = None

    focus_items = (
        cfg["focus_by_type"].get(focus_key, cfg["default_focus"])
        if focus_key
        else cfg["default_focus"]
    )
    prompt += "\n\nFOCUS ON:\n" + "\n".join(f"- {item}" for item in focus_items)

    # ── Language requirement (non-English only) ───────────────────────────────
    if language != "English":
        prompt += "\n\n" + cfg["language_note"].rstrip().format(language=language)

    # ── User-specified focus area prioritisation ──────────────────────────────
    if focus_areas:
        prompt += f"\n\nPrioritize these focus areas: {', '.join(focus_areas)}"

    return prompt


async def load_page_content(audit_id: str, filename: str, max_chars: int = 3000) -> str:
    """
    Load page content from input_llm directory.
    
    Args:
        audit_id: Audit ID
        filename: Result filename
        max_chars: Maximum characters to load
    
    Returns:
        Page content or empty string if not found
    """
    try:
        # Construct path to input_llm file
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        input_dir = os.path.join(base_dir, "audits", audit_id, "input_llm")
        
        # Try different extensions
        for ext in [".txt", ""]:
            file_path = os.path.join(input_dir, f"{filename}{ext}")
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read(max_chars)
                return content
        
        return ""
    except Exception as e:
        print(f"Error loading page content: {e}")
        return ""


def determine_priority(score: int) -> str:
    """Determine priority based on score."""
    if score < 50:
        return "critical"
    elif score < 65:
        return "high"
    elif score < 80:
        return "medium"
    else:
        return "low"


async def generate_single_brief(
    db: AsyncSession,
    audit: Audit,
    result: AuditResult,
    provider: str,
    model: str,
    language: str,
    focus_areas: Optional[List[str]] = None
) -> Optional[ContentBrief]:
    """
    Generate a content brief for a single page.
    
    Args:
        db: Database session
        audit: Audit object
        result: AuditResult object
        provider: LLM provider
        model: LLM model
        language: Target language
        focus_areas: Optional focus areas
    
    Returns:
        ContentBrief object or None if generation failed
    """
    try:
        # Parse result JSON
        result_data = {}
        if result.result_json:
            try:
                result_data = json.loads(result.result_json)
            except json.JSONDecodeError:
                result_data = {}
        
        # Load page content
        page_content = await load_page_content(audit.id, result.filename)
        
        # Build system prompt
        system_prompt = build_brief_system_prompt(audit.audit_type, language, focus_areas)
        
        # Build user content
        optimization_opportunities = result_data.get("optimization_opportunities", [])
        issues_text = "\n".join([f"- {opp}" for opp in optimization_opportunities])
        
        user_content = f"""Generate a content brief for this page:

URL: {result.page_url}
Current Score: {result.score}/100
Classification: {result.classification}
Audit Type: {audit.audit_type}

Key Issues Identified:
{issues_text if issues_text else "No specific issues listed"}

Current Page Content (truncated to 3000 chars):
{page_content if page_content else "[Content not available]"}

Generate a detailed content brief with specific, actionable recommendations."""
        
        # Call LLM
        response_text = await call_llm_for_summary(
            provider=provider,
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            max_tokens=8192
        )

        # Track cost (approximate token estimate: ~4 chars per token)
        input_tokens = max((len(system_prompt) + len(user_content)) // 4, 100)
        output_tokens = max(len(response_text) // 4, 50)
        asyncio.create_task(track_cost(
            source="brief",
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            audit_id=audit.id,
            website=audit.website
        ))

        # Clean and parse JSON
        cleaned_json = clean_json_response(response_text)
        brief_data = json.loads(cleaned_json)
        
        # Ensure required fields
        brief_data.setdefault("page_url", result.page_url)
        brief_data.setdefault("current_score", result.score)
        
        # Auto-determine priority if not provided
        if "priority" not in brief_data:
            brief_data["priority"] = determine_priority(result.score or 50)
        
        # Create ContentBrief
        content_brief = ContentBrief(
            audit_id=audit.id,
            result_id=result.id,
            page_url=result.page_url,
            brief_json=json.dumps(brief_data, ensure_ascii=False),
            status="generated",
            priority=brief_data["priority"],
            provider=provider,
            model=model
        )
        
        db.add(content_brief)
        await db.commit()
        await db.refresh(content_brief)
        
        return content_brief
    
    except Exception as e:
        print(f"Error generating brief for {result.page_url}: {e}")
        
        # Create failed brief
        failed_brief = ContentBrief(
            audit_id=audit.id,
            result_id=result.id,
            page_url=result.page_url,
            brief_json=json.dumps({"error": str(e)}, ensure_ascii=False),
            status="failed",
            priority="medium",
            provider=provider,
            model=model
        )
        
        db.add(failed_brief)
        await db.commit()
        
        return None


async def select_pages_for_briefs(
    db: AsyncSession,
    audit_id: str,
    page_ids: Optional[List[int]] = None,
    max_pages: int = 10,
    score_threshold: int = 70,
) -> List[AuditResult]:
    """
    Select pages that need briefs — guaranteed unique by page_url.

    Each physical page URL appears at most once, even when multiple audit
    types produced separate AuditResult rows for it.  Pages that already
    have a non-failed brief are skipped.

    If page_ids provided, use those. Otherwise auto-select low-scoring pages
    below *score_threshold*, then fill up with pages below
    (score_threshold + 15) if needed.
    """
    # ── 0. URLs that already have a usable brief ───────────────────────────
    existing_q = await db.execute(
        select(ContentBrief.page_url).where(
            and_(
                ContentBrief.audit_id == audit_id,
                ContentBrief.status != "failed",
            )
        )
    )
    already_briefed: set = {r[0] for r in existing_q.fetchall()}

    if page_ids:
        # ── Path A: caller supplied explicit result IDs ────────────────────
        result = await db.execute(
            select(AuditResult).where(
                and_(
                    AuditResult.audit_id == audit_id,
                    AuditResult.id.in_(page_ids),
                    AuditResult.result_json.isnot(None),
                )
            ).order_by(AuditResult.score.asc())
        )
        all_rows = list(result.scalars().all())

        seen: set = set()
        deduped = []
        for r in all_rows:
            if r.page_url not in seen and r.page_url not in already_briefed:
                seen.add(r.page_url)
                deduped.append(r)
        return deduped[:max_pages]

    else:
        # ── Path B: auto-select by score ───────────────────────────────────
        # Fetch without LIMIT first so we can deduplicate across audit types
        result_low = await db.execute(
            select(AuditResult).where(
                and_(
                    AuditResult.audit_id == audit_id,
                    AuditResult.score.isnot(None),
                    AuditResult.score < score_threshold,
                    AuditResult.result_json.isnot(None),
                )
            ).order_by(AuditResult.score.asc())
        )

        seen: set = set()
        pages = []
        for r in result_low.scalars().all():
            if r.page_url not in seen and r.page_url not in already_briefed:
                seen.add(r.page_url)
                pages.append(r)
                if len(pages) >= max_pages:
                    break

        # Fill remaining slots from the next score band if needed
        if len(pages) < max_pages:
            fill_threshold = min(score_threshold + 15, 100)
            result_medium = await db.execute(
                select(AuditResult).where(
                    and_(
                        AuditResult.audit_id == audit_id,
                        AuditResult.score.isnot(None),
                        AuditResult.score >= score_threshold,
                        AuditResult.score < fill_threshold,
                        AuditResult.result_json.isnot(None),
                    )
                ).order_by(AuditResult.score.asc())
            )
            for r in result_medium.scalars().all():
                if r.page_url not in seen and r.page_url not in already_briefed:
                    seen.add(r.page_url)
                    pages.append(r)
                    if len(pages) >= max_pages:
                        break

        return pages


async def background_generate_briefs(
    audit_id: str,
    page_ids: Optional[List[int]],
    max_pages: int,
    provider: str,
    model: str,
    language: str,
    focus_areas: Optional[List[str]],
    score_threshold: int = 70,
):
    """Background task to generate briefs for multiple pages."""
    async with AsyncSessionLocal() as db:
        try:
            # Load audit
            audit_result = await db.execute(
                select(Audit).where(Audit.id == audit_id)
            )
            audit = audit_result.scalar_one_or_none()

            if not audit:
                print(f"Audit {audit_id} not found")
                return

            # Select pages
            pages = await select_pages_for_briefs(
                db, audit_id, page_ids, max_pages, score_threshold
            )
            
            if not pages:
                print(f"No pages found for audit {audit_id}")
                return
            
            print(f"Generating briefs for {len(pages)} pages...")
            
            # Generate briefs sequentially (to avoid rate limits)
            for page in pages:
                await generate_single_brief(
                    db=db,
                    audit=audit,
                    result=page,
                    provider=provider,
                    model=model,
                    language=language,
                    focus_areas=focus_areas
                )
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(1)
            
            print(f"Completed brief generation for audit {audit_id}")
        
        except Exception as e:
            print(f"Error in background brief generation: {e}")


# ============================================================================
# API Endpoints
# ============================================================================

@router.post("/generate")
async def generate_briefs(
    request: GenerateBriefsRequest,
    background_tasks: BackgroundTasks
):
    """
    Generate content briefs for an audit.
    
    Runs in background. Generates briefs for specified pages or auto-selects
    top low-scoring pages.
    """
    async with AsyncSessionLocal() as db:
        # Verify audit exists and is completed
        audit_result = await db.execute(
            select(Audit).where(Audit.id == request.audit_id)
        )
        audit = audit_result.scalar_one_or_none()
        
        if not audit:
            raise_not_found("Audit")
        
        if audit.status != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Audit must be completed (current status: {audit.status})"
            )
        
        # Determine provider/model
        provider = request.provider or audit.provider
        model = request.model or audit.model
        
        # If using default and provider is Anthropic, use cheapest model
        if not request.model and provider.upper() == "ANTHROPIC":
            model = "claude-haiku-4-5-20251001"
        elif not request.model and provider.upper() == "OPENAI":
            model = "gpt-4o-mini"
        
        # Start background task
        background_tasks.add_task(
            background_generate_briefs,
            audit_id=request.audit_id,
            page_ids=request.page_ids,
            max_pages=request.max_pages,
            provider=provider,
            model=model,
            language=request.language,
            focus_areas=request.focus_areas,
            score_threshold=request.score_threshold,
        )

        return {
            "status": "started",
            "audit_id": request.audit_id,
            "message": "Brief generation started in background",
            "max_pages": request.max_pages,
            "score_threshold": request.score_threshold,
            "provider": provider,
            "model": model,
        }


@router.post("/generate-page", status_code=201)
async def generate_page_brief(request: SinglePageBriefRequest):
    """
    Generate a brief for a single specific page on demand (synchronous).

    Looks up the lowest-scoring AuditResult for the given page_url within
    the audit, generates the brief immediately, and returns it.
    """
    async with AsyncSessionLocal() as db:
        audit = await db.get(Audit, request.audit_id)
        if not audit:
            raise_not_found("Audit")
        if audit.status != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Audit must be completed (current status: {audit.status})",
            )

        # Find the best AuditResult for this URL (lowest score wins)
        result_q = await db.execute(
            select(AuditResult).where(
                and_(
                    AuditResult.audit_id == request.audit_id,
                    AuditResult.page_url == request.page_url,
                    AuditResult.result_json.isnot(None),
                )
            ).order_by(AuditResult.score.asc())
        )
        audit_result = result_q.scalars().first()
        if not audit_result:
            raise HTTPException(
                status_code=404,
                detail=f"No audit result found for '{request.page_url}'",
            )

        # Resolve provider / model
        provider = request.provider or audit.provider or "anthropic"
        model    = request.model    or audit.model    or "claude-haiku-4-5-20251001"

        brief = await generate_single_brief(
            db=db,
            audit=audit,
            result=audit_result,
            provider=provider,
            model=model,
            language=request.language,
            focus_areas=request.focus_areas,
        )

        if not brief or brief.status == "failed":
            raise HTTPException(status_code=500, detail="Brief generation failed")

        brief_data = json.loads(brief.brief_json) if brief.brief_json else {}
        return {
            "id":       brief.id,
            "page_url": brief.page_url,
            "status":   brief.status,
            "priority": brief.priority,
            "provider": brief.provider,
            "model":    brief.model,
            "brief":    brief_data,
        }


@router.get("")
async def list_briefs(
    audit_id: Optional[str] = Query(None, description="Audit ID to filter briefs")
):
    """List all briefs, optionally filtered by audit."""
    async with AsyncSessionLocal() as db:
        query = select(ContentBrief).order_by(
            ContentBrief.priority.desc(),
            ContentBrief.created_at.desc()
        )
        
        if audit_id:
            query = query.where(ContentBrief.audit_id == audit_id)
        
        result = await db.execute(query)
        briefs = result.scalars().all()
        
        return {
            "audit_id": audit_id,
            "total": len(briefs),
            "briefs": [brief.to_dict() for brief in briefs]
        }


@router.get("/{brief_id}")
async def get_brief(brief_id: int):
    """Get a single brief by ID."""
    async with AsyncSessionLocal() as db:
        query = select(ContentBrief).where(ContentBrief.id == brief_id)
        result = await db.execute(query)
        brief = result.scalar_one_or_none()
        
        if not brief:
            raise_not_found("Brief")
        
        return brief.to_dict()


@router.patch("/{brief_id}")
async def update_brief_status(
    brief_id: int,
    request: UpdateBriefStatusRequest
):
    """Update brief status."""
    async with AsyncSessionLocal() as db:
        query = select(ContentBrief).where(ContentBrief.id == brief_id)
        result = await db.execute(query)
        brief = result.scalar_one_or_none()
        
        if not brief:
            raise_not_found("Brief")
        
        brief.status = request.status
        await db.commit()
        await db.refresh(brief)
        
        return brief.to_dict()


@router.post("/{brief_id}/regenerate")
async def regenerate_brief(
    brief_id: int,
    request: RegenerateBriefRequest,
    background_tasks: BackgroundTasks
):
    """Regenerate a brief with different parameters."""
    async with AsyncSessionLocal() as db:
        # Get existing brief
        query = select(ContentBrief).where(ContentBrief.id == brief_id)
        result = await db.execute(query)
        brief = result.scalar_one_or_none()
        
        if not brief:
            raise_not_found("Brief")
        
        # Get audit and result
        audit_query = select(Audit).where(Audit.id == brief.audit_id)
        audit_result = await db.execute(audit_query)
        audit = audit_result.scalar_one_or_none()
        
        result_query = select(AuditResult).where(AuditResult.id == brief.result_id)
        result_result = await db.execute(result_query)
        page_result = result_result.scalar_one_or_none()
        
        if not audit or not page_result:
            raise_not_found("Audit or result")
        
        # Determine parameters
        provider = request.provider or brief.provider
        model = request.model or brief.model
        language = request.language or "Romanian"
        
        # Delete old brief
        await db.delete(brief)
        await db.commit()
        
        # Generate new brief
        async def regenerate_task():
            async with AsyncSessionLocal() as task_db:
                await generate_single_brief(
                    db=task_db,
                    audit=audit,
                    result=page_result,
                    provider=provider,
                    model=model,
                    language=language,
                    focus_areas=request.focus_areas
                )
        
        background_tasks.add_task(regenerate_task)
        
        return {
            "status": "started",
            "brief_id": brief_id,
            "message": "Brief regeneration started in background"
        }


@router.post("/{brief_id}/faq")
async def generate_faq(brief_id: int, request: GenerateFAQRequest):
    """
    Generate FAQ suggestions for a brief's page and, if a FAQPage schema
    already exists for that URL, review each existing question/answer pair.

    Result is stored inside brief_json under the 'faq_analysis' key.
    """
    async with AsyncSessionLocal() as db:
        brief_q = await db.execute(select(ContentBrief).where(ContentBrief.id == brief_id))
        brief = brief_q.scalar_one_or_none()
        if not brief:
            raise_not_found("Brief")

        brief_data = json.loads(brief.brief_json)
        page_url = brief.page_url

        provider = request.provider or brief.provider
        model = request.model or brief.model

        # Check for an existing FAQPage JSON-LD schema for this URL
        schema_q = await db.execute(
            select(SchemaMarkup)
            .where(SchemaMarkup.page_url == page_url)
            .where(SchemaMarkup.schema_type == "FAQPage")
            .order_by(SchemaMarkup.created_at.desc())
        )
        faq_schema = schema_q.scalar_one_or_none()

        # ── Build system prompt ───────────────────────────────────────────────
        review_instruction = ""
        if faq_schema:
            review_instruction = f"""
Also review the EXISTING FAQPage schema provided below. For each Q&A pair evaluate:
- status: "good" | "needs_improvement" | "poor"
- issue: what is wrong (null if good)
- improved_answer: a better answer (null if good)

"existing_faq_review" must contain one entry per existing Q&A."""

        system_prompt = f"""You are a content strategist specialising in FAQ optimisation for SEO and GEO \
(Generative Engine Optimization).

Your task:
1. Suggest {request.num_questions} FAQ questions that SHOULD appear on the given page to improve its \
discoverability by AI assistants and search engines.
2. For each question provide a clear, complete answer (1-3 sentences).{review_instruction}

CRITICAL: Return ONLY valid JSON — no markdown fences, no extra text.

JSON structure:
{{
  "suggested_faqs": [
    {{
      "question": "string",
      "answer": "string",
      "rationale": "string (why this Q matters for the page)"
    }}
  ],
  "existing_faq_review": [],
  "faq_summary": "string (overall assessment + recommendations)"
}}

Write ALL text content in {request.language}."""

        # ── Build user content ────────────────────────────────────────────────
        page_summary = brief_data.get("executive_summary", "No summary available.")
        keywords = brief_data.get("seo_requirements", {}).get("target_keywords", [])

        user_content = f"""Page URL: {page_url}
Page Summary: {page_summary}
Target Keywords: {', '.join(keywords) if keywords else 'not specified'}

Generate {request.num_questions} FAQ questions that should appear on this page."""

        if faq_schema:
            existing_json = json.loads(faq_schema.schema_json)
            main_entity = existing_json.get("mainEntity", [])
            user_content += f"""

EXISTING FAQPage schema to review:
{json.dumps(main_entity, ensure_ascii=False, indent=2)}

Review every question/answer and fill in "existing_faq_review"."""

        # ── Call LLM ─────────────────────────────────────────────────────────
        response_text = await call_llm_for_summary(
            provider=provider,
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            max_tokens=3000
        )

        input_tokens = max((len(system_prompt) + len(user_content)) // 4, 100)
        output_tokens = max(len(response_text) // 4, 50)
        asyncio.create_task(track_cost(
            source="brief",
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            audit_id=brief.audit_id,
            website=page_url
        ))

        # ── Parse & save ──────────────────────────────────────────────────────
        cleaned = clean_json_response(response_text)
        faq_data = json.loads(cleaned)
        faq_data["generated_at"] = datetime.now(timezone.utc).isoformat()
        faq_data["has_existing_schema"] = faq_schema is not None

        brief_data["faq_analysis"] = faq_data
        brief.brief_json = json.dumps(brief_data, ensure_ascii=False)
        await db.commit()

        return {"brief_id": brief_id, "faq_analysis": faq_data}


@router.get("/export/{audit_id}")
async def export_briefs(audit_id: str):
    """Export all briefs for an audit as JSON."""
    async with AsyncSessionLocal() as db:
        # Verify audit exists
        audit_query = select(Audit).where(Audit.id == audit_id)
        audit_result = await db.execute(audit_query)
        audit = audit_result.scalar_one_or_none()
        
        if not audit:
            raise_not_found("Audit")
        
        # Get all briefs
        query = select(ContentBrief).where(
            ContentBrief.audit_id == audit_id
        ).order_by(
            ContentBrief.priority.desc(),
            ContentBrief.created_at.asc()
        )
        
        result = await db.execute(query)
        briefs = result.scalars().all()
        
        # Build export data
        export_data = {
            "audit_id": audit_id,
            "website": audit.website,
            "audit_type": audit.audit_type,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "total_briefs": len(briefs),
            "briefs": [brief.to_dict() for brief in briefs]
        }
        
        return export_data
