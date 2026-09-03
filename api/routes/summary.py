"""
AI Executive Summary and Action Plan generation for completed audits.

Generates a narrative summary and prioritized action plan by analyzing
all audit results with a second LLM call.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from api.utils.errors import raise_not_found, raise_bad_request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import AsyncSessionLocal, Audit, AuditResult, AuditSummary
from api.routes.costs import track_cost

# call_llm_for_summary/clean_json_response moved to api/utils/llm_json_client.py
# (Etapa 5.1 of the consolidation) since 7 other files import them from this
# module's path -- re-exported here so none of those import sites need to change.
from api.utils.llm_json_client import call_llm_for_summary, clean_json_response  # noqa: F401

router = APIRouter(prefix="/api/audits", tags=["summary"])


def build_system_prompt(language: str) -> str:
    """Build the system prompt for summary generation."""
    language_instruction = ""
    if language.lower() != "english":
        language_instruction = f"\n\nIMPORTANT: Write ALL text values in {language}. JSON keys must remain in English."
    
    return f"""You are an expert digital marketing analyst preparing an executive summary for C-level stakeholders.

Analyze the provided audit results and generate a comprehensive summary in JSON format with exactly these keys:

1. "executive_summary": A narrative summary in 3-4 paragraphs suitable for C-level executives. Cover:
   - Overall website performance assessment
   - Critical issues requiring immediate attention
   - Competitive positioning insights
   - Strategic recommendations

2. "key_findings": An array of 5-8 most important findings. Each object must have:
   - "finding": Clear description of the finding
   - "impact": One of "high", "medium", or "low"
   - "category": Classification like "SEO", "Accessibility", "Content Quality", "UX", etc.

3. "action_plan": An array of prioritized actions grouped by implementation timeline (weeks 1-6). Each object must have:
   - "week": Integer 1-6 indicating when to implement
   - "action": Clear description of what needs to be done
   - "pages_affected": Number of pages or "All" or specific page count
   - "expected_impact": Description of expected outcome
   - "priority": One of "critical", "high", or "medium"

4. "competitive_position": A single paragraph (3-5 sentences) evaluating the website's competitive position based on the audit findings.

