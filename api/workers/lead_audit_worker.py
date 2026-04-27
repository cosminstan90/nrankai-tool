"""
Lead Audit Worker — nrankai.com integration

Polls the nrankai.com cloud API for pending lead audit jobs,
runs a single-page GEO audit on each URL using the local geo_tool engine,
then posts the formatted result back to the cloud.

Required env vars:
  NRANKAI_CLOUD_URL    — base URL of the cloud API (default: https://nrankai.com)
  NRANKAI_WORKER_KEY  — bearer token for /api/lead-audits/next and /result endpoints
"""

import asyncio
import ipaddress
import json
import logging
import os
import socket
import sys
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _assert_safe_url(url: str) -> None:
    """Raise ValueError if the URL resolves to a private/internal address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsafe URL scheme: {parsed.scheme}")
    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("URL has no hostname")
    try:
        resolved = ipaddress.ip_address(socket.gethostbyname(hostname))
        for net in _BLOCKED_NETWORKS:
            if resolved in net:
                raise ValueError(f"URL resolves to restricted address: {resolved}")
    except socket.gaierror as e:
        raise ValueError(f"Cannot resolve hostname {hostname!r}: {e}")

logger = logging.getLogger(__name__)

CLOUD_BASE_URL = os.environ.get("NRANKAI_CLOUD_URL", "https://nrankai.com")
WORKER_API_KEY = os.environ.get("NRANKAI_WORKER_KEY", "")
POLL_INTERVAL = 30  # seconds between polls when idle



# ── Audit logic ────────────────────────────────────────────────────────────────

async def _fetch_page_text(url: str) -> str:
    """Fetch a URL and extract clean text content."""
    _assert_safe_url(url)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "GEO-Analyzer/2.1 (nrankai)"})
        response.raise_for_status()
        html = response.text

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    return soup.get_text(separator=" ", strip=True)[:30000]


async def _run_geo_audit(text: str, language: str) -> dict:
    """Run the GEO_AUDIT prompt against extracted page text."""
    import os as _os
    from core.prompt_loader import PromptLoader
    from core.direct_analyzer import AsyncLLMClient, clean_json_response
    from api.provider_registry import get_cheapest_available_model

    cheapest = get_cheapest_available_model()
    provider = cheapest.provider.upper()
    model = cheapest.id

    # Prompts live at <project_root>/prompts/, not core/prompts/
    _project_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    _loader = PromptLoader(prompts_dir=_os.path.join(_project_root, "prompts"))
    system_msg = _loader.load_prompt("GEO_AUDIT")
    if language == "ro":
        system_msg += (
            "\n\nLANGUAGE INSTRUCTION: Write all text values in Romanian. "
            "Keep JSON keys and enum values in English."
        )

    llm = AsyncLLMClient(provider=provider, model_name=model)
    raw, _, _ = await llm.complete(system_message=system_msg, user_content=text)
    await llm.close()

    return json.loads(clean_json_response(raw))


async def _synthesize_result(url: str, geo_data: dict, lead: dict, language: str) -> dict:
    """
    Use a second LLM call to convert raw geo_audit output into the
    WorkerResultSuccess format expected by the cloud API.
    """
    from core.direct_analyzer import AsyncLLMClient, clean_json_response
    from api.provider_registry import get_cheapest_available_model

    cheapest = get_cheapest_available_model()
    provider = cheapest.provider.upper()
    model = cheapest.id

    company = lead.get("company_name", "") or url
    is_ro = language == "ro"

    system_prompt = (
        "You are an expert at structuring SEO/GEO audit data into concise reports. "
        "Given raw GEO audit JSON, extract and format it into the exact output schema. "
        "Return ONLY valid JSON — no markdown, no explanation."
    )

    user_prompt = f"""Website: {url}
Company: {company}
Language: {language}

Raw GEO audit data:
{json.dumps(geo_data, indent=2)[:8000]}

