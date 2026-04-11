"""
Schema Markup Generator - Automated JSON-LD generation for audited pages.

Generates valid Schema.org structured data ready for implementation.
"""

import asyncio
import json
import os
import re as re_module
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from api.utils.errors import raise_not_found, raise_bad_request
from api.limiter import limiter
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

# LLM clients
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from mistralai import Mistral

from api.models.database import AsyncSessionLocal, Audit, AuditResult, SchemaMarkup
from api.routes.costs import track_cost

router = APIRouter(prefix="/api/schema", tags=["schema"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class GenerateSchemaRequest(BaseModel):
    """Request model for schema generation."""
    audit_id: str
    result_ids: Optional[List[int]] = None
    max_pages: int = Field(default=50, ge=1, le=200)
    provider: Optional[str] = None
    model: Optional[str] = None
    schema_types_hint: Optional[List[str]] = None
    website_type: Optional[str] = None  # ecommerce, saas, local_business, blog, corporate, medical, legal


class RegenerateSchemaRequest(BaseModel):
    """Request model for regenerating a single schema."""
    provider: Optional[str] = None
    model: Optional[str] = None
    schema_types_hint: Optional[List[str]] = None
    website_type: Optional[str] = None


class GenerateFromUrlRequest(BaseModel):
    """Request model for generating schemas from a direct URL."""
    url: str
    page_content: Optional[str] = None
    provider: str = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    website_type: Optional[str] = None
    schema_types_hint: Optional[List[str]] = None


# ============================================================================
# VALIDATION ENGINE (Rules-based, no LLM)
# ============================================================================

REQUIRED_PROPERTIES = {
    "FAQPage": ["mainEntity"],
    "Article": ["headline", "author", "datePublished"],
    "BlogPosting": ["headline", "author", "datePublished"],
    "Product": ["name"],
    "LocalBusiness": ["name", "address"],
    "Organization": ["name"],
    "WebSite": ["name", "url"],
    "HowTo": ["name", "step"],
    "Event": ["name", "startDate", "location"],
    "Service": ["name"],
    "ProfessionalService": ["name"],
    "Person": ["name"],
    "BreadcrumbList": ["itemListElement"],
    "Review": ["itemReviewed", "author"],
    "AggregateRating": ["ratingValue", "reviewCount"],
    "ContactPage": ["name"],
    "WebPage": ["name"]
}


def validate_schema(schema_json: dict) -> dict:
    """
    Validate JSON-LD against schema.org requirements.
    
    Returns:
        dict with "status" (valid|has_warnings|invalid) and "notes" (list of issues)
    """
    notes = []
    status = "valid"
    
    # Check @context
    if "@context" not in schema_json:
        notes.append({"level": "error", "message": "@context missing"})
        status = "invalid"
    elif schema_json["@context"] != "https://schema.org":
        notes.append({"level": "warning", "message": "@context should be 'https://schema.org'"})
        if status == "valid":
            status = "has_warnings"
    
    # Check @type
    schema_type = schema_json.get("@type", "")
    if not schema_type:
        notes.append({"level": "error", "message": "@type missing"})
        status = "invalid"
    
    # Check required properties for this type
    required = REQUIRED_PROPERTIES.get(schema_type, [])
    for prop in required:
        if prop not in schema_json:
            notes.append({
                "level": "warning",
                "message": f"Recommended property '{prop}' missing for {schema_type}"
            })
            if status == "valid":
                status = "has_warnings"
    
    # If no issues, mark as validated
    if not notes:
        status = "validated"
        notes.append({"level": "success", "message": "Schema is valid and ready for implementation"})
    
    return {"status": status, "notes": notes}


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def clean_json_response(text: str) -> str:
    """Strip markdown code fences from LLM JSON responses."""
    text = text.strip()
    # Remove ```json or ``` prefix
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1:]
    # Remove trailing ```
    if text.rstrip().endswith("```"):
        text = text.rstrip()[:-3]
    return text.strip()


def _find_page_text_file(website: str, result: AuditResult) -> Optional[str]:
    """
    Find and read the text content file for a page result.
    
    Returns:
        Text content or None if file not found
    """
    input_dir = os.path.join(website, "input_llm")
    
    base_name = result.filename
    if base_name.endswith('.json'):
        txt_name = re_module.sub(r'^\d+_', '', base_name).replace('.json', '.txt')
    else:
        txt_name = base_name.replace('.json', '.txt') if not base_name.endswith('.txt') else base_name
    
    txt_path = os.path.join(input_dir, txt_name)
    
    # Try exact match first
    if not os.path.exists(txt_path):
        if os.path.exists(input_dir):
            available = os.listdir(input_dir)
            # Try matching by URL part
            url_part = result.page_url.replace('https://', '').replace('http://', '')
            matches = [f for f in available if f.endswith('.txt') and url_part.replace('/', '_') in f]
            if matches:
                txt_path = os.path.join(input_dir, matches[0])
            else:
                return None
        else:
            return None
    
    try:
        with open(txt_path, 'r', encoding='utf-8') as f:
            content = f.read()
            # Limit to 4000 chars to reduce LLM cost
            return content[:4000] if len(content) > 4000 else content
    except Exception:
        return None


def build_system_prompt(website_type: Optional[str] = None, schema_types_hint: Optional[List[str]] = None) -> str:
    """Build the system prompt for schema generation."""
    
    base_prompt = """You are a Schema.org structured data expert. Analyze the page content and URL, then generate the most appropriate JSON-LD schema markup.

Rules:
1. Return ONLY a valid JSON object (no markdown fences) with these keys:
   - "schema_type": The primary Schema.org type chosen (e.g., "FAQPage", "Article", "LocalBusiness")
   - "reasoning": One sentence explaining why this type was chosen
   - "json_ld": The complete, valid JSON-LD object ready for <script type="application/ld+json">
   - "additional_schemas": Array of additional JSON-LD objects if the page warrants multiple types (e.g., FAQPage + BreadcrumbList)

2. The json_ld MUST include:
   - "@context": "https://schema.org"
   - "@type": appropriate type
   - All REQUIRED properties for that type per schema.org spec
   - As many RECOMMENDED properties as the content supports

3. Schema type selection logic:
   - Page has Q&A content → FAQPage
   - Page is a blog post/news → Article or BlogPosting
   - Page is a product → Product (with offers, reviews if available)
   - Page is a service → Service or ProfessionalService
   - Page is about a local business → LocalBusiness (with address, hours)
   - Page is a how-to guide → HowTo
   - Page has reviews/testimonials → Review or AggregateRating
   - Page is an event → Event
   - Page is a person/team → Person or AboutPage
   - Homepage → Organization or WebSite
   - Contact page → ContactPage
   - Any page → BreadcrumbList (always include as additional)

4. Extract REAL data from the page content — don't use placeholder values.
   Use actual business name, addresses, phone numbers, product names, prices, FAQ questions/answers found in the content.
   If content is insufficient for a rich schema, generate WebPage with name and description."""
    
    # Add website type hint
    if website_type:
        base_prompt += f"\n\n5. This is a {website_type} website. Prioritize schema types relevant to this business type."
    
    # Add schema type preferences
    if schema_types_hint:
        types_str = ", ".join(schema_types_hint)
        base_prompt += f"\n\n6. The user prefers these schema types when applicable: {types_str}. Use them if the content supports it."
    
    return base_prompt


async def call_llm_for_schema(
    provider: str,
    model: str,
    system_prompt: str,
    user_content: str,
    max_tokens: int = 2048,
    prefill: str = ""
) -> tuple:
    """
    Call LLM provider to generate schema markup.

    prefill: optional assistant-role prefix injected before generation (Anthropic only).
             Forces the model to continue from that string — great for locking in JSON output.

    Returns:
        Tuple of (response_text, input_tokens, output_tokens)
    """
    provider = provider.upper()

    if provider == "ANTHROPIC":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not configured")

        client = AsyncAnthropic(api_key=api_key)
        messages = [{"role": "user", "content": user_content}]
        if prefill:
            messages.append({"role": "assistant", "content": prefill})
        for _attempt in range(3):
            try:
                response = await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=messages
                )
                break
            except Exception as e:
                if _attempt == 2 or "overloaded" not in str(e).lower():
                    raise
                await asyncio.sleep(5 * (_attempt + 1))
        text = response.content[0].text
        # Re-attach the prefill so the caller gets a complete string
        if prefill:
            text = prefill + text
        return text, response.usage.input_tokens, response.usage.output_tokens

    elif provider == "OPENAI":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")

        client = AsyncOpenAI(api_key=api_key)
        for _attempt in range(3):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    response_format={"type": "json_object"}
                )
                break
            except Exception as e:
                if _attempt == 2 or "rate" not in str(e).lower():
                    raise
                await asyncio.sleep(5 * (_attempt + 1))
        return response.choices[0].message.content, response.usage.prompt_tokens, response.usage.completion_tokens

    elif provider == "MISTRAL":
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise ValueError("MISTRAL_API_KEY not configured")

        client = Mistral(api_key=api_key)
        for _attempt in range(3):
            try:
                response = await client.chat.complete_async(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    response_format={"type": "json_object"}
                )
                break
            except Exception as e:
                if _attempt == 2 or "rate" not in str(e).lower():
                    raise
                await asyncio.sleep(5 * (_attempt + 1))
        return response.choices[0].message.content, response.usage.prompt_tokens, response.usage.completion_tokens

    elif provider == "GOOGLE":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not configured")

        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(api_key=api_key)

        def _google_sync():
            resp = client.models.generate_content(
                model=model,
                contents=[
                    genai_types.Content(
                        parts=[genai_types.Part(text=f"{system_prompt}\n\n{user_content}")]
                    )
                ],
                config=genai_types.GenerateContentConfig(max_output_tokens=max_tokens),
            )
            text = resp.text or ""
            in_tok  = (resp.usage_metadata.prompt_token_count     if resp.usage_metadata else 0)
            out_tok = (resp.usage_metadata.candidates_token_count  if resp.usage_metadata else 0)
            return text, in_tok, out_tok

        for _attempt in range(3):
            try:
                result = await asyncio.to_thread(_google_sync)
                return result
            except Exception as e:
                if _attempt == 2 or "quota" not in str(e).lower():
                    raise
                await asyncio.sleep(5 * (_attempt + 1))

    elif provider == "PERPLEXITY":
        api_key = os.getenv("PERPLEXITY_API_KEY")
        if not api_key:
            raise ValueError("PERPLEXITY_API_KEY not configured")

        client = AsyncOpenAI(api_key=api_key, base_url="https://api.perplexity.ai")
        for _attempt in range(3):
            try:
                response = await client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                )
                break
            except Exception as e:
                if _attempt == 2 or "rate" not in str(e).lower():
                    raise
                await asyncio.sleep(5 * (_attempt + 1))
        return (
            response.choices[0].message.content,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )

    else:
        raise ValueError(f"Unknown provider: {provider}")


