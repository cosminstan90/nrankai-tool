"""
Bot Access Auditor (Prompt 33)
================================
Checks if AI crawlers are allowed or blocked in robots.txt and meta robots.
Free — uses only httpx, no paid APIs.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("bot_access_auditor")

BOT_CRAWLERS: Dict[str, dict] = {
    "GPTBot":               {"label": "OpenAI ChatGPT",           "weight": 30},
    "PerplexityBot":        {"label": "Perplexity AI",            "weight": 25},
    "ClaudeBot":            {"label": "Anthropic Claude",          "weight": 20},
    "Claude-Web":           {"label": "Anthropic Claude (web)",    "weight": 10},
    "Googlebot-Extended":   {"label": "Google SGE",               "weight": 15},
    "Bingbot":              {"label": "Microsoft Copilot",         "weight": 10},
    "meta-externalagent":   {"label": "Meta AI",                  "weight": 5},
    "Applebot-Extended":    {"label": "Apple AI",                 "weight": 5},
    "YouBot":               {"label": "You.com AI",               "weight": 3},
    "cohere-ai":            {"label": "Cohere AI",                "weight": 2},
}

FIX_SNIPPETS: Dict[str, str] = {
    "GPTBot":              "User-agent: GPTBot\nAllow: /",
    "PerplexityBot":       "User-agent: PerplexityBot\nAllow: /",
    "ClaudeBot":           "User-agent: ClaudeBot\nAllow: /",
    "Claude-Web":          "User-agent: Claude-Web\nAllow: /",
    "Googlebot-Extended":  "User-agent: Googlebot-Extended\nAllow: /",
    "Bingbot":             "User-agent: Bingbot\nAllow: /",
    "meta-externalagent":  "User-agent: meta-externalagent\nAllow: /",
    "Applebot-Extended":   "User-agent: Applebot-Extended\nAllow: /",
    "YouBot":              "User-agent: YouBot\nAllow: /",
    "cohere-ai":           "User-agent: cohere-ai\nAllow: /",
}


def _parse_robots(content: str) -> Dict[str, List[str]]:
    """Parse robots.txt into {user_agent: [disallow_paths]}."""
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


def _is_blocked(bot_name: str, rules: Dict[str, List[str]]) -> bool:
    """Return True if the bot has a Disallow: / or Disallow: /* in its rules."""
    # Check exact match first, then wildcard *
    for agent_key in [bot_name, "*"]:
        disallowed = rules.get(agent_key, [])
        for path in disallowed:
            if path in ("/", "/*", ""):
                return True
    return False


async def audit(target_domain: str) -> dict:
    """
    Fetch robots.txt + homepage meta, audit AI crawler access.
    Returns a dict matching BotAccessReport structure.
    """
    import httpx

    # Normalise domain
    domain = target_domain.strip().rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"

    robots_url   = f"{domain}/robots.txt"
    robots_raw   = ""
    robots_accessible = False
    rules: Dict[str, List[str]] = {}

    async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                  headers={"User-agent": "Mozilla/5.0 (compatible; nrankai-bot/1.0)"}) as client:
        # Fetch robots.txt
        try:
            r = await client.get(robots_url)
            if r.status_code == 200:
                robots_raw = r.text
                robots_accessible = True
                rules = _parse_robots(robots_raw)
        except Exception as exc:
            logger.warning("Could not fetch robots.txt for %s: %s", domain, exc)

        # Fetch homepage meta robots + X-Robots-Tag header
        meta_robots_homepage = ""
        x_robots_header = ""
        try:
            hp = await client.get(domain)
            x_robots_header = hp.headers.get("x-robots-tag", "")
            # Extract <meta name="robots" content="...">
            m = re.search(r'<meta[^>]+name=["\']robots["\'][^>]+content=["\']([^"\']+)["\']', hp.text, re.I)
            if m:
                meta_robots_homepage = m.group(1)
        except Exception:
            pass

    # Evaluate each bot
    crawlers: Dict[str, dict] = {}
    blocked_crawlers: List[str] = []
    allowed_crawlers: List[str] = []

    for bot, info in BOT_CRAWLERS.items():
        if not robots_accessible:
            status = "unknown"
        elif _is_blocked(bot, rules):
            status = "blocked"
        else:
            status = "allowed"

        crawlers[bot] = {
            "label":         info["label"],
            "weight":        info["weight"],
            "status":        status,
            "blocked_paths": rules.get(bot, []),
            "fix_snippet":   FIX_SNIPPETS.get(bot, ""),
        }
        if status == "blocked":
            blocked_crawlers.append(bot)
        elif status == "allowed":
            allowed_crawlers.append(bot)

    # Access score
    total_weight = sum(info["weight"] for info in BOT_CRAWLERS.values())
    allowed_weight = sum(
        BOT_CRAWLERS[bot]["weight"]
        for bot in allowed_crawlers
        if bot in BOT_CRAWLERS
    )
    access_score = round(allowed_weight / max(total_weight, 1) * 100, 1) if robots_accessible else 50.0

    # Overall status
    blocked_ratio = len(blocked_crawlers) / max(len(BOT_CRAWLERS), 1)
    if blocked_ratio >= 0.8:
        overall_status = "fully_blocked"
    elif blocked_ratio >= 0.5:
        overall_status = "mostly_blocked"
    elif blocked_ratio > 0:
        overall_status = "partially_blocked"
    else:
        overall_status = "open"

    # Recommendations
    recommendations = []
    for bot in blocked_crawlers:
        label = BOT_CRAWLERS[bot]["label"]
        snippet = FIX_SNIPPETS.get(bot, "")
        recommendations.append({
            "bot":       bot,
            "label":     label,
            "message":   f"Allow {label} in robots.txt",
            "fix":       snippet,
        })
    if "*" in rules and rules["*"] and not blocked_crawlers:
        recommendations.append({
            "bot":     "*",
            "label":   "All bots (wildcard)",
            "message": "Wildcard Disallow detected — explicitly whitelist LLM crawlers to be safe",
            "fix":     "\n".join(FIX_SNIPPETS.values()),
        })

    return {
        "target_domain":          target_domain,
        "robots_accessible":      robots_accessible,
        "crawlers":               crawlers,
        "meta_robots_homepage":   meta_robots_homepage,
        "x_robots_header":        x_robots_header,
        "overall_status":         overall_status,
        "access_score":           access_score,
        "blocked_crawlers":       blocked_crawlers,
        "allowed_crawlers":       allowed_crawlers,
        "recommendations":        recommendations,
        "robots_txt_raw":         robots_raw,
    }
