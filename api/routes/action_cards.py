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
        response = await call_llm_for_summary(
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


async def export_csv(cards: List[ActionCard], audit: Audit) -> Response:
    """Export as CSV file."""
    import csv
    import io
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        "Page URL", "Priority", "Current Score", "Target Score",
        "Action #", "Category", "Action", "Current Text", 
        "Recommended Text", "Reason", "Difficulty", "Status"
    ])
    
    # Data
    for card in cards:
        actions = json.loads(card.actions_json)
        for action in actions:
            writer.writerow([
                card.page_url,
                card.priority,
                card.current_score,
                card.target_score,
                action.get("id"),
                action.get("category"),
                action.get("action"),
                action.get("current", "")[:200],
                action.get("recommended", "")[:500],
                action.get("reason", ""),
                action.get("difficulty"),
                "✓" if action.get("completed") else "☐"
            ])
    
    csv_content = output.getvalue()
    
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=action_cards_{audit.website}_{datetime.now().strftime('%Y%m%d')}.csv"
        }
    )


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
    
    # Group by priority
    lists = {
        "critical": {"name": "🔴 Critical Priority", "cards": []},
        "high": {"name": "🟠 High Priority", "cards": []},
        "medium": {"name": "🟡 Medium Priority", "cards": []},
        "low": {"name": "🟢 Low Priority", "cards": []}
    }
    
    for card in cards:
        actions = json.loads(card.actions_json)
        
        # Create checklist items
        checklist_items = []
        for action in actions:
            item_text = f"{action.get('action')} ({action.get('difficulty')})"
            checklist_items.append({
                "name": item_text,
                "checked": action.get("completed", False)
            })
        
        # Card description
        description = f"""**Page:** {card.page_url}
**Current Score:** {card.current_score}/100
**Target Score:** {card.target_score}/100

## Actions:

"""
        for action in actions:
            description += f"""### {action.get('action')}
**Category:** {action.get('category')}
**Difficulty:** {action.get('difficulty')}

**Current:** {action.get('current') or 'N/A'}

**Recommended:**
{action.get('recommended')}

**Why:** {action.get('reason')}

---

"""
        
        # Trello API hard limit is 16 384 chars per card description.
        _TRELLO_MAX_DESC = 15_000
        if len(description) > _TRELLO_MAX_DESC:
            print(f"[action_cards] Note: Trello description truncated from {len(description):,} "
                  f"to {_TRELLO_MAX_DESC:,} chars for {card.page_url}")
            description = description[:_TRELLO_MAX_DESC] + "\n\n*[truncated — see full export for remaining actions]*"

        trello_card = {
            "name": f"{card.page_title or card.page_url} ({card.current_score}→{card.target_score})",
            "desc": description,
            "checklists": [
                {
                    "name": "Implementation Checklist",
                    "items": checklist_items
                }
            ]
        }
        
        lists[card.priority]["cards"].append(trello_card)
    
    return {
        "name": f"Action Cards: {audit.website}",
        "lists": [v for k, v in lists.items() if v["cards"]]
    }