async def generate_schema_for_page(
    audit: Audit,
    result: AuditResult,
    provider: str,
    model: str,
    website_type: Optional[str],
    schema_types_hint: Optional[List[str]]
) -> Optional[SchemaMarkup]:
    """
    Generate schema markup for a single page.
    
    Returns:
        SchemaMarkup object or None if generation failed
    """
    async with AsyncSessionLocal() as db:
        try:
            # Load page content
            page_content = _find_page_text_file(audit.website, result)
            if not page_content:
                print(f"⚠️ No text content found for {result.page_url}")
                return None
            
            # Build prompts
            system_prompt = build_system_prompt(website_type, schema_types_hint)
            
            # Load audit result context
            result_context = ""
            if result.result_json:
                try:
                    result_data = json.loads(result.result_json)
                    result_context = f"\nPage Score: {result.score}\nClassification: {result.classification}\n"
                    if "optimization_opportunities" in result_data:
                        result_context += f"Optimization Opportunities: {json.dumps(result_data['optimization_opportunities'][:3])}\n"
                except Exception as _ex:
                    print(f"[schema_gen] Warning: failed to parse result_json context for {result.page_url}: {_ex}")
            
            user_content = f"""Page URL: {result.page_url}
{result_context}
Page Content (first 4000 chars):
{page_content}

Generate appropriate JSON-LD schema markup for this page."""
            
            # Call LLM
            response, input_tokens, output_tokens = await call_llm_for_schema(provider, model, system_prompt, user_content)
            response = clean_json_response(response)

            # Track cost (fire-and-forget)
            asyncio.create_task(track_cost(
                source="schema",
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                audit_id=audit.id,
                website=audit.website
            ))
            
            # Parse response
            try:
                llm_output = json.loads(response)
            except json.JSONDecodeError as e:
                print(f"❌ JSON parse error for {result.page_url}: {e}")
                return None
            
            # Extract main schema
            schema_type = llm_output.get("schema_type", "WebPage")
            json_ld = llm_output.get("json_ld", {})
            
            # Validate schema
            validation_result = validate_schema(json_ld)
            
            # Create SchemaMarkup entry for primary schema
            markup = SchemaMarkup(
                audit_id=audit.id,
                result_id=result.id,
                page_url=result.page_url,
                schema_type=schema_type,
                schema_json=json.dumps(json_ld, indent=2),
                validation_status=validation_result["status"],
                validation_notes=json.dumps(validation_result["notes"]),
                provider=provider,
                model=model
            )
            db.add(markup)

            # Also save additional schemas returned by the LLM
            for add_schema in llm_output.get("additional_schemas", []):
                if isinstance(add_schema, dict) and "@type" in add_schema:
                    add_type = add_schema.get("@type", "WebPage")
                    add_validation = validate_schema(add_schema)
                    db.add(SchemaMarkup(
                        audit_id=audit.id,
                        result_id=result.id,
                        page_url=result.page_url,
                        schema_type=add_type,
                        schema_json=json.dumps(add_schema, indent=2),
                        validation_status=add_validation["status"],
                        validation_notes=json.dumps(add_validation["notes"]),
                        provider=provider,
                        model=model
                    ))

            await db.commit()
            await db.refresh(markup)

            print(f"✅ Generated {schema_type} schema for {result.page_url} - Status: {validation_result['status']}")

            return markup
            
        except Exception as e:
            print(f"❌ Error generating schema for {result.page_url}: {e}")
            return None


