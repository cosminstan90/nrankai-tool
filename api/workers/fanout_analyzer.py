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
# PUBLIC API
# ============================================================================

# Default models per provider
PROVIDER_DEFAULTS = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-20250514",   # best balance quality/cost for fan-out
}

# Cheap models for bulk/batch use
PROVIDER_CHEAP = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
}


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
        provider: "openai" or "anthropic".
        model: Model ID. Defaults to PROVIDER_DEFAULTS[provider].
        user_location: Optional location context appended to the prompt.

    Returns:
        FanoutResult with fanout_queries, sources, and stats.
    """
    provider = provider.lower()
    if provider not in ("openai", "anthropic"):
        raise ValueError(f"Unsupported provider: {provider!r}. Use 'openai' or 'anthropic'.")

    resolved_model = model or PROVIDER_DEFAULTS[provider]

    logger.info("Analyzing fan-out | provider=%s model=%s prompt=%.60r", provider, resolved_model, prompt)

    if provider == "openai":
        return await _analyze_openai(prompt, resolved_model, user_location)
    else:
        return await _analyze_anthropic(prompt, resolved_model, user_location)


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
    parser.add_argument("--provider", default="openai", choices=["openai", "anthropic"])
    parser.add_argument("--model", default=None, help="Override model ID")
    parser.add_argument("--location", default=None, help="User location context")
    args = parser.parse_args()

    async def main():
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
