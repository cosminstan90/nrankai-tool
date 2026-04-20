"""
VelocityCMS Content Draft Creator (Prompt 28)
Uses Claude Haiku to generate a content brief, then POSTs the draft to the
VelocityCMS REST API.

Env vars required:
  VELOCITYCMS_API_URL  — base URL of the VelocityCMS instance (e.g. https://cms.example.com)
  VELOCITYCMS_API_KEY  — bearer token for the VelocityCMS API
  ANTHROPIC_API_KEY    — used to call claude-haiku-4-5-20251001
"""

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import anthropic
import httpx

logger = logging.getLogger("velocitycms_bridge")

# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class CMSDraftResult:
    success: bool
    draft_id: Optional[str]
    draft_url: Optional[str]
    title: Optional[str]
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "draft_id": self.draft_id,
            "draft_url": self.draft_url,
            "title": self.title,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Content brief generator
# ---------------------------------------------------------------------------

async def _generate_content_brief(gap: dict, project: dict) -> dict:
    """
    Use claude-haiku-4-5-20251001 to generate a content brief for a content gap.

    gap keys   : query, suggested_content_type, priority, competitor_who_covers
    project keys: target_brand, target_domain, vertical, language

    Returns: {
        "h1": str,
        "meta_description": str,   # ≤155 chars
        "outline_html": str,
        "faq": [{"q": str, "a": str}, ...]
    }
    """
    query = gap.get("query", "")
    content_type = gap.get("suggested_content_type", "blog_post")
    priority = gap.get("priority", "medium")
    competitor = gap.get("competitor_who_covers", "")

    brand = project.get("target_brand", "")
    domain = project.get("target_domain", "")
    vertical = project.get("vertical", "generic")
    language = project.get("language", "en")

    user_prompt = (
        f"Create a GEO-optimised content brief for the following content gap.\n\n"
        f"Query: {query}\n"
        f"Content type: {content_type}\n"
        f"Priority: {priority}\n"
        f"Competitor currently covering this topic: {competitor or 'unknown'}\n"
        f"Brand: {brand}\n"
        f"Domain: {domain}\n"
        f"Vertical: {vertical}\n"
        f"Language: {language}\n\n"
        f"Return ONLY a JSON object with these exact keys:\n"
        f"  h1 (string): the page H1 heading\n"
        f"  meta_description (string): SEO meta description, max 155 characters\n"
        f"  outline_html (string): full HTML outline with H2/H3 structure and brief section notes\n"
        f"  faq (array): 3-5 FAQ items, each with keys 'q' and 'a'\n"
        f"No other text, no markdown fences — pure JSON."
    )

    defaults = {
        "h1": query,
        "meta_description": f"Learn about {query} from {brand}."[:155],
        "outline_html": f"<h2>Introduction</h2>\n<h2>Key Information</h2>\n<h2>Conclusion</h2>",
        "faq": [{"q": f"What is {query}?", "a": ""}],
    }

    try:
        client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            temperature=0.3,
            system="You are a GEO content strategist. Generate content brief as JSON only.",
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip accidental markdown code fences
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        brief = json.loads(raw)
        # Enforce meta_description length
        if len(brief.get("meta_description", "")) > 155:
            brief["meta_description"] = brief["meta_description"][:152] + "..."
        return brief
    except anthropic.APIError as exc:
        logger.error("Anthropic API error generating brief: %s", exc)
        return defaults
    except (json.JSONDecodeError, IndexError, KeyError) as exc:
        logger.error("Failed to parse brief JSON from Haiku: %s", exc)
        return defaults
    except Exception as exc:
        logger.error("Unexpected error generating brief: %s", exc)
        return defaults


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def create_draft_from_gap(
    gap: dict,
    project: dict,
    context: Optional[dict] = None,
) -> CMSDraftResult:
    """
    1. Generate a content brief with Claude Haiku.
    2. POST the draft to VelocityCMS REST API.

    gap keys   : query, suggested_content_type, priority, competitor_who_covers, cluster
    project keys: target_brand, target_domain, vertical, language
    context    : optional extra metadata passed through to the CMS payload

    Returns CMSDraftResult.
    """
    cms_url = os.getenv("VELOCITYCMS_API_URL", "").rstrip("/")
    cms_key = os.getenv("VELOCITYCMS_API_KEY", "")

    if not cms_url:
        logger.warning("VELOCITYCMS_API_URL not configured — skipping draft creation")
        return CMSDraftResult(
            success=False,
            draft_id=None,
            draft_url=None,
            title=None,
            error="VelocityCMS not configured",
        )

    # Step 1 — generate brief
    logger.info("Generating content brief for gap query: %r", gap.get("query"))
    brief = await _generate_content_brief(gap, project)

    # Step 2 — build CMS payload
    payload: dict = {
        "title": brief.get("h1") or gap.get("query", "Untitled"),
        "meta_description": brief.get("meta_description", ""),
        "content": brief.get("outline_html", ""),
        "status": "draft",
        "geo_source": {
            "fanout_query": gap.get("query", ""),
            "cluster": gap.get("cluster") or gap.get("prompt_cluster", ""),
            "priority": gap.get("priority", "medium"),
            "competitor_who_covers": gap.get("competitor_who_covers", ""),
        },
    }

    # Attach FAQ as structured metadata if the CMS supports it
    faq = brief.get("faq")
    if faq:
        payload["faq"] = faq

    if context:
        payload["context"] = context

    # Step 3 — POST to VelocityCMS
    endpoint = f"{cms_url}/api/posts"
    headers = {
        "Authorization": f"Bearer {cms_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            resp = await client.post(endpoint, headers=headers, content=json.dumps(payload))
            resp.raise_for_status()
            data = resp.json()

        draft_id = str(data.get("id") or data.get("draft_id") or "")
        draft_url = (
            data.get("url")
            or data.get("draft_url")
            or (f"{cms_url}/posts/{draft_id}" if draft_id else None)
        )
        title = data.get("title") or payload["title"]

        logger.info("VelocityCMS draft created: id=%s url=%s", draft_id, draft_url)
        return CMSDraftResult(
            success=True,
            draft_id=draft_id or None,
            draft_url=draft_url,
            title=title,
        )

    except httpx.HTTPStatusError as exc:
        error_body = exc.response.text[:300] if exc.response else ""
        msg = f"VelocityCMS HTTP {exc.response.status_code}: {error_body}"
        logger.error("VelocityCMS draft creation failed: %s", msg)
        return CMSDraftResult(
            success=False,
            draft_id=None,
            draft_url=None,
            title=payload.get("title"),
            error=msg,
        )
    except httpx.RequestError as exc:
        msg = f"VelocityCMS request error: {exc}"
        logger.error(msg)
        return CMSDraftResult(
            success=False,
            draft_id=None,
            draft_url=None,
            title=payload.get("title"),
            error=msg,
        )
    except Exception as exc:
        msg = f"Unexpected error creating VelocityCMS draft: {exc}"
        logger.error(msg)
        return CMSDraftResult(
            success=False,
            draft_id=None,
            draft_url=None,
            title=payload.get("title"),
            error=msg,
        )