# ============================================================================
# API ENDPOINTS
# ============================================================================

@router.post("/generate")
@limiter.limit("20/hour")  # each call makes N LLM requests (one per page) — costs credits
async def generate_schemas(http_request: Request, request: GenerateSchemaRequest):
    """
    Generate schema markup for pages in an audit.
    
    Runs in background and generates JSON-LD for each selected page.
    """
    async with AsyncSessionLocal() as db:
        # Get audit
        audit_result = await db.execute(
            select(Audit).where(Audit.id == request.audit_id)
        )
        audit = audit_result.scalar_one_or_none()
        if not audit:
            raise_not_found("Audit")
        
        if audit.status != "completed":
            raise_bad_request("Audit must be completed first")
        
        # Determine provider and model
        provider = request.provider or audit.provider
        model = request.model or audit.model
        
        # Default to cheaper models if no model specified
        if not request.model:
            _default_models = {
                "ANTHROPIC":  "claude-haiku-4-5-20251001",
                "OPENAI":     "gpt-4o-mini",
                "MISTRAL":    "mistral-small-latest",
                "GOOGLE":     "gemini-2.0-flash-lite",
                "PERPLEXITY": "sonar",
            }
            model = _default_models.get(provider.upper(), model)
        
        # Get results to process
        query = select(AuditResult).where(AuditResult.audit_id == request.audit_id)
        
        if request.result_ids:
            query = query.where(AuditResult.id.in_(request.result_ids))
        else:
            # Auto-select all pages, prioritize lower scores
            query = query.order_by(AuditResult.score.asc())
        
        query = query.limit(request.max_pages)
        
        results_query = await db.execute(query)
        results = results_query.scalars().all()
        
        if not results:
            raise HTTPException(status_code=404, detail="No results found to process")
        
        # Estimate cost
        pages_count = len(results)
        _cost_map = {
            "ANTHROPIC":  0.005,
            "OPENAI":     0.0008,
            "MISTRAL":    0.0004,
            "GOOGLE":     0.0003,
            "PERPLEXITY": 0.0010,
        }
        cost_per_page = _cost_map.get(provider.upper(), 0.005)  # rough estimates
        estimated_cost = pages_count * cost_per_page
        
        # Launch background task
        asyncio.create_task(
            _generate_schemas_background(
                audit, results, provider, model,
                request.website_type, request.schema_types_hint
            )
        )
        
        return {
            "message": "Schema generation started",
            "audit_id": request.audit_id,
            "pages_count": pages_count,
            "estimated_cost": f"${estimated_cost:.3f}",
            "provider": provider,
            "model": model
        }


