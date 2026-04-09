"""
AI Executive Summary and Action Plan generation for completed audits.

Generates a narrative summary and prioritized action plan by analyzing
all audit results with a second LLM call.
"""

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from api.utils.errors import raise_not_found, raise_bad_request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# LLM clients
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from mistralai import Mistral

from api.models.database import AsyncSessionLocal, Audit, AuditResult, AuditSummary
from api.routes.costs import track_cost

router = APIRouter(prefix="/api/audits", tags=["summary"])


def _repair_json(text: str) -> str:
    """
    Attempt to repair common LLM JSON issues:
      - Trailing commas before } or ]
      - Missing commas between adjacent string values
      - Unterminated strings (truncated output): closes open structures gracefully
    """
    # Fix trailing commas before closing bracket/brace
    text = re.sub(r',(\s*[}\]])', r'\1', text)
    # Fix missing commas between "value"\n"key": patterns
    text = re.sub(r'("(?:[^"\\]|\\.)*")\s*\n(\s*")', r'\1,\n\2', text)

    # Try parsing; if it still fails with an unterminated string, truncate & close
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError as e:
        err = str(e)
        if 'Unterminated string' in err or 'Expecting' in err:
            # Truncate to just before the bad position and close open structures
            pos = getattr(e, 'pos', len(text))
            truncated = text[:pos].rstrip().rstrip(',')
            # Walk through and track bracket/brace depth (skip strings)
            stack = []
            in_string = False
            escape_next = False
            for ch in truncated:
                if escape_next:
                    escape_next = False
                    continue
                if ch == '\\' and in_string:
                    escape_next = True
                    continue
                if ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch in '{[':
                        stack.append(ch)
                    elif ch in '}]' and stack:
                        stack.pop()
            # If we're still inside a string, close it first
            if in_string:
                truncated += '"'
            # Close all open structures
            closing = ''.join('}' if c == '{' else ']' for c in reversed(stack))
            return truncated + closing
    return text


def clean_json_response(text: str) -> str:
    """Strip markdown code fences and repair common JSON issues from LLM responses."""
    text = text.strip()
    # Remove ```json or ``` prefix
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1:]
    # Remove trailing ```
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3]
    text = text.strip()
    # Attempt structural repair
    text = _repair_json(text)
    return text


async def call_llm_for_summary(
    provider: str,
    model: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 4096
) -> tuple[str, int, int]:
    """
    Call LLM provider to generate summary.

    Returns:
        (raw_response_text, input_tokens, output_tokens)
    """
    provider = provider.upper()

    if provider == "ANTHROPIC":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not configured")

        client = AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}]
        )
        in_tok  = response.usage.input_tokens  if response.usage else 0
        out_tok = response.usage.output_tokens if response.usage else 0
        return response.content[0].text, in_tok, out_tok

    elif provider == "OPENAI":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")

        client = AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            response_format={"type": "json_object"}
        )
        in_tok  = response.usage.prompt_tokens     if response.usage else 0
        out_tok = response.usage.completion_tokens if response.usage else 0
        return response.choices[0].message.content, in_tok, out_tok

    elif provider == "MISTRAL":
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise ValueError("MISTRAL_API_KEY not configured")

        client = Mistral(api_key=api_key)
        response = await client.chat.complete_async(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            response_format={"type": "json_object"}
        )
        in_tok  = response.usage.prompt_tokens     if response.usage else 0
        out_tok = response.usage.completion_tokens if response.usage else 0
        return response.choices[0].message.content, in_tok, out_tok

    elif provider == "GOOGLE":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not configured")

        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=model,
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=max_tokens,
                temperature=0.3,
                response_mime_type="application/json"
            )
        )
        meta = getattr(response, "usage_metadata", None)
        in_tok  = getattr(meta, "prompt_token_count",     0) if meta else 0
        out_tok = getattr(meta, "candidates_token_count", 0) if meta else 0
        return response.text, in_tok, out_tok

    else:
        raise ValueError(f"Unknown provider: {provider}")


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
            asyncio.create_task(track_cost(
                source="summary",
                provider=provider.lower(),
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                audit_id=audit_id,
                website=audit.website,
            ))

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
                existing.generated_at = datetime.utcnow()
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
                    generated_at=datetime.utcnow()
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
