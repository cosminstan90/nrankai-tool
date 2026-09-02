"""
Action Cards Routes - Transform audit results into actionable todo items.

Generates simple, concrete action cards that non-technical clients can implement.
Each card contains 3-5 specific actions with exact text to implement.
"""

import json
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import List, Optional
import asyncio

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Response
from api.utils.errors import raise_not_found, raise_bad_request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from pydantic import BaseModel, Field

from api.models.database import (
    get_db, AsyncSessionLocal, ActionCard, Audit, AuditResult, ContentBrief, SchemaMarkup
)

# Import LLM helper (same as other routes)
from api.routes.summary import call_llm_for_summary, clean_json_response

router = APIRouter(prefix="/api/action-cards", tags=["action-cards"])


# ==================== JSON Repair ====================

def _repair_truncated_json_array(text: str) -> str:
    """
    Recover a truncated JSON array by keeping only fully-closed objects.

    The LLM can stop mid-string when it hits max_tokens, leaving the last
    JSON object unclosed.  We walk the text character-by-character, track
    brace/string depth, and collect every top-level object that was properly
    closed before the text ended.
    """
    text = text.strip()

    # Strip a leading '[' so we can scan objects individually
    if text.startswith("["):
        text = text[1:]
    # Strip trailing ']' in case it was somehow preserved
    if text.rstrip().endswith("]"):
        text = text.rstrip()[:-1]

    complete_objects: list[str] = []
    depth = 0
    in_string = False
    escape_next = False
    obj_start: Optional[int] = None

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                complete_objects.append(text[obj_start : i + 1])
                obj_start = None

    if complete_objects:
        print(f"[ActionCards] Repaired JSON: kept {len(complete_objects)} complete object(s)")
        return "[" + ", ".join(complete_objects) + "]"

    return "[]"


# ==================== Pydantic Models ====================

class GenerateActionCardsRequest(BaseModel):
    """Request to generate action cards for an audit."""
    audit_id: str
    result_ids: Optional[List[int]] = None  # If None, auto-select pages
    max_pages: int = Field(default=20, ge=1, le=100)
    max_actions_per_page: int = Field(default=5, ge=3, le=10)
    include_schema_markup: bool = True
    include_exact_text: bool = True
    language: str = "Romanian"
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    force_regenerate: bool = False  # If True, delete existing cards and regenerate


class ToggleActionRequest(BaseModel):
    """Request to toggle an action's completed status."""
    completed: bool


class UpdateCardStatusRequest(BaseModel):
    """Request to update card status."""
    status: str  # pending, in_progress, completed


class ActionCardResponse(BaseModel):
    """Response containing action card data."""
    id: int
    audit_id: str
    result_id: Optional[int]
    page_url: str
    page_title: Optional[str]
    current_score: Optional[int]
    target_score: Optional[int]
    priority: str
    actions: List[dict]
    total_actions: int
    completed_actions: int
    status: str
    provider: Optional[str]
    model: Optional[str]
    created_at: str
    updated_at: str


# ==================== Helper Functions ====================

async def get_page_content(website: str, filename: str) -> Optional[str]:
    """Load page content from input_llm directory."""
    try:
        base_dir = Path(__file__).parent.parent.parent
        content_path = base_dir / website / "input_llm" / filename
        
        if content_path.exists():
            return content_path.read_text(encoding="utf-8")
        return None
    except Exception as e:
        print(f"Error loading page content: {e}")
        return None


