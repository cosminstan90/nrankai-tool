"""
Deterministic technical-SEO facts for the "Technical SEO Content Audit" prompt.

Etapa 6 of docs/CONSOLIDATION_PLAN.md ("fapte inainte de opinii"): the prompt
asks the LLM to guess three things the tool can verify directly instead --
AI crawler blocking (robots.txt), llms.txt presence, and JSON-LD structured
data. The first two are invisible to the LLM today because it only ever
sees the page's own HTML/text, never a separate file at the domain root.
The third is invisible for a different reason: core/html2llm_converter.py's
extract_content() decomposes every <script> tag (including JSON-LD) before
the LLM ever sees the page text, except for FAQPage content it reformats as
plain prose -- so "Schema.org presence: detectable?" is asked against text
that has had 100% of its markup stripped.

fetch_domain_facts() is meant to run ONCE per audit run (facts are the same
for every page on the domain); extract_structured_data_types() runs once per
page against the page's original (pre-conversion) HTML.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger("technical_facts")

# Same crawler set core/direct_analyzer.py's prompt already asks about --
# kept separate from api/utils/bot_access_auditor.py (which has its own
# scoring/recommendations concerns) so core/ stays free of api/ imports.
AI_CRAWLERS = ["GPTBot", "PerplexityBot", "ClaudeBot", "Google-Extended", "Applebot-Extended"]


def _parse_robots_disallow(content: str) -> Dict[str, List[str]]:
    rules: Dict[str, List[str]] = {}
    current_agents: List[str] = []
    for line in content.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            current_agents = []
            continue
        lower = line.lower()
        if lower.startswith("user-agent:"):
            agent = line.split(":", 1)[1].strip()
            current_agents.append(agent)
            rules.setdefault(agent, [])
        elif lower.startswith("disallow:") and current_agents:
            path = line.split(":", 1)[1].strip()
            for agent in current_agents:
                rules.setdefault(agent, []).append(path)
    return rules


def _is_fully_blocked(agent: str, rules: Dict[str, List[str]]) -> bool:
    for key in (agent, "*"):
        for path in rules.get(key, []):
            if path in ("/", "/*"):
                return True
    return False


async def fetch_domain_facts(domain: str) -> Optional[dict]:
    """
    One-time, per-audit-run fetch of domain-level facts: robots.txt AI-crawler
    blocking + llms.txt existence. Returns None on total failure (caller
    treats that as "unknown, flag for verification" -- matching the prompt's
    own stated philosophy of never guessing about things it can't confirm).
    """
    import httpx

    domain = domain.strip().rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"

    facts = {
        "robots_txt_accessible": False,
        "ai_crawlers_blocked": [],
        "ai_crawlers_allowed": [],
        "llms_txt_present": False,
    }

    try:
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; nrankai-bot/1.0)"},
        ) as client:
            try:
                r = await client.get(f"{domain}/robots.txt")
                if r.status_code == 200:
                    facts["robots_txt_accessible"] = True
                    rules = _parse_robots_disallow(r.text)
                    for agent in AI_CRAWLERS:
                        if _is_fully_blocked(agent, rules):
                            facts["ai_crawlers_blocked"].append(agent)
                        else:
                            facts["ai_crawlers_allowed"].append(agent)
            except Exception as exc:
                logger.warning("robots.txt fetch failed for %s: %s", domain, exc)

            try:
                r = await client.get(f"{domain}/llms.txt")
                facts["llms_txt_present"] = r.status_code == 200
            except Exception as exc:
                logger.warning("llms.txt check failed for %s: %s", domain, exc)
    except Exception as exc:
        logger.warning("fetch_domain_facts failed entirely for %s: %s", domain, exc)
        return None

    return facts


_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)


def extract_structured_data_types(html: str) -> List[str]:
    """
    Parse <script type="application/ld+json"> blocks in the page's ORIGINAL
    HTML (before html2llm_converter strips them) and return the deduplicated
    list of schema.org @type values actually present.
    """
    types: List[str] = []
    for match in _JSONLD_RE.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            nodes = graph if isinstance(graph, list) else [item]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                t = node.get("@type")
                if isinstance(t, list):
                    types.extend(str(x) for x in t)
                elif t:
                    types.append(str(t))

    seen = set()
    result = []
    for t in types:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def format_facts_block(domain_facts: Optional[dict], structured_data_types: Optional[List[str]]) -> str:
    """Render facts as a labeled block to prepend to the LLM's page content."""
    lines = ["=== AUTOMATED VERIFICATION FACTS (ground truth from live checks -- do NOT contradict these) ==="]

    if domain_facts is None:
        lines.append("robots.txt: could not be checked (network error) -- treat AI crawler access as unknown, flag for verification")
        lines.append("/llms.txt: could not be checked (network error) -- treat as unknown")
    else:
        if not domain_facts.get("robots_txt_accessible"):
            lines.append("robots.txt: not accessible/found -- treat AI crawler access as unknown, flag for verification")
        else:
            blocked = domain_facts.get("ai_crawlers_blocked") or []
            if blocked:
                lines.append(f"robots.txt: BLOCKS these AI crawlers: {', '.join(blocked)}")
            else:
                lines.append(
                    "robots.txt: does NOT block any known AI crawler "
                    "(GPTBot, PerplexityBot, ClaudeBot, Google-Extended, Applebot-Extended)"
                )
        lines.append(f"/llms.txt: {'present (200 OK)' if domain_facts.get('llms_txt_present') else 'not found'}")

    if structured_data_types:
        lines.append(f"JSON-LD structured data DETECTED on this page: {', '.join(structured_data_types)}")
    else:
        lines.append(
            "JSON-LD structured data: NONE detected on this page (note: the page text below "
            "has had all <script> tags removed during conversion -- this fact is your only "
            "signal for structured data presence, do not try to infer schema presence from "
            "the text itself)"
        )

    lines.append("=== END AUTOMATED FACTS -- page content follows below ===")
    return "\n".join(lines)