async def _generate_schemas_background(
    audit: Audit,
    results: List[AuditResult],
    provider: str,
    model: str,
    website_type: Optional[str],
    schema_types_hint: Optional[List[str]]
):
    """Background task to generate schemas for all pages."""
    print(f"🚀 Starting schema generation for {len(results)} pages...")

    success_count = 0
    for result in results:
        markup = await generate_schema_for_page(
            audit, result, provider, model, website_type, schema_types_hint
        )
        if markup:
            success_count += 1

        # Small delay to avoid rate limits
        await asyncio.sleep(0.5)

    print(f"✅ Schema generation completed: {success_count}/{len(results)} successful")


async def generate_schemas_for_url(
    url: str,
    page_content: Optional[str],
    provider: str,
    model: str,
    website_type: Optional[str],
    schema_types_hint: Optional[List[str]]
) -> List[dict]:
    """
    Generate all applicable schemas for a single URL (no audit required).

    Returns:
        List of schema dicts (serialised, safe to return after session closes)
    """
    async with AsyncSessionLocal() as db:
        try:
            system_prompt = build_system_prompt(website_type, schema_types_hint)

            content_section = (
                page_content[:4000] if page_content
                else "(No page content provided — generate the most likely schemas based on the URL alone.)"
            )

            user_content = f"""Page URL: {url}

Page Content:
{content_section}

Generate ALL applicable JSON-LD schema types for this page. The primary schema should go in "json_ld" and every additional applicable type in "additional_schemas"."""

            response, input_tokens, output_tokens = await call_llm_for_schema(
                provider, model, system_prompt, user_content, max_tokens=4096
            )
            response = clean_json_response(response)

            asyncio.create_task(track_cost(
                source="schema",
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                audit_id=None,
                website=url
            ))

            try:
                llm_output = json.loads(response)
            except json.JSONDecodeError as e:
                print(f"❌ JSON parse error for {url}: {e}")
                return []

            saved_dicts = []

            # Helper to persist one schema row and collect its dict
            async def _save(schema_json_obj: dict, schema_type_override: Optional[str] = None):
                stype = schema_type_override or schema_json_obj.get("@type", "WebPage")
                val = validate_schema(schema_json_obj)
                m = SchemaMarkup(
                    audit_id=None,
                    result_id=None,
                    page_url=url,
                    schema_type=stype,
                    schema_json=json.dumps(schema_json_obj, indent=2),
                    validation_status=val["status"],
                    validation_notes=json.dumps(val["notes"]),
                    provider=provider,
                    model=model
                )
                db.add(m)
                return m

            # Primary schema
            primary_type = llm_output.get("schema_type", "WebPage")
            primary_json = llm_output.get("json_ld", {})
            primary = await _save(primary_json, primary_type)

            # Additional schemas
            extras = []
            for add_schema in llm_output.get("additional_schemas", []):
                if isinstance(add_schema, dict) and "@type" in add_schema:
                    extras.append(await _save(add_schema))

            await db.commit()

            # Refresh and serialise while session is still open
            for m in [primary] + extras:
                await db.refresh(m)
                saved_dicts.append(m.to_dict())

            print(f"✅ URL schema generation: {len(saved_dicts)} schema(s) for {url}")
            return saved_dicts

        except Exception as e:
            print(f"❌ Error generating schemas for {url}: {e}")
            return []