async def generate_actions_with_llm(
    page_url: str,
    page_content: str,
    audit_issues: dict,
    content_brief: Optional[dict],
    schema_markup: Optional[dict],
    language: str,
    max_actions: int,
    include_exact_text: bool,
    provider: str,
    model: str
) -> List[dict]:
    """
    Call LLM to generate concrete action items for a page.
    
    Returns list of action objects with exact, implementable instructions.
    """
    
    # Build context for LLM
    context = {
        "page_url": page_url,
        "current_issues": audit_issues.get("optimization_opportunities", [])[:10],
        "current_score": audit_issues.get("score"),
        "classification": audit_issues.get("classification")
    }
    
    # Add content brief recommendations if available
    if content_brief:
        context["content_recommendations"] = content_brief.get("content_changes", [])[:5]
    
    # Add schema markup as potential action if available
    if schema_markup:
        context["available_schema"] = {
            "type": schema_markup.get("schema_type"),
            "code": schema_markup.get("schema_json")
        }
    
    # Truncate page content if too long (keep first 3000 chars)
    _MAX_CONTENT = 3000
    if page_content and len(page_content) > _MAX_CONTENT:
        print(f"[action_cards] Note: page content truncated from {len(page_content):,} "
              f"to {_MAX_CONTENT:,} chars for {page_url}")
    content_sample = page_content[:_MAX_CONTENT] if page_content else ""
    
    # Build system prompt
    system_prompt = f"""You are a website optimization expert creating simple, actionable todo items for a non-technical client.

Given the audit issues and page content, create {max_actions} specific actions. Each action MUST include:

1. A clear, simple instruction (what to do)
2. The EXACT current text/element (if applicable)
3. The EXACT recommended replacement text (ready to copy-paste)
4. A simple reason (1 sentence, no jargon)
5. Difficulty: easy (copy-paste), medium (some writing), hard (needs developer)

CRITICAL RULES:
- Give EXACT text, not generic advice. "Change title to 'X'" not "Improve the title"
- Actions must be implementable by someone who knows how to edit a CMS, not an SEO expert
- NO technical jargon - use plain language
- Each action should take 5-30 minutes to implement
- Prioritize high-impact, low-effort actions first

Language for recommendations: {language}

Return a JSON array of action objects with this structure:
[
    {{
        "id": 1,
        "category": "meta|content|schema|structure|ux",
        "action": "Clear action title",
        "current": "Current text or null if none exists",
        "recommended": "Exact replacement text or content to add",
        "reason": "One sentence explaining why this helps",
        "difficulty": "easy|medium|hard"
    }}
]

Categories:
- meta: title, description, headers
- content: body text, FAQ, examples
- schema: structured data markup
- structure: internal links, navigation
- ux: user experience improvements
"""

    # Build user prompt
    user_prompt = f"""Page URL: {page_url}
Current Score: {context.get('current_score', 'N/A')}/100

AUDIT ISSUES:
{json.dumps(context.get('current_issues', []), indent=2, ensure_ascii=False)}

"""
    
    if context.get("content_recommendations"):
        user_prompt += f"""
CONTENT RECOMMENDATIONS:
{json.dumps(context.get('content_recommendations', []), indent=2, ensure_ascii=False)}

"""
    
    if context.get("available_schema"):
        user_prompt += f"""
AVAILABLE SCHEMA MARKUP (ready to implement):
Type: {context['available_schema']['type']}

"""
    
    if content_sample:
        user_prompt += f"""
PAGE CONTENT SAMPLE:
{content_sample}
...

"""
    
    user_prompt += f"""
Create {max_actions} concrete, actionable items for this page. Focus on:
1. Quick wins (easy to implement, high impact)
2. Exact text to use (no generic "improve X")
3. Plain language a content manager can understand

Return ONLY the JSON array, no other text.
"""
    
    try:
        # Call LLM using shared helper
        # max_tokens=4096 to avoid truncated JSON strings in "recommended" fields
        response, _in_tok, _out_tok = await call_llm_for_summary(
            provider=provider,
            model=model,
            system_prompt=system_prompt,
            user_content=user_prompt,
            max_tokens=4096
        )

        # Parse JSON response (with truncation recovery)
        response_text = clean_json_response(response.strip())

        try:
            actions = json.loads(response_text)
        except json.JSONDecodeError as parse_err:
            print(f"[ActionCards] JSON parse error ({parse_err}), attempting repair...")
            response_text = _repair_truncated_json_array(response_text)
            actions = json.loads(response_text)  # let it raise if still invalid

        if not isinstance(actions, list):
            raise ValueError(f"LLM returned non-list: {type(actions)}")

        # Validate and set completed=false for all actions
        for i, action in enumerate(actions):
            action["completed"] = False
            if "id" not in action:
                action["id"] = i + 1
            if "difficulty" not in action:
                action["difficulty"] = "medium"

        return actions[:max_actions]

    except Exception as e:
        print(f"Error generating actions with LLM: {e}")
        # Return fallback generic actions
        return [
            {
                "id": 1,
                "category": "meta",
                "action": "Review page title and meta description",
                "current": None,
                "recommended": "Optimize for target keywords and user intent",
                "reason": "Meta tags are critical for AI search visibility",
                "difficulty": "medium",
                "completed": False
            }
        ]


