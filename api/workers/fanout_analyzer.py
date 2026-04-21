"""
WLA Fan-Out Analyzer — Core Engine.

Reverse-engineers the search queries that AI engines fire internally when
answering a user prompt, revealing the "retrieval surface" needed to appear
in AI-generated answers.

Supports:
  - OpenAI  : Responses API with built-in web_search tool
  - Anthropic: Messages API agentic loop with web_search_20250305 tool

Usage (standalone test):
    python -m api.workers.fanout_analyzer "best seo agency romania"
    python -m api.workers.fanout_analyzer "best seo agency romania" --provider anthropic
"""

import os
import sys
import json
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv, find_dotenv
from pathlib import Path

# find_dotenv() walks up the directory tree until it finds .env
# Works correctly both from the main project and from a git worktree
_dotenv_path = find_dotenv(usecwd=False) or str(Path(__file__).parent.parent.parent / ".env")
load_dotenv(_dotenv_path)

logger = logging.getLogger("fanout_analyzer")


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class FanoutSource:
    url: str
    title: str
    domain: str = ""
    snippet: str = ""

    def __post_init__(self):
        if not self.domain and self.url:
            try:
                self.domain = urlparse(self.url).netloc.lstrip("www.")
            except Exception:
                self.domain = ""


@dataclass
class FanoutResult:
    prompt: str
    provider: str                          # "openai" | "anthropic"
    model: str
    fanout_queries: List[str] = field(default_factory=list)
    sources: List[FanoutSource] = field(default_factory=list)
    search_call_count: int = 0
    total_fanout_queries: int = 0
    total_sources: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_output: list = field(default_factory=list)  # raw items for debugging

    def finalize(self):
        self.total_fanout_queries = len(self.fanout_queries)
        self.total_sources = len(self.sources)
        return self


# ============================================================================
# OPENAI PROVIDER
# ============================================================================

def _extract_openai_queries(output: list) -> List[str]:
    """Extract unique search queries from OpenAI Responses API output items."""
    queries = []
    seen = set()
    for item in output:
        item_type = getattr(item, "type", None) or item.get("type", "")
        if item_type == "web_search_call":
            # SDK objects have attributes; dicts have keys
            query = None
            if hasattr(item, "action"):
                action = item.action
                query = getattr(action, "query", None)
            elif isinstance(item, dict):
                action = item.get("action", {})
                query = action.get("query") or item.get("query")
            if query and query not in seen:
                seen.add(query)
                queries.append(query)
    return queries


def _extract_openai_sources(output: list) -> List[FanoutSource]:
    """Extract cited sources from OpenAI Responses API message annotations."""
    sources = []
    seen_urls = set()
    for item in output:
        item_type = getattr(item, "type", None) or item.get("type", "")
        if item_type != "message":
            continue
        content_blocks = (
            getattr(item, "content", []) if hasattr(item, "content") else item.get("content", [])
        )
        for block in content_blocks:
            annotations = (
                getattr(block, "annotations", []) if hasattr(block, "annotations")
                else block.get("annotations", [])
            )
            for ann in annotations:
                ann_type = getattr(ann, "type", None) or ann.get("type", "")
                if ann_type == "url_citation":
                    url = getattr(ann, "url", None) or ann.get("url", "")
                    title = getattr(ann, "title", None) or ann.get("title", "") or ""
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        sources.append(FanoutSource(url=url, title=title))
    return sources


async def _analyze_openai(
    prompt: str,
    model: str,
    user_location: Optional[str],
) -> FanoutResult:
    from openai import AsyncOpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set")

    client = AsyncOpenAI(api_key=api_key)
    full_prompt = f"{prompt} (location: {user_location})" if user_location else prompt

    last_exc = None
    for attempt in range(3):
        try:
            response = await client.responses.create(
                model=model,
                input=full_prompt,
                tools=[{"type": "web_search"}],
            )
            break
        except Exception as exc:
            last_exc = exc
            if "rate_limit" in str(exc).lower() or "429" in str(exc):
                wait = 2 ** attempt * 5
                logger.warning("OpenAI rate limit, retrying in %ss", wait)
                await asyncio.sleep(wait)
            else:
                raise
    else:
        raise last_exc

    raw = list(response.output)
    queries = _extract_openai_queries(raw)
    sources = _extract_openai_sources(raw)
    search_calls = sum(
        1 for item in raw
        if (getattr(item, "type", None) or item.get("type", "")) == "web_search_call"
    )

    result = FanoutResult(
        prompt=prompt,
        provider="openai",
        model=model,
        fanout_queries=queries,
        sources=sources,
        search_call_count=search_calls,
        raw_output=raw,
    )
    return result.finalize()