Return ONLY valid JSON. Do not include any explanatory text before or after the JSON.{language_instruction}"""


def build_audit_data_payload(results: list[AuditResult]) -> str:
    """Build the data payload from audit results."""
    if not results:
        return "No audit results available."
    
    # Calculate score distribution
    scores = [r.score for r in results if r.score is not None]
    if not scores:
        score_stats = "No scores available"
    else:
        score_stats = f"Average: {sum(scores)/len(scores):.1f}, Min: {min(scores)}, Max: {max(scores)}"
    
    # Get top 10 best and worst pages
    sorted_results = sorted([r for r in results if r.score is not None], key=lambda x: x.score)
    worst_10 = sorted_results[:10]
    best_10 = sorted_results[-10:][::-1]
    
    # Extract top optimization opportunities across all pages
    all_opportunities = []
    for result in results:
        if result.result_json:
            try:
                result_data = json.loads(result.result_json)
                opportunities = result_data.get('optimization_opportunities', [])
                for opp in opportunities[:3]:  # Top 3 from each page
                    all_opportunities.append({
                        'page': result.page_url,
                        'opportunity': opp
                    })
            except (json.JSONDecodeError, KeyError):
                continue
    
    # Build payload
    payload_parts = [
        f"AUDIT OVERVIEW:",
        f"Total pages analyzed: {len(results)}",
        f"Score distribution: {score_stats}",
        f"",
        f"TOP 10 BEST PERFORMING PAGES:",
    ]
    
    for i, result in enumerate(best_10, 1):
        payload_parts.append(f"{i}. {result.page_url} - Score: {result.score}")
    
    payload_parts.extend([
        f"",
        f"TOP 10 WORST PERFORMING PAGES:",
    ])
    
    for i, result in enumerate(worst_10, 1):
        payload_parts.append(f"{i}. {result.page_url} - Score: {result.score}")
    
    payload_parts.extend([
        f"",
        f"TOP 30 OPTIMIZATION OPPORTUNITIES (across all pages):",
    ])
    
    for i, item in enumerate(all_opportunities[:30], 1):
        opp = item['opportunity']
        if isinstance(opp, dict):
            priority = opp.get('priority', 'unknown')
            issue = opp.get('issue', str(opp))
            payload_parts.append(f"{i}. [{priority}] {issue} (Page: {item['page']})")
        else:
            payload_parts.append(f"{i}. {opp} (Page: {item['page']})")
    
    return "\n".join(payload_parts)


async def generate_summary_task(
    audit_id: str,
    language: str,
    provider: Optional[str],
    model: Optional[str]
):
    """
    Background task to generate AI summary.
    
    This runs asynchronously after the endpoint returns.
    """
    async with AsyncSessionLocal() as db:
        try:
            # Load audit
            audit_result = await db.execute(
                select(Audit).where(Audit.id == audit_id)
            )
            audit = audit_result.scalar_one_or_none()
            
            if not audit:
                print(f"[Summary] Audit {audit_id} not found")
                return
            
            # Use audit's provider/model if not overridden
            if not provider:
                provider = audit.provider
            if not model:
                model = audit.model
            
            # Load all audit results
            results_query = await db.execute(
                select(AuditResult).where(AuditResult.audit_id == audit_id)
            )
            results = results_query.scalars().all()
            
            if not results:
                print(f"[Summary] No results found for audit {audit_id}")
                return
            
            # Build prompts
            system_prompt = build_system_prompt(language)
            user_content = build_audit_data_payload(results)
            
            # Call LLM
            print(f"[Summary] Generating summary for audit {audit_id} using {provider}/{model}")
            response_text, in_tok, out_tok = await call_llm_for_summary(
                provider=provider,
                model=model,
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=4096
            )
            # Awaited, not fire-and-forget via asyncio.create_task: track_cost() opens its
            # own AsyncSessionLocal(), and firing it concurrently while this function's own
            # `db` session is still open (it commits below) can silently drop that commit --
            # both sessions share one physical SQLite connection (StaticPool). See the
            # Etapa 3 fix + comment in api/routes/visibility.py for the reproduced bug.
            await track_cost(
                source="summary",
                provider=provider.lower(),
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                audit_id=audit_id,
                website=audit.website,
            )

            # Clean and parse response
            clean_text = clean_json_response(response_text)
            summary_data = json.loads(clean_text)
            
            # Validate required keys
            required_keys = ['executive_summary', 'key_findings', 'action_plan', 'competitive_position']
            for key in required_keys:
                if key not in summary_data:
                    raise ValueError(f"Missing required key: {key}")
            
            # Check if summary already exists
            existing_summary = await db.execute(
                select(AuditSummary).where(AuditSummary.audit_id == audit_id)
            )
            existing = existing_summary.scalar_one_or_none()
            
            if existing:
                # Update existing
                existing.executive_summary = summary_data['executive_summary']
                existing.key_findings = json.dumps(summary_data['key_findings'])
                existing.action_plan = json.dumps(summary_data['action_plan'])
                existing.competitive_position = summary_data['competitive_position']
                existing.language = language
                existing.provider = provider
                existing.model = model
                existing.generated_at = datetime.now(timezone.utc)
            else:
                # Create new
                new_summary = AuditSummary(
                    audit_id=audit_id,
                    executive_summary=summary_data['executive_summary'],
                    key_findings=json.dumps(summary_data['key_findings']),
                    action_plan=json.dumps(summary_data['action_plan']),
                    competitive_position=summary_data['competitive_position'],
                    language=language,
                    provider=provider,
                    model=model,
                    generated_at=datetime.now(timezone.utc)
                )
                db.add(new_summary)
            
            await db.commit()
            print(f"[Summary] Successfully generated summary for audit {audit_id}")
            
        except Exception as e:
            print(f"[Summary] Error generating summary for audit {audit_id}: {str(e)}")
            await db.rollback()


@router.post("/{audit_id}/summary")
async def generate_audit_summary(
    audit_id: str,
    background_tasks: BackgroundTasks,
    language: str = Query(default="English", description="Output language for summary"),
    provider: Optional[str] = Query(default=None, description="Override LLM provider (anthropic/openai/mistral)"),
    model: Optional[str] = Query(default=None, description="Override LLM model")
):
    """
    Generate AI executive summary and action plan for a completed audit.
    
    This endpoint returns immediately and generates the summary in the background.
    Use GET /{audit_id}/summary to check status and retrieve results.
    
    Query Parameters:
    - language: Output language (default: English)
    - provider: Override provider (optional - uses audit's provider if not set)
    - model: Override model (optional - uses audit's model if not set)
    """
    # Verify audit exists and is completed
    async with AsyncSessionLocal() as db:
        audit_result = await db.execute(
            select(Audit).where(Audit.id == audit_id)
        )
        audit = audit_result.scalar_one_or_none()
        
        if not audit:
            raise_not_found("Audit")
        
        if audit.status != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Audit must be completed before generating summary (current status: {audit.status})"
            )
    
    # Normalize provider name
    if provider:
        provider = provider.lower()
        if provider not in ["anthropic", "openai", "mistral"]:
            raise_bad_request("Invalid provider. Use: anthropic, openai, or mistral")
    
    # Schedule background task
    background_tasks.add_task(
        generate_summary_task,
        audit_id=audit_id,
        language=language,
        provider=provider,
        model=model
    )
    
    return {
        "status": "generating",
        "message": "Summary generation started. Check GET /{audit_id}/summary for results.",
        "audit_id": audit_id
    }


@router.get("/{audit_id}/summary")
async def get_audit_summary(audit_id: str):
    """
    Retrieve the AI-generated summary for an audit.
    
    Returns:
    - Summary data if generated
    - Status "not_generated" if summary doesn't exist yet
    """
    async with AsyncSessionLocal() as db:
        # Verify audit exists
        audit_result = await db.execute(
            select(Audit).where(Audit.id == audit_id)
        )
        audit = audit_result.scalar_one_or_none()
        
        if not audit:
            raise_not_found("Audit")
        
        # Get summary
        summary_result = await db.execute(
            select(AuditSummary).where(AuditSummary.audit_id == audit_id)
        )
        summary = summary_result.scalar_one_or_none()
        
        if not summary:
            return {"status": "not_generated"}
        
        return summary.to_dict()