async def determine_priority(score: int, issues_count: int) -> str:
    """Determine card priority based on score and issues."""
    if score < 50 or issues_count >= 10:
        return "critical"
    elif score < 65 or issues_count >= 6:
        return "high"
    elif score < 80 or issues_count >= 3:
        return "medium"
    else:
        return "low"


async def estimate_target_score(current_score: int, actions_count: int) -> int:
    """Estimate score improvement after implementing actions."""
    # Rough estimate: each action adds 3-8 points depending on current score
    if current_score < 50:
        improvement_per_action = 8
    elif current_score < 70:
        improvement_per_action = 5
    else:
        improvement_per_action = 3
    
    estimated = current_score + (actions_count * improvement_per_action)
    return min(estimated, 95)  # Cap at 95 (perfection is rare)


async def generate_card_for_page(
    db: AsyncSession,
    audit: Audit,
    result: AuditResult,
    request: GenerateActionCardsRequest
) -> Optional[ActionCard]:
    """Generate a single action card for a page."""
    
    try:
        # Parse result JSON
        result_data = json.loads(result.result_json) if result.result_json else {}
        
        # Load page content
        page_content = await get_page_content(audit.website, result.filename)
        
        # Try to load content brief if exists
        content_brief_data = None
        try:
            brief_result = await db.execute(
                select(ContentBrief).where(
                    and_(
                        ContentBrief.audit_id == audit.id,
                        ContentBrief.page_url == result.page_url
                    )
                )
            )
            brief = brief_result.scalar_one_or_none()
            if brief and brief.brief_json:
                content_brief_data = json.loads(brief.brief_json)
        except Exception:
            pass
        
        # Try to load schema markup if exists and requested
        schema_markup_data = None
        if request.include_schema_markup:
            try:
                schema_result = await db.execute(
                    select(SchemaMarkup).where(
                        and_(
                            SchemaMarkup.audit_id == audit.id,
                            SchemaMarkup.page_url == result.page_url
                        )
                    )
                )
                schema = schema_result.scalar_one_or_none()
                if schema and schema.schema_json:
                    schema_markup_data = json.loads(schema.schema_json)
            except Exception:
                pass
        
        # Generate actions with LLM
        actions = await generate_actions_with_llm(
            page_url=result.page_url,
            page_content=page_content or "",
            audit_issues=result_data,
            content_brief=content_brief_data,
            schema_markup=schema_markup_data,
            language=request.language,
            max_actions=request.max_actions_per_page,
            include_exact_text=request.include_exact_text,
            provider=request.provider,
            model=request.model
        )
        
        # Determine priority
        issues_count = len(result_data.get("optimization_opportunities", []))
        priority = await determine_priority(result.score or 0, issues_count)
        
        # Estimate target score
        target_score = await estimate_target_score(result.score or 0, len(actions))
        
        # Extract page title from content or result
        page_title = result_data.get("page_title")
        if not page_title and page_content:
            # Try to extract from content
            lines = page_content.split("\n")
            page_title = lines[0][:200] if lines else None
        
        # Create action card
        card = ActionCard(
            audit_id=audit.id,
            result_id=result.id,
            page_url=result.page_url,
            page_title=page_title,
            current_score=result.score,
            target_score=target_score,
            priority=priority,
            actions_json=json.dumps(actions, ensure_ascii=False),
            total_actions=len(actions),
            completed_actions=0,
            status="pending",
            provider=request.provider,
            model=request.model
        )
        
        db.add(card)
        await db.commit()
        await db.refresh(card)
        
        return card
        
    except Exception as e:
        print(f"Error generating card for {result.page_url}: {e}")
        await db.rollback()
        return None


# ==================== Routes ====================