async def export_html(cards: List[ActionCard], audit: Audit, db: AsyncSession) -> Response:
    """Export as standalone HTML report."""
    
    # Try to get branding config if exists
    agency_name = "Your Agency"
    agency_logo = None
    
    # Build HTML
    html = f"""<!DOCTYPE html>
<html lang="ro">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Action Cards - {audit.website}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
            padding: 20px;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}
        
        .header {{
            border-bottom: 3px solid #4F46E5;
            padding-bottom: 20px;
            margin-bottom: 40px;
        }}
        
        .header h1 {{
            color: #1F2937;
            font-size: 32px;
            margin-bottom: 10px;
        }}
        
        .header .meta {{
            color: #6B7280;
            font-size: 14px;
        }}
        
        .card {{
            border: 2px solid #E5E7EB;
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 24px;
            break-inside: avoid;
        }}
        
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 1px solid #E5E7EB;
        }}
        
        .card-url {{
            font-size: 14px;
            color: #4F46E5;
            font-weight: 600;
            word-break: break-all;
        }}
        
        .priority-badge {{
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        
        .priority-critical {{ background: #FEE2E2; color: #991B1B; }}
        .priority-high {{ background: #FED7AA; color: #9A3412; }}
        .priority-medium {{ background: #FEF3C7; color: #92400E; }}
        .priority-low {{ background: #D1FAE5; color: #065F46; }}
        
        .score-section {{
            display: flex;
            gap: 24px;
            margin-bottom: 20px;
            padding: 12px;
            background: #F9FAFB;
            border-radius: 4px;
        }}
        
        .score-item {{
            flex: 1;
        }}
        
        .score-label {{
            font-size: 12px;
            color: #6B7280;
            text-transform: uppercase;
            margin-bottom: 4px;
        }}
        
        .score-value {{
            font-size: 24px;
            font-weight: 700;
            color: #1F2937;
        }}
        
        .action {{
            margin-bottom: 20px;
            padding: 16px;
            background: #F9FAFB;
            border-left: 4px solid #4F46E5;
            border-radius: 4px;
        }}
        
        .action-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }}
        
        .action-title {{
            font-size: 16px;
            font-weight: 600;
            color: #1F2937;
        }}
        
        .action-difficulty {{
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }}
        
        .difficulty-easy {{ background: #D1FAE5; color: #065F46; }}
        .difficulty-medium {{ background: #FED7AA; color: #9A3412; }}
        .difficulty-hard {{ background: #FEE2E2; color: #991B1B; }}
        
        .action-content {{
            margin-top: 12px;
        }}
        
        .action-section {{
            margin-bottom: 12px;
        }}
        
        .action-label {{
            font-size: 11px;
            color: #6B7280;
            text-transform: uppercase;
            font-weight: 600;
            margin-bottom: 4px;
        }}
        
        .action-text {{
            font-size: 14px;
            color: #374151;
            padding: 8px;
            background: white;
            border-radius: 4px;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        
        .action-reason {{
            font-size: 13px;
            color: #6B7280;
            font-style: italic;
            margin-top: 8px;
        }}
        
        .footer {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #E5E7EB;
            text-align: center;
            color: #6B7280;
            font-size: 13px;
        }}
        
        @media print {{
            body {{ background: white; padding: 0; }}
            .container {{ box-shadow: none; padding: 20px; }}
            .card {{ page-break-inside: avoid; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Action Cards pentru {audit.website}</h1>
            <div class="meta">
                Generated by {agency_name} on {datetime.now().strftime('%d %B %Y')}
            </div>
        </div>
"""
    
    # Add cards
    for card in cards:
        actions = json.loads(card.actions_json)
        
        html += f"""
        <div class="card">
            <div class="card-header">
                <div class="card-url">{escape(card.page_url or "")}</div>
                <span class="priority-badge priority-{escape(card.priority or "")}">{escape(card.priority or "")}</span>
            </div>

            <div class="score-section">
                <div class="score-item">
                    <div class="score-label">Current Score</div>
                    <div class="score-value">{card.current_score}/100</div>
                </div>
                <div class="score-item">
                    <div class="score-label">Target Score</div>
                    <div class="score-value">{card.target_score}/100</div>
                </div>
                <div class="score-item">
                    <div class="score-label">Actions</div>
                    <div class="score-value">{card.completed_actions}/{card.total_actions}</div>
                </div>
            </div>
"""

        for action in actions:
            difficulty_class = f"difficulty-{escape(action.get('difficulty', 'medium') or 'medium')}"

            html += f"""
            <div class="action">
                <div class="action-header">
                    <div class="action-title">{"✓ " if action.get('completed') else "☐ "}{escape(action.get('action') or "")}</div>
                    <span class="action-difficulty {difficulty_class}">{escape(action.get('difficulty') or "")}</span>
                </div>

                <div class="action-content">
"""

            if action.get('current'):
                html += f"""
                    <div class="action-section">
                        <div class="action-label">Current:</div>
                        <div class="action-text">{escape(action.get('current') or "")}</div>
                    </div>
"""

            html += f"""
                    <div class="action-section">
                        <div class="action-label">Recommended:</div>
                        <div class="action-text">{escape(action.get('recommended') or "")}</div>
                    </div>

                    <div class="action-reason">
                        💡 {escape(action.get('reason') or "")}
                    </div>
                </div>
            </div>
"""
        
        html += """
        </div>
"""
    
    html += f"""
        <div class="footer">
            Generated by {agency_name} • {datetime.now().strftime('%d %B %Y')}
        </div>
    </div>
</body>
</html>
"""
    
    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Content-Disposition": f"attachment; filename=action_cards_{audit.website}_{datetime.now().strftime('%Y%m%d')}.html"
        }
    )