# ============================================================================
# ANTHROPIC PROVIDER
# ============================================================================

async def _anthropic_call(client, **kwargs) -> object:
    """Single Anthropic API call with exponential backoff on rate limits."""
    last_exc = None
    for attempt in range(3):
        try:
            return await client.messages.create(**kwargs)
        except Exception as exc:
            last_exc = exc
            if "rate_limit" in str(exc).lower() or "529" in str(exc) or "429" in str(exc):
                wait = 2 ** attempt * 5
                logger.warning("Anthropic rate limit, retrying in %ss", wait)
                await asyncio.sleep(wait)
            else:
                raise
    raise last_exc


async def _analyze_anthropic(
    prompt: str,
    model: str,
    user_location: Optional[str],
) -> FanoutResult:
    """
    Two-phase Anthropic fan-out analysis.

    Phase 1 — Query prediction (no web search):
        Ask Claude which search queries it would fire to answer the prompt.
        The web_search_20250305 tool is server-side and does NOT expose
        tool_use blocks, so queries must be predicted explicitly.

    Phase 2 — Source retrieval (web search enabled):
        Run the actual search with the tool enabled and extract citations
        from the text block annotations.
    """
    from anthropic import AsyncAnthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = AsyncAnthropic(api_key=api_key)
    full_prompt = f"{prompt} (location: {user_location})" if user_location else prompt

    raw_output = []

    # ------------------------------------------------------------------
    # Phase 1: predict fan-out queries (no web search tool)
    # ------------------------------------------------------------------
    logger.info("Anthropic phase 1: predicting fan-out queries")
    phase1_system = (
        "You are an AI search behavior analyst. "
        "Your task is to list the exact web search queries you would fire "
        "to answer the user's question comprehensively. "
        "Return ONLY a numbered list of search queries, one per line, "
        "with NO extra commentary. Example format:\n"
        "1. query one\n"
        "2. query two\n"
        "Be exhaustive — list every distinct query you would use."
    )
    phase1_resp = await _anthropic_call(
        client,
        model=model,
        max_tokens=1024,
        system=phase1_system,
        messages=[{"role": "user", "content": f"Question: {full_prompt}"}],
    )
    raw_output.append({"phase": 1, "response": phase1_resp.model_dump()})

    # Parse numbered list from the text response
    all_queries: List[str] = []
    seen_queries: set = set()
    for block in phase1_resp.content:
        if block.type == "text":
            for line in block.text.splitlines():
                line = line.strip()
                # Strip leading number + dot/paren: "1. query" or "1) query"
                import re as _re
                cleaned = _re.sub(r"^\d+[\.\)]\s*", "", line).strip()
                if cleaned and cleaned not in seen_queries:
                    seen_queries.add(cleaned)
                    all_queries.append(cleaned)

    logger.info("Phase 1 extracted %d queries", len(all_queries))

    # ------------------------------------------------------------------
    # Phase 2: web search to retrieve actual sources + citation URLs
    # ------------------------------------------------------------------
    logger.info("Anthropic phase 2: web search for sources")
    all_sources: List[FanoutSource] = []
    seen_urls: set = set()

    phase2_resp = await _anthropic_call(
        client,
        model=model,
        max_tokens=4096,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": full_prompt}],
    )
    raw_output.append({"phase": 2, "response": phase2_resp.model_dump()})

    for block in phase2_resp.content:
        if block.type == "text":
            citations = getattr(block, "citations", None) or []
            for cit in citations:
                url = getattr(cit, "url", None) or ""
                title = getattr(cit, "title", None) or ""
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_sources.append(FanoutSource(url=url, title=title))

    logger.info("Phase 2 extracted %d sources", len(all_sources))

    result = FanoutResult(
        prompt=prompt,
        provider="anthropic",
        model=model,
        fanout_queries=all_queries,
        sources=all_sources,
        search_call_count=len(all_queries),  # one search per predicted query
        raw_output=raw_output,
    )
    return result.finalize()