@router.post("/generate")
async def generate_action_cards(
    request: GenerateActionCardsRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    Generate action cards for an audit.
    
    If result_ids not specified, auto-selects worst-performing pages.
    Runs generation in background task.
    """
    
    # Verify audit exists and is completed
    result = await db.execute(select(Audit).where(Audit.id == request.audit_id))
    audit = result.scalar_one_or_none()
    
    if not audit:
        raise_not_found("Audit")
    
    if audit.status != "completed":
        raise HTTPException(
            status_code=400, 
            detail="Audit must be completed before generating action cards"
        )
    
    # Get results to process
    if request.result_ids:
        # Use specified results
        results_query = await db.execute(
            select(AuditResult).where(
                and_(
                    AuditResult.audit_id == request.audit_id,
                    AuditResult.id.in_(request.result_ids)
                )
            )
        )
        results = results_query.scalars().all()
    else:
        # Auto-select worst pages (score < 70, limit to max_pages)
        results_query = await db.execute(
            select(AuditResult)
            .where(
                and_(
                    AuditResult.audit_id == request.audit_id,
                    AuditResult.score < 70,
                    AuditResult.score.isnot(None)
                )
            )
            .order_by(AuditResult.score.asc())
            .limit(request.max_pages)
        )
        results = results_query.scalars().all()
    
    if not results:
        raise HTTPException(
            status_code=404,
            detail="No results found for processing"
        )

    # Find which result_ids already have cards
    result_ids_all = [r.id for r in results]
    existing_q = await db.execute(
        select(ActionCard.result_id).where(
            and_(
                ActionCard.audit_id == request.audit_id,
                ActionCard.result_id.in_(result_ids_all)
            )
        )
    )
    existing_result_ids = {row[0] for row in existing_q.fetchall()}

    if request.force_regenerate and existing_result_ids:
        # Delete only the existing cards for pages we're about to regenerate
        await db.execute(
            ActionCard.__table__.delete().where(
                and_(
                    ActionCard.audit_id == request.audit_id,
                    ActionCard.result_id.in_(list(existing_result_ids))
                )
            )
        )
        await db.commit()
        result_ids_to_process = result_ids_all
    else:
        # Skip pages that already have cards — preserve progress
        result_ids_to_process = [r.id for r in results if r.id not in existing_result_ids]

    skipped = len(result_ids_all) - len(result_ids_to_process)

    if not result_ids_to_process:
        return {
            "status": "already_generated",
            "audit_id": request.audit_id,
            "pages_to_process": 0,
            "skipped": skipped,
            "message": f"Toate {skipped} paginile au deja action cards. Folosește 'Force Regenerate' pentru a le recrea."
        }

    # Serialize audit_id only — re-fetch inside background task to avoid DetachedInstanceError
    audit_id_str = audit.id
    request_copy = request  # pydantic model is safe to pass

    async def generate_all_cards():
        async with AsyncSessionLocal() as session:
            audit_q = await session.execute(select(Audit).where(Audit.id == audit_id_str))
            audit_obj = audit_q.scalar_one_or_none()
            if not audit_obj:
                print(f"[ActionCards] Audit {audit_id_str} not found in background task")
                return
            for rid in result_ids_to_process:
                try:
                    res_q = await session.execute(
                        select(AuditResult).where(AuditResult.id == rid)
                    )
                    result_obj = res_q.scalar_one_or_none()
                    if result_obj:
                        await generate_card_for_page(session, audit_obj, result_obj, request_copy)
                except Exception as e:
                    print(f"[ActionCards] Error generating card for result {rid}: {e}")

    background_tasks.add_task(generate_all_cards)

    return {
        "status": "generating",
        "audit_id": request.audit_id,
        "pages_to_process": len(result_ids_to_process),
        "skipped": skipped,
        "message": f"Se generează {len(result_ids_to_process)} action cards... ({skipped} existente păstrate)"
    }


@router.get("")
async def list_action_cards(
    audit_id: str,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """List action cards for an audit with optional filters."""
    
    query = select(ActionCard).where(ActionCard.audit_id == audit_id)
    
    if status:
        query = query.where(ActionCard.status == status)
    
    if priority:
        query = query.where(ActionCard.priority == priority)
    
    query = query.order_by(
        ActionCard.priority.desc(),
        ActionCard.current_score.asc()
    )
    
    result = await db.execute(query)
    cards = result.scalars().all()
    
    return {
        "cards": [card.to_dict() for card in cards],
        "total": len(cards)
    }


@router.get("/{card_id}")
async def get_action_card(
    card_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get a specific action card."""
    
    result = await db.execute(
        select(ActionCard).where(ActionCard.id == card_id)
    )
    card = result.scalar_one_or_none()
    
    if not card:
        raise_not_found("Action card")
    
    return card.to_dict()


@router.patch("/{card_id}/actions/{action_id}")
async def toggle_action(
    card_id: str,
    action_id: int,
    request: ToggleActionRequest,
    db: AsyncSession = Depends(get_db)
):
    """Toggle an action's completed status."""
    
    result = await db.execute(
        select(ActionCard).where(ActionCard.id == card_id)
    )
    card = result.scalar_one_or_none()
    
    if not card:
        raise_not_found("Action card")
    
    # Parse actions
    actions = json.loads(card.actions_json)
    
    # Find and toggle action
    action_found = False
    for action in actions:
        if action.get("id") == action_id:
            action["completed"] = request.completed
            action_found = True
            break
    
    if not action_found:
        raise_not_found("Action")
    
    # Update card
    card.actions_json = json.dumps(actions, ensure_ascii=False)
    card.completed_actions = sum(1 for a in actions if a.get("completed"))
    card.updated_at = datetime.now(timezone.utc)
    
    # Auto-update status
    if card.completed_actions == 0:
        card.status = "pending"
    elif card.completed_actions == card.total_actions:
        card.status = "completed"
    else:
        card.status = "in_progress"
    
    await db.commit()
    await db.refresh(card)
    
    return card.to_dict()


@router.patch("/{card_id}")
async def update_card_status(
    card_id: str,
    request: UpdateCardStatusRequest,
    db: AsyncSession = Depends(get_db)
):
    """Update card status manually."""
    
    if request.status not in ["pending", "in_progress", "completed"]:
        raise_bad_request("Invalid status")
    
    result = await db.execute(
        select(ActionCard).where(ActionCard.id == card_id)
    )
    card = result.scalar_one_or_none()
    
    if not card:
        raise_not_found("Action card")
    
    card.status = request.status
    card.updated_at = datetime.now(timezone.utc)
    
    await db.commit()
    await db.refresh(card)
    
    return card.to_dict()


@router.get("/export/{audit_id}")
async def export_action_cards(
    audit_id: str,
    format: str = "csv",
    db: AsyncSession = Depends(get_db)
):
    """
    Export action cards in various formats.
    
    Formats: csv, json, html, trello
    """
    
    # Get all cards for audit
    result = await db.execute(
        select(ActionCard)
        .where(ActionCard.audit_id == audit_id)
        .order_by(ActionCard.priority.desc(), ActionCard.current_score.asc())
    )
    cards = result.scalars().all()
    
    if not cards:
        raise HTTPException(status_code=404, detail="No action cards found")
    
    # Get audit info
    audit_result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = audit_result.scalar_one_or_none()
    
    if format == "csv":
        return await export_csv(cards, audit)
    elif format == "json":
        return await export_json(cards, audit)
    elif format == "html":
        return await export_html(cards, audit, db)
    elif format == "trello":
        return await export_trello(cards, audit)
    else:
        raise_bad_request("Invalid format")


def _cards_to_export_pages(cards: List[ActionCard]) -> List["ExportPage"]:
    """Normalize ActionCard rows into the shared export engine's shape."""
    from api.utils.recommendation_export import ExportItem, ExportPage

    pages = []
    for card in cards:
        actions = json.loads(card.actions_json)
        items = [
            ExportItem(
                title=action.get("action"),
                category=action.get("category"),
                tag=action.get("difficulty"),
                current=action.get("current"),
                recommended=action.get("recommended"),
                reason=action.get("reason"),
                completed=bool(action.get("completed")),
            )
            for action in actions
        ]
        pages.append(ExportPage(
            page_url=card.page_url,
            page_title=card.page_title,
            priority=card.priority,
            current_score=card.current_score,
            target_score=card.target_score,
            progress_label=f"{card.completed_actions}/{card.total_actions}",
            items=items,
        ))
    return pages


async def export_csv(cards: List[ActionCard], audit: Audit) -> Response:
    """Export as CSV file."""
    from api.utils.recommendation_export import build_csv_response
    return build_csv_response(_cards_to_export_pages(cards), audit.website, "action_cards")


async def export_json(cards: List[ActionCard], audit: Audit) -> dict:
    """Export as JSON."""
    return {
        "audit_id": audit.id,
        "website": audit.website,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "cards": [card.to_dict() for card in cards]
    }


async def export_trello(cards: List[ActionCard], audit: Audit) -> dict:
    """Export in Trello-importable format."""
    from api.utils.recommendation_export import build_trello_export
    return build_trello_export(_cards_to_export_pages(cards), audit.website, "Action Cards")


async def export_html(cards: List[ActionCard], audit: Audit, db: AsyncSession) -> Response:
    """Export as standalone HTML report."""
    from api.utils.recommendation_export import build_html_response
    return build_html_response(_cards_to_export_pages(cards), audit.website, "Action Cards", "action_cards")