@router.post("/generate-from-url")
async def generate_schemas_from_url(request: GenerateFromUrlRequest):
    """
    Generate all applicable schema types for a single URL.

    No audit required — accepts a URL and optional page content directly.
    Returns results synchronously.
    """
    if not request.url.startswith(("http://", "https://")):
        raise_bad_request("URL must start with http:// or https://")

    schemas = await generate_schemas_for_url(
        url=request.url,
        page_content=request.page_content,
        provider=request.provider,
        model=request.model,
        website_type=request.website_type,
        schema_types_hint=request.schema_types_hint
    )

    if not schemas:
        raise HTTPException(status_code=500, detail="Schema generation failed. Check server logs.")

    return {"schemas": schemas, "count": len(schemas), "page_url": request.url}


@router.get("")
async def list_schemas(audit_id: Optional[str] = None, source: Optional[str] = None, page_url: Optional[str] = None):
    """
    List schema markups.

    Filters (mutually exclusive, checked in order):
    - audit_id: schemas belonging to a specific audit
    - page_url: schemas for a specific URL (across all sources)
    - source=url: URL-generated schemas (no audit)
    - (none): all schemas
    """
    async with AsyncSessionLocal() as db:
        stmt = select(SchemaMarkup).order_by(SchemaMarkup.page_url, SchemaMarkup.created_at.desc())

        if audit_id:
            stmt = stmt.where(SchemaMarkup.audit_id == audit_id)
        elif page_url:
            stmt = stmt.where(SchemaMarkup.page_url == page_url)
        elif source == "url":
            stmt = stmt.where(SchemaMarkup.audit_id == None)  # noqa: E711

        query = await db.execute(stmt)
        markups = query.scalars().all()

        result = []
        for markup in markups:
            preview = markup.schema_json[:100] + "..." if len(markup.schema_json) > 100 else markup.schema_json
            result.append({
                "id": markup.id,
                "page_url": markup.page_url,
                "schema_type": markup.schema_type,
                "validation_status": markup.validation_status,
                "preview": preview,
                "created_at": markup.created_at.isoformat() if markup.created_at else None
            })

        return {"schemas": result, "count": len(result)}