# ============================================================================
# PERPLEXITY PROVIDER
# ============================================================================

def _infer_queries_from_text(text: str) -> List[str]:
    """
    Heuristically infer what search queries Perplexity fired internally.

    Perplexity doesn't expose internal queries, so we extract implied topics
    from noun phrases and entity mentions in the response text.
    """
    import re
    queries: List[str] = []
    seen: set = set()

    # Split into sentences
    sentences = re.split(r"[.!?]+", text)
    for sent in sentences:
        sent = sent.strip()
        if len(sent) < 10 or len(sent) > 300:
            continue
        # Capture: "According to X", "Based on X", proper-noun runs (2-4 capitalized words)
        patterns = [
            r"(?:According to|Based on|As per)\s+([^,]{5,60})",
            r"\b([A-Z][a-z]{2,}\s+(?:[A-Z][a-z]{2,}\s+){0,2}[A-Z][a-z]{2,})\b",
        ]
        for pattern in patterns:
            for m in re.findall(pattern, sent):
                m = m.strip()
                if 8 < len(m) < 60 and m.lower() not in seen:
                    seen.add(m.lower())
                    queries.append(m)
        if len(queries) >= 10:
            break

    return queries[:10]


async def _analyze_perplexity(
    prompt: str,
    model: str,
    user_location: Optional[str],
) -> FanoutResult:
    from openai import AsyncOpenAI

    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        raise ValueError("PERPLEXITY_API_KEY not set")

    client = AsyncOpenAI(api_key=api_key, base_url="https://api.perplexity.ai")
    full_prompt = f"{prompt} (location: {user_location})" if user_location else prompt

    last_exc = None
    for attempt in range(3):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a research assistant. Provide comprehensive, factual answers. "
                            "Include specific names, brands, websites and sources when relevant."
                        ),
                    },
                    {"role": "user", "content": full_prompt},
                ],
                max_tokens=2000,
                temperature=0.1,
            )
            break
        except Exception as exc:
            last_exc = exc
            if "rate_limit" in str(exc).lower() or "429" in str(exc):
                wait = 2 ** attempt * 5
                logger.warning("Perplexity rate limit, retrying in %ss", wait)
                await asyncio.sleep(wait)
            else:
                raise
    else:
        raise last_exc

    response_text = response.choices[0].message.content or ""

    # Citations: Perplexity returns them as a list of URLs on the response object
    sources: List[FanoutSource] = []
    seen_urls: set = set()
    raw_citations = getattr(response, "citations", None) or []
    for citation in raw_citations:
        url = citation if isinstance(citation, str) else getattr(citation, "url", str(citation))
        if url and url not in seen_urls:
            seen_urls.add(url)
            sources.append(FanoutSource(url=url, title=""))

    inferred_queries = _infer_queries_from_text(response_text)

    result = FanoutResult(
        prompt=prompt,
        provider="perplexity",
        model=model,
        fanout_queries=inferred_queries,
        sources=sources,
        search_call_count=1,
        raw_output=[{"response_text": response_text[:500], "citation_count": len(sources)}],
    )
    return result.finalize()


# ============================================================================
# GEMINI PROVIDER
# ============================================================================