Return this exact JSON structure (all strings in {"Romanian" if is_ro else "English"}):
{{
  "geo_score": <integer 0-100, from overall_score>,
  "average_page_score": <same as geo_score for single page>,
  "classification": <"poor"|"needs_improvement"|"good"|"excellent">,
  "pages_analyzed": 1,
  "industry_benchmark": 61,
  "gap": <geo_score minus 61>,
  "top_issues": [
    {{
      "rank": 1,
      "category": "<category>",
      "severity": "<critical|major|medium>",
      "title": "<short title>",
      "description": "<1-2 sentences>",
      "impact": "<business impact>"
    }}
  ],
  "quick_wins": ["<actionable win>"],
  "summary": {{
    "one_liner": "<one sentence on AI visibility>",
    "executive": "<2-3 sentences: current state, problems, opportunity>",
    "opportunity": "<biggest opportunity if fixed>"
  }},
  "email_ready": {{
    "subject": {{
      "en": "<email subject mentioning {company}>",
      "ro": "<Romanian subject>"
    }},
    "preheader": {{
      "en": "<preheader text>",
      "ro": "<Romanian preheader>"
    }}
  }}
}}"""

    llm = AsyncLLMClient(provider=provider, model_name=model)
    raw, _, _ = await llm.complete(system_message=system_prompt, user_content=user_prompt)
    await llm.close()

    return json.loads(clean_json_response(raw))


async def _process_job(job: dict) -> dict:
    """
    Process one lead audit job end-to-end.
    Returns a dict matching WorkerResultSuccess or WorkerResultFailure.
    """
    job_id = job["job_id"]
    url = job["website"]
    lead = job.get("lead", {})
    language = lead.get("language", "en")

    logger.info("Processing lead job %s — %s", job_id, url)
    try:
        text = await _fetch_page_text(url)
        geo_data = await _run_geo_audit(text, language)
        synthesized = await _synthesize_result(url, geo_data, lead, language)

        return {
            "status": "completed",
            "scores": {
                "geo_score": int(synthesized["geo_score"]),
                "average_page_score": int(synthesized["average_page_score"]),
                "classification": synthesized["classification"],
                "pages_analyzed": 1,
                "industry_benchmark": 61,
                "gap": int(synthesized["geo_score"]) - 61,
            },
            "top_issues": synthesized.get("top_issues", []),
            "quick_wins": synthesized.get("quick_wins", []),
            "summary": synthesized["summary"],
            "email_ready": synthesized["email_ready"],
        }

    except Exception as exc:
        logger.error("Lead job %s failed: %s", job_id, exc, exc_info=True)
        return {
            "status": "failed",
            "error": {
                "code": "audit_failed",
                "message": str(exc)[:500],
                "retryable": True,
            },
        }


async def _post_result(job_id: str, result: dict) -> None:
    """POST the result back to the cloud API."""
    url = f"{CLOUD_BASE_URL}/api/lead-audits/{job_id}/result"
    headers = {
        "Authorization": f"Bearer {WORKER_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=result, headers=headers)
            if r.status_code < 300:
                logger.info("Result posted for lead job %s (%s)", job_id, r.status_code)
            else:
                logger.error("Failed to post result for %s: %s %s", job_id, r.status_code, r.text[:200])
    except Exception as exc:
        logger.error("Error posting result for %s: %s", job_id, exc)


# ── Main loop ──────────────────────────────────────────────────────────────────

async def lead_audit_worker_loop():
    """
    Background loop: polls nrankai.com every POLL_INTERVAL seconds for pending
    lead audit jobs, processes them, and posts results back.

    Disabled automatically if NRANKAI_WORKER_KEY is not set.
    """
    if not WORKER_API_KEY:
        logger.info("NRANKAI_WORKER_KEY not set — lead audit worker disabled")
        return

    logger.info(
        "Lead audit worker started (cloud: %s, polling every %ds)",
        CLOUD_BASE_URL,
        POLL_INTERVAL,
    )

    headers = {"Authorization": f"Bearer {WORKER_API_KEY}"}

    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{CLOUD_BASE_URL}/api/lead-audits/next", headers=headers)

            if r.status_code == 204:
                pass  # No pending jobs — normal
            elif r.status_code == 200:
                job = r.json()
                result = await _process_job(job)
                await _post_result(job["job_id"], result)
            else:
                logger.warning("Unexpected response from /next: %s", r.status_code)

        except asyncio.CancelledError:
            logger.info("Lead audit worker cancelled")
            return
        except Exception as exc:
            logger.error("Worker poll error: %s", exc)

        await asyncio.sleep(POLL_INTERVAL)