@router.get("/{markup_id}")
async def get_schema(markup_id: int):
    """
    Get complete schema markup with JSON-LD.
    
    Returns:
        Full schema markup object
    """
    async with AsyncSessionLocal() as db:
        query = await db.execute(
            select(SchemaMarkup).where(SchemaMarkup.id == markup_id)
        )
        markup = query.scalar_one_or_none()
        
        if not markup:
            raise_not_found("Schema markup")
        
        return markup.to_dict()


@router.get("/{markup_id}/raw")
async def get_schema_raw(markup_id: int):
    """
    Get ONLY the JSON-LD markup, ready for copy-paste.
    
    Returns:
        Plain JSON (Content-Type: application/json)
    """
    async with AsyncSessionLocal() as db:
        query = await db.execute(
            select(SchemaMarkup).where(SchemaMarkup.id == markup_id)
        )
        markup = query.scalar_one_or_none()
        
        if not markup:
            raise_not_found("Schema markup")
        
        # Return raw JSON with proper content type
        return Response(
            content=markup.schema_json,
            media_type="application/json"
        )


@router.post("/{markup_id}/regenerate")
async def regenerate_schema(markup_id: int, request: RegenerateSchemaRequest):
    """
    Regenerate schema markup with different parameters.
    
    Replaces existing markup with newly generated one.
    """
    async with AsyncSessionLocal() as db:
        # Get existing markup
        query = await db.execute(
            select(SchemaMarkup).where(SchemaMarkup.id == markup_id)
        )
        old_markup = query.scalar_one_or_none()
        
        if not old_markup:
            raise_not_found("Schema markup")
        
        # Get audit and result
        audit_query = await db.execute(
            select(Audit).where(Audit.id == old_markup.audit_id)
        )
        audit = audit_query.scalar_one_or_none()
        
        result_query = await db.execute(
            select(AuditResult).where(AuditResult.id == old_markup.result_id)
        )
        result = result_query.scalar_one_or_none()
        
        if not audit or not result:
            raise_not_found("Related audit or result")
        
        # Determine parameters
        provider = request.provider or old_markup.provider or audit.provider
        model = request.model or old_markup.model or audit.model
        
        # Delete old markup
        await db.delete(old_markup)
        await db.commit()
        
        # Generate new one
        new_markup = await generate_schema_for_page(
            audit, result, provider, model,
            request.website_type, request.schema_types_hint
        )
        
        if not new_markup:
            raise HTTPException(status_code=500, detail="Failed to regenerate schema")
        
        return new_markup.to_dict()