async def _analyze_gemini(
    prompt: str,
    model: str,
    user_location: Optional[str],
) -> FanoutResult:
    from google import genai
    from google.genai import types as genai_types

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=api_key)
    full_prompt = f"{prompt} (location: {user_location})" if user_location else prompt

    last_exc = None
    for attempt in range(3):
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=full_prompt,
                config=genai_types.GenerateContentConfig(
                    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                ),
            )
            break
        except Exception as exc:
            last_exc = exc
            if any(kw in str(exc).lower() for kw in ("rate_limit", "429", "quota")):
                wait = 2 ** attempt * 5
                logger.warning("Gemini rate limit, retrying in %ss", wait)
                await asyncio.sleep(wait)
            else:
                raise
    else:
        raise last_exc

    fanout_queries: List[str] = []
    sources: List[FanoutSource] = []
    seen_urls: set = set()
    search_call_count = 0

    try:
        candidate = response.candidates[0] if response.candidates else None
        if candidate:
            gm = getattr(candidate, "grounding_metadata", None)
            if gm:
                web_queries = getattr(gm, "web_search_queries", None) or []
                fanout_queries = list(web_queries)

                chunks = getattr(gm, "grounding_chunks", None) or []
                for chunk in chunks:
                    web = getattr(chunk, "web", None)
                    if web:
                        url = getattr(web, "uri", None) or ""
                        title = getattr(web, "title", None) or ""
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            sources.append(FanoutSource(url=url, title=title))

                if getattr(gm, "search_entry_point", None) or fanout_queries:
                    search_call_count = max(1, len(fanout_queries))
    except Exception as exc:
        logger.warning("Gemini grounding metadata extraction failed: %s", exc)

    result = FanoutResult(
        prompt=prompt,
        provider="gemini",
        model=model,
        fanout_queries=fanout_queries,
        sources=sources,
        search_call_count=search_call_count,
        raw_output=[],
    )
    return result.finalize()


# ============================================================================
# MULTI-ENGINE
# ============================================================================

@dataclass
class MultiEngineResult:
    prompt: str
    engines: dict                        # provider -> FanoutResult
    combined_sources: List[FanoutSource] # deduped by URL across all engines
    combined_queries: List[str]          # deduped across all engines
    source_overlap: dict                 # {"all_engines": [...], "unique_per_engine": {...}}
    engine_agreement_score: float        # 0-100: % sources shared by all engines
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


async def analyze_multi_engine(
    prompt: str,
    providers: List[str],
    models: Optional[dict] = None,
    user_location: Optional[str] = None,
) -> MultiEngineResult:
    """
    Run fan-out analysis on multiple AI engines in parallel.

    Args:
        prompt: The user-facing question to analyze.
        providers: List of provider names, e.g. ["openai", "gemini"].
        models: Optional dict of provider -> model override.
        user_location: Optional location context.

    Returns:
        MultiEngineResult with per-engine results and cross-engine overlap.
    """
    models = models or {}

    async def _run_one(provider: str) -> tuple[str, Optional[FanoutResult]]:
        model = models.get(provider)
        try:
            result = await analyze_prompt(prompt, provider=provider, model=model, user_location=user_location)
            return provider, result
        except Exception as exc:
            logger.warning("Engine %s failed: %s", provider, exc)
            return provider, None

    pairs = await asyncio.gather(*[_run_one(p) for p in providers])
    engines = {p: r for p, r in pairs if r is not None}

    # Combine sources (dedup by URL)
    seen_urls: set = set()
    combined_sources: List[FanoutSource] = []
    url_to_engines: dict = {}  # url -> set of providers
    for provider, result in engines.items():
        for src in result.sources:
            url_to_engines.setdefault(src.url, set()).add(provider)
            if src.url not in seen_urls:
                seen_urls.add(src.url)
                combined_sources.append(src)

    # Combine queries (dedup)
    seen_q: set = set()
    combined_queries: List[str] = []
    for result in engines.values():
        for q in result.fanout_queries:
            if q.lower() not in seen_q:
                seen_q.add(q.lower())
                combined_queries.append(q)

    # Source overlap
    active_engines = set(engines.keys())
    shared_urls = [url for url, eng_set in url_to_engines.items() if eng_set == active_engines]
    unique_per_engine = {
        p: [url for url, eng_set in url_to_engines.items() if eng_set == {p}]
        for p in active_engines
    }

    # Agreement score: % of combined sources shared by ALL engines
    agreement = (len(shared_urls) / len(combined_sources) * 100) if combined_sources else 0.0

    return MultiEngineResult(
        prompt=prompt,
        engines=engines,
        combined_sources=combined_sources,
        combined_queries=combined_queries,
        source_overlap={"all_engines": shared_urls, "unique_per_engine": unique_per_engine},
        engine_agreement_score=round(agreement, 1),
    )


