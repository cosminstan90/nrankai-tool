#!/usr/bin/env python3
"""
Run AI visibility checks on dental prospects.

For each prospect with a city, runs 2 dental prompts through OpenAI web_search
and checks whether the practice appears in the AI-generated answer/sources.

Results stored in prospect.ai_queries_run:
[
  {
    "query": "best dentist in Milwaukee",
    "appears": false,
    "cited_instead": ["Lake Shore Dental", "Dr. Smith Family Dentistry"],
    "provider": "openai"
  },
  ...
]

Usage (from geo_tool root):
    python scripts/run_ai_visibility_check.py
    python scripts/run_ai_visibility_check.py --segment dental --max 10
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from dotenv import load_dotenv

load_dotenv()

CLOUD_URL = os.environ.get("NRANKAI_CLOUD_URL", "https://api.nrankai.com")
API_KEY   = os.environ.get("NRANKAI_N8N_KEY", os.environ.get("NRANKAI_WORKER_KEY", ""))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Dental prompts to test — pick 2 most relevant per prospect
DENTAL_PROMPTS = [
    "best dentist in {city}",
    "affordable dental care {city}",
    "family dentist {city}",
]


def _normalize(text: str) -> str:
    """Lowercase + remove punctuation for fuzzy matching."""
    return re.sub(r"[^a-z0-9\s]", "", text.lower())


def _business_mentioned(business_name: str, text: str) -> bool:
    """Check if business name (or significant words from it) appear in text."""
    norm_biz = _normalize(business_name)
    norm_text = _normalize(text)

    # Full name match
    if norm_biz in norm_text:
        return True

    # Match on significant words (≥5 chars, skip generic words)
    SKIP = {"dental", "dentist", "dentistry", "family", "clinic", "care",
            "group", "center", "centre", "associates", "practice", "office"}
    words = [w for w in norm_biz.split() if len(w) >= 5 and w not in SKIP]
    if words and all(w in norm_text for w in words):
        return True

    return False


def _extract_cited_names(sources, business_name: str) -> list[str]:
    """Extract competitor names from cited sources (titles), excluding own practice."""
    cited = []
    norm_biz = _normalize(business_name)
    for src in sources[:6]:
        title = src.title or src.url or ""
        if not title:
            continue
        # Skip if it's the prospect's own site
        if _business_mentioned(business_name, title):
            continue
        # Skip generic titles
        skip_patterns = ["yelp", "google", "healthgrades", "zocdoc", "webmd",
                          "wikipedia", "reddit", "youtube", "facebook", "instagram"]
        if any(p in title.lower() for p in skip_patterns):
            continue
        # Take first part of title (before " - " or " | ")
        clean = re.split(r"\s[-|]\s", title)[0].strip()
        if clean and len(clean) > 3 and clean not in cited:
            cited.append(clean)
        if len(cited) >= 3:
            break
    return cited


async def _get_openai_response_text(prompt: str, city: str) -> tuple[str, list]:
    """Get full response text + sources from OpenAI web_search."""
    from openai import AsyncOpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set")

    client = AsyncOpenAI(api_key=api_key)
    full_prompt = f"{prompt} (I'm looking in {city}, USA)"

    response = await client.responses.create(
        model="gpt-4o-mini",
        input=full_prompt,
        tools=[{"type": "web_search"}],
    )

    # Extract response text
    response_text = ""
    sources = []
    seen_urls = set()

    for item in response.output:
        item_type = getattr(item, "type", None) or item.get("type", "")
        if item_type == "message":
            content_blocks = getattr(item, "content", []) or item.get("content", [])
            for block in content_blocks:
                block_type = getattr(block, "type", None) or block.get("type", "")
                if block_type == "output_text":
                    response_text += getattr(block, "text", "") or block.get("text", "")
                annotations = getattr(block, "annotations", []) or block.get("annotations", [])
                for ann in annotations:
                    ann_type = getattr(ann, "type", None) or ann.get("type", "")
                    if ann_type == "url_citation":
                        url   = getattr(ann, "url",   None) or ann.get("url",   "")
                        title = getattr(ann, "title", None) or ann.get("title", "") or ""
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            from api.workers.fanout_analyzer import FanoutSource
                            sources.append(FanoutSource(url=url, title=title))

    return response_text, sources


async def check_prospect(prospect: dict) -> list[dict]:
    """Run 2 AI visibility checks for a prospect. Returns list of results."""
    business_name = prospect["business_name"]
    city          = prospect.get("city") or ""

    if not city:
        logger.warning("Prospect %d has no city — skipping AI check", prospect["id"])
        return []

    results = []
    prompts_to_run = [p.format(city=city) for p in DENTAL_PROMPTS[:2]]

    for query in prompts_to_run:
        logger.info("Checking: '%s' for '%s'", query, business_name)
        try:
            response_text, sources = await _get_openai_response_text(query, city)

            # Check if business appears in response text OR source titles
            full_text = response_text + " " + " ".join(
                (s.title or s.url) for s in sources
            )
            appears = _business_mentioned(business_name, full_text)
            cited   = _extract_cited_names(sources, business_name)

            result = {
                "query":         query,
                "appears":       appears,
                "cited_instead": cited,
                "provider":      "openai",
            }
            results.append(result)
            logger.info(
                "  → %s | cited: %s",
                "✅ APPEARS" if appears else "❌ NOT FOUND",
                cited[:2],
            )
        except Exception as e:
            logger.error("AI check failed for '%s': %s", query, e)
            results.append({"query": query, "appears": None, "error": str(e)[:200]})

        await asyncio.sleep(2)  # avoid rate limit

    return results


async def fetch_prospects(segment: str, limit: int) -> list[dict]:
    """Fetch prospects that haven't been AI-checked yet."""
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{CLOUD_URL}/prospects",
            params={"segment": segment, "limit": limit, "offset": 0},
            headers=headers,
        )
    r.raise_for_status()
    data = r.json()
    prospects = data if isinstance(data, list) else data.get("prospects", data.get("items", []))
    # Filter: has city, no ai_queries_run yet
    return [
        p for p in prospects
        if p.get("city") or p.get("location_city")
        if not p.get("ai_queries_run")
    ]


async def save_ai_results(prospect_id: int, results: list[dict]) -> None:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.patch(
            f"{CLOUD_URL}/prospects/{prospect_id}/audit-data",
            json={"ai_queries_run": results},
            headers=headers,
        )
    if r.status_code != 200:
        logger.error("Failed to save AI results for %d: %s", prospect_id, r.text)


async def main(segment: str, max_prospects: int) -> None:
    if not API_KEY:
        logger.error("NRANKAI_N8N_KEY not set — aborting")
        sys.exit(1)
    if not os.getenv("OPENAI_API_KEY"):
        logger.error("OPENAI_API_KEY not set — aborting")
        sys.exit(1)

    logger.info("Fetching up to %d prospects (segment=%s)...", max_prospects, segment)
    prospects = await fetch_prospects(segment, max_prospects)
    logger.info("%d prospects to check", len(prospects))

    for i, p in enumerate(prospects, 1):
        # Normalize city field (API returns location_city)
        p["city"] = p.get("city") or p.get("location_city") or ""

        results = await check_prospect(p)
        if results:
            await save_ai_results(p["id"], results)

        logger.info("Progress: %d / %d", i, len(prospects))
        await asyncio.sleep(1)

    logger.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--segment", default="dental")
    parser.add_argument("--max",     type=int, default=100)
    args = parser.parse_args()
    asyncio.run(main(args.segment, args.max))
