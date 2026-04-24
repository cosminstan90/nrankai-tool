import os
import json
import asyncio
import logging
from typing import Optional, List

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import true

from api.limiter import limiter
from api.models.database import get_db
from api.models.analytics import GscQueryRow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/meta-generator", tags=["meta_generator"])

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ScrapeRequest(BaseModel):
    url: str


class ScrapeResponse(BaseModel):
    content: str
    current_title: Optional[str] = None
    current_meta: Optional[str] = None
    current_h1: Optional[str] = None


class GenerateRequest(BaseModel):
    url: Optional[str] = None
    content: str = Field(..., min_length=10, max_length=20000)
    current_title: Optional[str] = None
    current_meta: Optional[str] = None
    current_h1: Optional[str] = None
    primary_keyword: str = Field(..., min_length=1, max_length=200)
    keywords: List[str] = Field(default_factory=list, max_length=50)
    brand_name: Optional[str] = Field(None, max_length=100)
    language: str = "ro"
    provider: str = "openai"
    model: str = "gpt-4o-mini"


class MetaVariant(BaseModel):
    text: str
    chars: int


class GenerateResponse(BaseModel):
    titles: List[MetaVariant]
    meta_descriptions: List[MetaVariant]
    h1: str
    h2_suggestions: List[str]
    tokens_used: int
    cost_usd: float


# ---------------------------------------------------------------------------
# Helper: load prompt template
# ---------------------------------------------------------------------------

def _load_prompt() -> str:
    prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompts", "meta_generator.txt")
    with open(os.path.normpath(prompt_path), "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/scrape", response_model=ScrapeResponse)
async def scrape_url(body: ScrapeRequest):
    """Scrape a URL and return current on-page elements + body text."""
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            resp = await client.get(body.url)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=422, detail=f"Failed to fetch URL: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Meta generator scrape error for {body.url}: {e}")
        raise HTTPException(status_code=422, detail=f"Could not fetch URL: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")

    current_title: Optional[str] = None
    title_tag = soup.find("title")
    if title_tag:
        current_title = title_tag.get_text(strip=True) or None

    current_meta: Optional[str] = None
    meta_tag = soup.find("meta", attrs={"name": "description"})
    if meta_tag and meta_tag.get("content"):
        current_meta = meta_tag["content"].strip() or None

    current_h1: Optional[str] = None
    h1_tag = soup.find("h1")
    if h1_tag:
        current_h1 = h1_tag.get_text(strip=True) or None

    raw_text = soup.get_text(separator=" ", strip=True)
    if len(raw_text) > 8000:
        content = raw_text[:6000] + " [...] " + raw_text[-1500:]
    else:
        content = raw_text

    return ScrapeResponse(
        content=content,
        current_title=current_title,
        current_meta=current_meta,
        current_h1=current_h1,
    )


@router.get("/gsc-keywords")
async def get_gsc_keywords(url: str, db: AsyncSession = Depends(get_db)):
    """Return top 20 most-clicked GSC queries from local DB."""
    try:
        result = await db.execute(
            select(GscQueryRow.query, GscQueryRow.clicks)
            .where(GscQueryRow.query.isnot(None))
            .order_by(desc(GscQueryRow.clicks))
            .limit(20)
        )
        rows = result.all()
    except Exception as e:
        logger.error(f"GSC keywords DB error: {e}")
        return {"keywords": [], "message": "No GSC data available. Connect GSC first."}

    if not rows:
        return {"keywords": [], "message": "No GSC data available. Connect GSC first."}

    return {"keywords": [{"query": r.query, "clicks": r.clicks} for r in rows]}


@router.post("/generate", response_model=GenerateResponse)
@limiter.limit("30/hour")
async def generate_meta(request: Request, body: GenerateRequest):
    """Generate 3 title/meta variants + H1 + H2 suggestions via LLM."""
    provider = body.provider.lower()
    model = body.model
    url = body.url or ""

    # Build prompt
    prompt_template = _load_prompt()
    keywords_str = ", ".join(body.keywords) if body.keywords else "none"
    brand_name_str = body.brand_name or ""
    prompt = prompt_template.format(
        content=body.content,
        primary_keyword=body.primary_keyword,
        keywords=keywords_str,
        brand_name=brand_name_str,
        language=body.language,
    )

    # LLM call
    response_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    try:
        if provider == "openai":
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            response = await client.chat.completions.create(
                model=model,
                max_tokens=2000,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = response.choices[0].message.content
            input_tokens = response.usage.prompt_tokens
            output_tokens = response.usage.completion_tokens

        elif provider == "mistral":
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=os.getenv("MISTRAL_API_KEY"),
                base_url="https://api.mistral.ai/v1",
            )
            response = await client.chat.completions.create(
                model=model,
                max_tokens=2000,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = response.choices[0].message.content
            input_tokens = response.usage.prompt_tokens
            output_tokens = response.usage.completion_tokens

        elif provider == "anthropic":
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            response = await client.messages.create(
                model=model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = response.content[0].text
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens

        elif provider == "google":
            import google.generativeai as genai
            genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
            gemini_model = genai.GenerativeModel(model)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: gemini_model.generate_content(prompt)
            )
            response_text = response.text
            input_tokens = 0
            output_tokens = 0

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Meta generator LLM error: {e}")
        raise HTTPException(status_code=502, detail="AI service error")

    # Parse JSON response
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error(f"Meta generator JSON parse error: {e}. Raw: {response_text[:500]}")
        raise HTTPException(status_code=502, detail="AI returned invalid JSON")

    # Normalise titles — ensure `chars` field present
    titles: List[MetaVariant] = []
    for item in data.get("titles", []):
        text = item.get("text", "")
        chars = item.get("chars", len(text))
        titles.append(MetaVariant(text=text, chars=chars))

    # Normalise meta descriptions
    meta_descriptions: List[MetaVariant] = []
    for item in data.get("meta_descriptions", []):
        text = item.get("text", "")
        chars = item.get("chars", len(text))
        meta_descriptions.append(MetaVariant(text=text, chars=chars))

    h1: str = data.get("h1", "")
    h2_suggestions: List[str] = data.get("h2_suggestions", [])

    total_tokens = input_tokens + output_tokens

    # Fire-and-forget cost tracking
    try:
        from api.routes.costs import track_cost
        await track_cost(
            source="meta_generator",
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            website=url,
        )
    except Exception as e:
        logger.warning(f"Meta generator cost tracking failed: {e}")

    # Compute cost estimate for response (best-effort; costs module may differ)
    cost_usd: float = 0.0
    try:
        from api.routes.costs import calculate_cost
        cost_usd = calculate_cost(provider, model, input_tokens, output_tokens)
    except Exception:
        pass

    return GenerateResponse(
        titles=titles,
        meta_descriptions=meta_descriptions,
        h1=h1,
        h2_suggestions=h2_suggestions,
        tokens_used=total_tokens,
        cost_usd=cost_usd,
    )