# ============================================================================
# PUBLIC API
# ============================================================================

# Default models per provider
PROVIDER_DEFAULTS = {
    "openai":      "gpt-4o",
    "anthropic":   "claude-sonnet-4-20250514",
    "gemini":      "gemini-2.5-flash",
    "perplexity":  "sonar-pro",
}

# Cheap models for bulk/batch use
PROVIDER_CHEAP = {
    "openai":      "gpt-4o-mini",
    "anthropic":   "claude-haiku-4-5-20251001",
    "gemini":      "gemini-2.5-flash",   # already fast/cheap
    "perplexity":  "sonar",
}

SUPPORTED_PROVIDERS = tuple(PROVIDER_DEFAULTS.keys())

# Cost per 1 K tokens (input / output) in USD — used for run_cost_usd calculation
COST_PER_1K_TOKENS: dict = {
    "gpt-4o":              {"input": 0.005,    "output": 0.015},
    "gpt-4o-mini":         {"input": 0.00015,  "output": 0.0006},
    "gpt-4.1":             {"input": 0.002,    "output": 0.008},
    "gpt-4.1-mini":        {"input": 0.0001,   "output": 0.0004},
    "claude-sonnet-4-20250514":  {"input": 0.003,  "output": 0.015},
    "claude-haiku-4-5-20251001": {"input": 0.00025, "output": 0.00125},
    "claude-opus-4-5-20251101":  {"input": 0.015,  "output": 0.075},
    "gemini-2.5-flash":    {"input": 0.000075, "output": 0.0003},
    "gemini-2.5-pro":      {"input": 0.00125,  "output": 0.005},
    "sonar-pro":           {"input": 0.003,    "output": 0.015},
    "sonar":               {"input": 0.001,    "output": 0.001},
}

# Approximate token usage per fan-out call (conservative estimates)
_EST_INPUT_TOKENS  = 800
_EST_OUTPUT_TOKENS = 600


def estimate_run_cost(model: str, input_tokens: int = _EST_INPUT_TOKENS, output_tokens: int = _EST_OUTPUT_TOKENS) -> float:
    """Return estimated cost in USD for one analyze_prompt call with *model*."""
    rates = COST_PER_1K_TOKENS.get(model)
    if not rates:
        # Fallback: use provider default cost
        for prefix, r in [("gpt-4o-mini", COST_PER_1K_TOKENS["gpt-4o-mini"]),
                           ("gpt-4o",      COST_PER_1K_TOKENS["gpt-4o"]),
                           ("claude-haiku", COST_PER_1K_TOKENS["claude-haiku-4-5-20251001"]),
                           ("claude-sonnet", COST_PER_1K_TOKENS["claude-sonnet-4-20250514"]),
                           ("gemini",       COST_PER_1K_TOKENS["gemini-2.5-flash"]),
                           ("sonar",        COST_PER_1K_TOKENS["sonar"])]:
            if model.startswith(prefix):
                rates = r
                break
        if not rates:
            rates = {"input": 0.001, "output": 0.003}
    return round(
        (input_tokens / 1000) * rates["input"] + (output_tokens / 1000) * rates["output"],
        6,
    )