@router.post("/{markup_id}/validate")
async def validate_schema_endpoint(markup_id: int):
    """
    Validate schema markup against Schema.org requirements.
    
    Returns:
        Validation status and notes
    """
    async with AsyncSessionLocal() as db:
        query = await db.execute(
            select(SchemaMarkup).where(SchemaMarkup.id == markup_id)
        )
        markup = query.scalar_one_or_none()
        
        if not markup:
            raise_not_found("Schema markup")
        
        # Parse and validate
        try:
            schema_json = json.loads(markup.schema_json)
        except json.JSONDecodeError:
            return {
                "status": "invalid",
                "notes": [{"level": "error", "message": "Invalid JSON"}]
            }
        
        validation_result = validate_schema(schema_json)
        
        # Update markup with validation results
        markup.validation_status = validation_result["status"]
        markup.validation_notes = json.dumps(validation_result["notes"])
        await db.commit()
        
        return validation_result


@router.get("/export/{audit_id}")
async def export_schemas(audit_id: str, format: str = "json"):
    """
    Export all schema markups for an audit.
    
    Args:
        format: "json" or "html"
    
    Returns:
        JSON array or HTML snippets
    """
    async with AsyncSessionLocal() as db:
        query = await db.execute(
            select(SchemaMarkup)
            .where(SchemaMarkup.audit_id == audit_id)
            .order_by(SchemaMarkup.page_url)
        )
        markups = query.scalars().all()
        
        if not markups:
            raise HTTPException(status_code=404, detail="No schemas found for this audit")
        
        if format == "html":
            # Generate HTML snippets
            html_parts = []
            for markup in markups:
                html_parts.append(f"""<!-- Schema for {markup.page_url} -->
<script type="application/ld+json">
{markup.schema_json}
</script>
""")
            
            html_output = "\n".join(html_parts)
            return Response(
                content=html_output,
                media_type="text/html",
                headers={"Content-Disposition": f"attachment; filename=schemas_{audit_id}.html"}
            )
        else:
            # JSON format
            schemas_data = []
            for markup in markups:
                schemas_data.append({
                    "page_url": markup.page_url,
                    "schema_type": markup.schema_type,
                    "validation_status": markup.validation_status,
                    "json_ld": json.loads(markup.schema_json)
                })
            
            return {"audit_id": audit_id, "schemas": schemas_data, "count": len(schemas_data)}
