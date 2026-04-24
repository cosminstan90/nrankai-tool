import json
import logging
import os
from typing import Dict, List

from anthropic import AsyncAnthropic
from fastapi import APIRouter, HTTPException, Request
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from api.limiter import limiter
from api.provider_registry import calculate_cost
from api.routes.costs import track_cost

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai-visibility", tags=["query_suggestions"])

SUGGEST_QUERIES_PROMPT = """You are an SEO/GEO specialist. Generate {count} search queries that a user might ask an AI assistant (ChatGPT, Perplexity, Google Gemini) when looking for information in the {industry} industry, where the brand {brand_name} ({website}) would ideally appear as a recommendation or citation.

Generate queries in {language}. Mix query types:
- Direct brand queries (20%): brand name + review/opinion/experience
- Category queries (40%): generic "{industry} best X" style, no brand mentioned
- Problem-based queries (30%): "how do I X in {industry}"
- Comparison queries (10%): "X vs Y" where Y might be a competitor

Return ONLY valid JSON in this exact format:
{{"queries": ["query1", "query2", ...], "categories": {{"brand_direct": [...], "category_generic": [...], "problem_based": [...], "competitor_comparison": [...]}}}}"""


class SuggestQueriesRequest(BaseModel):
    brand_name: str = Field(..., min_length=1, max_length=100)
    website: str = Field(..., min_length=1, max_length=255)
    industry: str = Field(..., min_length=1, max_length=100)
    language: str = Field(default="ro", max_length=10)
    count: int = Field(default=20, ge=5, le=50)


class SuggestQueriesResponse(BaseModel):
    queries: List[str]
    categories: Dict[str, List[str]]
    tokens_used: int
    cost_usd: float


async def _call_anthropic(prompt: str) -> tuple:
    client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text, response.usage.input_tokens, response.usage.output_tokens, "anthropic", "claude-haiku-4-5-20251001"


async def _call_openai(prompt: str) -> tuple:
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=2000,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content, response.usage.prompt_tokens, response.usage.completion_tokens, "openai", "gpt-4o-mini"


@router.post("/suggest-queries", response_model=SuggestQueriesResponse)
@limiter.limit("10/hour")
async def suggest_queries(request: Request, body: SuggestQueriesRequest):
    """Generate probe queries for GeoMonitor/CitationTracker using LLM."""
    prompt = SUGGEST_QUERIES_PROMPT.format(
        count=body.count,
        industry=body.industry,
        brand_name=body.brand_name,
        website=body.website,
        language=body.language,
    )

    try:
        if os.getenv("ANTHROPIC_API_KEY"):
            text, input_tok, output_tok, provider, model = await _call_anthropic(prompt)
        elif os.getenv("OPENAI_API_KEY"):
            text, input_tok, output_tok, provider, model = await _call_openai(prompt)
        else:
            raise HTTPException(status_code=503, detail="No LLM API key configured (set ANTHROPIC_API_KEY or OPENAI_API_KEY)")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Query suggestion LLM error: {e}")
        raise HTTPException(status_code=502, detail="AI service error")

    try:
        data = json.loads(text)
        queries = [str(q) for q in data.get("queries", [])]
        categories = {k: [str(v) for v in vs] for k, vs in data.get("categories", {}).items()}
    except (json.JSONDecodeError, AttributeError, TypeError):
        logger.error(f"Failed to parse LLM response as JSON: {text[:300]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON response")

    cost = calculate_cost(provider, model, input_tok, output_tok)
    await track_cost(
        source="query_suggestions",
        provider=provider,
        model=model,
        input_tokens=input_tok,
        output_tokens=output_tok,
        website=body.website,
    )

    return SuggestQueriesResponse(
        queries=queries,
        categories=categories,
        tokens_used=input_tok + output_tok,
        cost_usd=cost,
    )