async def analyze_prompt(
    prompt: str,
    provider: str = "openai",
    model: Optional[str] = None,
    user_location: Optional[str] = None,
) -> FanoutResult:
    """
    Analyze a single prompt to extract AI fan-out search queries.

    Args:
        prompt: The user-facing question to analyze.
        provider: "openai", "anthropic", "gemini", or "perplexity".
        model: Model ID. Defaults to PROVIDER_DEFAULTS[provider].
        user_location: Optional location context appended to the prompt.

    Returns:
        FanoutResult with fanout_queries, sources, and stats.
    """
    provider = provider.lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider!r}. Use one of: {', '.join(SUPPORTED_PROVIDERS)}")

    resolved_model = model or PROVIDER_DEFAULTS[provider]
    logger.info("Analyzing fan-out | provider=%s model=%s prompt=%.60r", provider, resolved_model, prompt)

    if provider == "openai":
        return await _analyze_openai(prompt, resolved_model, user_location)
    elif provider == "anthropic":
        return await _analyze_anthropic(prompt, resolved_model, user_location)
    elif provider == "gemini":
        return await _analyze_gemini(prompt, resolved_model, user_location)
    else:
        return await _analyze_perplexity(prompt, resolved_model, user_location)


async def analyze_batch(
    prompts: List[str],
    provider: str = "openai",
    model: Optional[str] = None,
    user_location: Optional[str] = None,
    delay_seconds: float = 2.0,
) -> List[FanoutResult]:
    """
    Analyze multiple prompts sequentially with a delay between each.

    Args:
        prompts: List of prompts to analyze.
        provider: "openai" or "anthropic".
        model: Model ID override.
        user_location: Optional location context.
        delay_seconds: Pause between requests (rate-limit buffer).

    Returns:
        List of FanoutResult, one per prompt.
    """
    results = []
    for i, prompt in enumerate(prompts):
        if i > 0:
            await asyncio.sleep(delay_seconds)
        try:
            result = await analyze_prompt(prompt, provider=provider, model=model, user_location=user_location)
            results.append(result)
        except Exception as exc:
            logger.error("Prompt %d failed: %s", i, exc)
            # Append an empty result so indices match
            results.append(FanoutResult(prompt=prompt, provider=provider, model=model or ""))
    return results


# ============================================================================
# CLI / QUICK TEST
# ============================================================================

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="WLA Fan-Out Analyzer — quick test")
    parser.add_argument("prompt", help='Prompt to analyze, e.g. "best seo agency romania"')
    parser.add_argument("--provider", default="openai", choices=list(SUPPORTED_PROVIDERS))
    parser.add_argument("--engines", default=None, help="Comma-separated engines for multi-engine mode, e.g. openai,gemini")
    parser.add_argument("--model", default=None, help="Override model ID (single-engine only)")
    parser.add_argument("--location", default=None, help="User location context")
    args = parser.parse_args()

    async def main():
        if args.engines:
            providers = [p.strip() for p in args.engines.split(",")]
            result = await analyze_multi_engine(args.prompt, providers=providers, user_location=args.location)
            print(f"\n=== Multi-Engine Fan-Out Analysis ===")
            print(f"Prompt   : {result.prompt}")
            print(f"Engines  : {', '.join(result.engines.keys())}")
            print(f"Agreement: {result.engine_agreement_score:.1f}%")
            print(f"Combined queries : {len(result.combined_queries)}")
            print(f"Combined sources : {len(result.combined_sources)}")
            print(f"Shared sources   : {len(result.source_overlap['all_engines'])}")
            for provider, r in result.engines.items():
                print(f"\n  [{provider}] queries={r.total_fanout_queries} sources={r.total_sources}")
        else:
            result = await analyze_prompt(
                args.prompt,
                provider=args.provider,
                model=args.model,
                user_location=args.location,
            )
            print(f"\n=== Fan-Out Analysis ===")
            print(f"Provider : {result.provider}")
            print(f"Model    : {result.model}")
            print(f"Prompt   : {result.prompt}")
            print(f"Queries  : {result.total_fanout_queries}")
            print(f"Sources  : {result.total_sources}")
            print(f"Searches : {result.search_call_count}")
            print(f"\nFan-out queries:")
            for i, q in enumerate(result.fanout_queries, 1):
                print(f"  Q{i:02d}: {q}")
            print(f"\nTop sources:")
            for s in result.sources[:10]:
                print(f"  [{s.domain}] {s.title or s.url}")

    asyncio.run(main())
