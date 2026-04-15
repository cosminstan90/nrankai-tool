"""
WLA Prompt Discovery Module

Discovers which prompts trigger AI engines to mention a target domain/brand.
Runs fan-out analysis across a candidate prompt set and reports mention rates,
competitor dominance, and strongest/weakest prompts.

Usage (standalone):
    python -m api.workers.prompt_discovery --domain example.com --brand "Example" \
        --category seo_agency --location "Bucharest, Romania" --engines openai,gemini
    python -m api.workers.prompt_discovery --domain example.com --brand "Example" \
        --category beauty_clinic --location "Miami, FL" --quick
    python -m api.workers.prompt_discovery --domain example.com --brand "Example" \
        --category seo_agency --smart --count 30
"""

import asyncio
import json as _json
import logging
import re as _re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

from api.workers.fanout_analyzer import (
    analyze_prompt, analyze_multi_engine,
    PROVIDER_DEFAULTS, SUPPORTED_PROVIDERS,
)

logger = logging.getLogger("prompt_discovery")

# Current year for template interpolation
_YEAR = datetime.now().year

# Cost estimates per provider (USD per prompt)
_COST_PER_PROMPT = {
    "openai":     0.005,
    "anthropic":  0.004,
    "gemini":     0.002,
    "perplexity": 0.008,
}


# ============================================================================
# PROMPT TEMPLATES — per business category
# ============================================================================

TEMPLATES: Dict[str, List[str]] = {
    "seo_agency": [
        "best seo agency in {city}",
        "top seo companies {country} {year}",
        "how much does seo cost {city}",
        "seo agency vs in-house seo",
        "best seo agency reviews {city}",
        "is {brand} a good seo agency",
        "alternatives to {brand} seo",
        "seo agency pricing {country}",
        "best seo tools {year}",
        "how to choose an seo agency",
        "top rated seo consultants {city}",
        "{brand} seo results and case studies",
        "seo agency for small business {city}",
        "affordable seo services {country}",
        "best digital marketing agency {city}",
    ],
    "beauty_clinic": [
        "best botox clinic in {city}",
        "laser hair removal cost {city}",
        "top rated med spa near me",
        "botox vs fillers which is better",
        "best aesthetic clinic reviews {city}",
        "how much does coolsculpting cost {city}",
        "is {brand} good for skin treatments",
        "med spa vs dermatologist",
        "best skin care clinic {city}",
        "anti-aging treatments {city}",
        "lip filler clinic {city} reviews",
        "non-surgical facelift {city}",
    ],
    "dental_clinic": [
        "best dentist in {city}",
        "dental implants cost {city}",
        "teeth whitening clinic {city}",
        "emergency dentist near me",
        "affordable dental care {city}",
        "invisalign dentist {city} reviews",
        "cosmetic dentist {city}",
        "dental crown cost {city}",
    ],
    "restaurant": [
        "best restaurants in {city} {year}",
        "romantic dinner {city}",
        "best {category} restaurant {city}",
        "fine dining {city}",
        "top rated restaurants {city} reviews",
        "where to eat in {city} {year}",
        "best brunch {city}",
        "michelin star restaurants {city}",
    ],
    "saas": [
        "best {category} software {year}",
        "{brand} vs {competitor}",
        "{brand} pricing and plans",
        "alternatives to {brand}",
        "{brand} reviews and complaints",
        "top {category} tools for small business",
        "{brand} vs competitors comparison",
        "is {brand} worth it {year}",
        "best {category} platform {year}",
        "{brand} free trial",
    ],
    "law_firm": [
        "best {service} lawyer {city}",
        "top law firms {city}",
        "how much does a {service} attorney cost",
        "{service} lawyer reviews {city}",
        "best personal injury attorney {city}",
        "law firm near me {city}",
        "experienced {service} lawyer {city}",
    ],
    "real_estate": [
        "best real estate agent {city}",
        "apartments for rent {city} {year}",
        "houses for sale {city}",
        "real estate agency reviews {city}",
        "top realtors {city}",
        "property management company {city}",
        "commercial real estate {city}",
    ],
    "generic": [
        "best {category} in {city}",
        "top {category} companies {country}",
        "{brand} reviews",
        "how much does {service} cost",
        "{brand} alternatives",
        "is {brand} worth it",
        "{category} near me",
        "{brand} vs {competitor}",
        "best {category} {year}",
        "top rated {category} {city}",
        "{brand} complaints",
        "cheapest {category} {city}",
    ],
}

# Cluster → template keys (for quick_discover priority selection)
_CLUSTER_PRIORITY = ["best_of", "pricing", "comparison", "branded", "generic"]

_CLUSTER_PATTERNS = {
    "branded":         ["reviews", "is {brand}", "alternatives to {brand}", "{brand} vs", "{brand} pricing", "{brand} complaints"],
    "best_of":         ["best ", "top ", "leading ", "highest rated", "#1"],
    "pricing":         ["cost", "price", "how much", "cheap", "affordable", "pricing"],
    "comparison":      ["versus", " vs ", "compare", "difference", "better than", "alternatives"],
    "local":           ["near me", "nearby", "local", "in {city}"],
    "problem_solution":["how to", "how do i", "fix", "solve", "guide"],
}


def classify_prompt_cluster(prompt: str) -> str:
    """Classify a prompt into a cluster without any API call."""
    p = prompt.lower()
    # Priority order
    for cluster in ["branded", "pricing", "comparison", "local", "best_of", "problem_solution"]:
        patterns = _CLUSTER_PATTERNS[cluster]
        if any(pat in p for pat in patterns):
            return cluster
    return "generic"


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class PromptMentionResult:
    prompt: str
    cluster: str
    mentioned: bool
    engines_with_mention: List[str]           # providers where target was found
    position_per_engine: Dict[str, int]       # provider -> source position (1-based, 0=not found)
    total_sources: int
    top_competitors: List[str]                # top domains found instead of target


@dataclass
class DiscoveryResult:
    target_domain: str
    target_brand: str
    prompts_tested: int
    prompts_with_mention: int
    mention_rate: float                       # 0.0–1.0
    mentioned_in: List[PromptMentionResult]
    not_mentioned_in: List[PromptMentionResult]
    strongest_prompts: List[str]              # top 5 by mention across engines
    weakest_prompts: List[str]                # prompts with 0 mentions
    competitor_dominance: Dict[str, dict]     # domain → {appearances, avg_position}
    total_cost_usd: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def summary(self) -> str:
        lines = [
            f"Domain       : {self.target_domain}",
            f"Brand        : {self.target_brand}",
            f"Prompts      : {self.prompts_tested}",
            f"Mentions     : {self.prompts_with_mention} ({self.mention_rate*100:.1f}%)",
            f"Cost         : ${self.total_cost_usd:.3f}",
            "",
            "Top competitors:",
        ]
        for domain, stats in sorted(
            self.competitor_dominance.items(),
            key=lambda x: x[1]["appearances"],
            reverse=True,
        )[:5]:
            lines.append(f"  {domain}: {stats['appearances']} appearances, avg pos {stats['avg_position']:.1f}")
        return "\n".join(lines)


# ============================================================================
# MAIN CLASS
# ============================================================================

class PromptDiscovery:
    """
    Discovers which prompts cause AI engines to mention a target domain/brand.

    Args:
        target_domain: Domain to look for in sources (e.g. "example.com").
        target_brand:  Brand name to look for in source titles/snippets.
        category:      Business category key from TEMPLATES.
        location:      Optional location string for template interpolation.
    """

    def __init__(
        self,
        target_domain: str,
        target_brand: str,
        category: str = "generic",
        location: Optional[str] = None,
    ):
        self.target_domain = target_domain.lower().lstrip("www.")
        self.target_brand = target_brand
        self.category = category if category in TEMPLATES else "generic"
        self.location = location or ""

        # Parse city and country from location string (best-effort)
        parts = [p.strip() for p in self.location.split(",")]
        self.city = parts[0] if parts else "your city"
        self.country = parts[-1] if len(parts) > 1 else "your country"

    # ------------------------------------------------------------------
    # Template helpers
    # ------------------------------------------------------------------

    def _fill_template(self, tpl: str) -> str:
        """Substitute template placeholders with instance values."""
        competitor = f"{self.category} company"
        service = self.category.replace("_", " ")
        cuisine = self.category.replace("_", " ")
        practice_area = service

        return (
            tpl
            .replace("{city}", self.city)
            .replace("{country}", self.country)
            .replace("{brand}", self.target_brand)
            .replace("{year}", str(_YEAR))
            .replace("{category}", service)
            .replace("{service}", service)
            .replace("{competitor}", competitor)
            .replace("{cuisine}", cuisine)
            .replace("{practice_area}", practice_area)
        )

    def generate_candidate_prompts(self, count: int = 50) -> List[str]:
        """
        Generate candidate prompts from templates for this category.

        Args:
            count: Max number of prompts to return.

        Returns:
            List of filled prompt strings, deduplicated.
        """
        templates = TEMPLATES.get(self.category, TEMPLATES["generic"])
        # Also add generic templates to pad if needed
        combined = list(templates)
        if len(combined) < count:
            for t in TEMPLATES["generic"]:
                if t not in combined:
                    combined.append(t)

        filled = []
        seen = set()
        for tpl in combined[:count]:
            prompt = self._fill_template(tpl)
            if prompt.lower() not in seen:
                seen.add(prompt.lower())
                filled.append(prompt)

        return filled[:count]

    # ------------------------------------------------------------------
    # Domain matching
    # ------------------------------------------------------------------

    def _domain_matches(self, url: str) -> bool:
        """Check if a source URL belongs to the target domain."""
        try:
            netloc = urlparse(url).netloc.lower().lstrip("www.")
            return netloc == self.target_domain or netloc.endswith("." + self.target_domain)
        except Exception:
            return False

    def _find_target_position(self, sources, provider: str) -> int:
        """Return 1-based position of target domain in sources, 0 if not found."""
        for i, src in enumerate(sources, 1):
            url = src.url if hasattr(src, "url") else src.get("url", "")
            if self._domain_matches(url):
                return i
        return 0

    # ------------------------------------------------------------------
    # Analysis helpers
    # ------------------------------------------------------------------

    def _build_mention_result(self, prompt: str, engine_results: dict) -> PromptMentionResult:
        """Build a PromptMentionResult from per-engine FanoutResult objects."""
        engines_with_mention: List[str] = []
        position_per_engine: Dict[str, int] = {}
        competitor_counts: Dict[str, int] = {}

        for provider, result in engine_results.items():
            pos = self._find_target_position(result.sources, provider)
            position_per_engine[provider] = pos
            if pos > 0:
                engines_with_mention.append(provider)

            for src in result.sources:
                url = src.url if hasattr(src, "url") else src.get("url", "")
                try:
                    domain = urlparse(url).netloc.lower().lstrip("www.")
                except Exception:
                    domain = ""
                if domain and domain != self.target_domain:
                    competitor_counts[domain] = competitor_counts.get(domain, 0) + 1

        top_competitors = sorted(competitor_counts, key=lambda d: -competitor_counts[d])[:3]
        total_sources = sum(r.total_sources for r in engine_results.values())

        return PromptMentionResult(
            prompt=prompt,
            cluster=classify_prompt_cluster(prompt),
            mentioned=bool(engines_with_mention),
            engines_with_mention=engines_with_mention,
            position_per_engine=position_per_engine,
            total_sources=total_sources,
            top_competitors=top_competitors,
        )

    def _build_discovery_result(
        self,
        prompt_results: List[PromptMentionResult],
        engines: List[str],
        total_cost: float,
    ) -> DiscoveryResult:
        mentioned = [r for r in prompt_results if r.mentioned]
        not_mentioned = [r for r in prompt_results if not r.mentioned]

        # Strongest: most engines mentioning + lowest avg position
        def _strength(r: PromptMentionResult) -> tuple:
            positions = [p for p in r.position_per_engine.values() if p > 0]
            avg_pos = sum(positions) / len(positions) if positions else 999
            return (-len(r.engines_with_mention), avg_pos)

        strongest = [r.prompt for r in sorted(mentioned, key=_strength)[:5]]
        weakest = [r.prompt for r in not_mentioned[:5]]

        # Competitor dominance across all results
        domain_appearances: Dict[str, List[int]] = {}
        for r in prompt_results:
            for comp in r.top_competitors:
                domain_appearances.setdefault(comp, []).append(1)

        competitor_dominance = {
            domain: {
                "appearances": sum(counts),
                "avg_position": 1.0,  # position not tracked at competitor level
            }
            for domain, counts in domain_appearances.items()
        }

        return DiscoveryResult(
            target_domain=self.target_domain,
            target_brand=self.target_brand,
            prompts_tested=len(prompt_results),
            prompts_with_mention=len(mentioned),
            mention_rate=len(mentioned) / len(prompt_results) if prompt_results else 0.0,
            mentioned_in=mentioned,
            not_mentioned_in=not_mentioned,
            strongest_prompts=strongest,
            weakest_prompts=weakest,
            competitor_dominance=competitor_dominance,
            total_cost_usd=total_cost,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def estimate_cost(self, max_prompts: int, engines: List[str]) -> float:
        """Estimate cost in USD for a discovery run."""
        return sum(_COST_PER_PROMPT.get(e, 0.005) for e in engines) * max_prompts

    async def discover(
        self,
        engines: Optional[List[str]] = None,
        max_prompts: int = 20,
    ) -> DiscoveryResult:
        """
        Run full discovery: test max_prompts prompts across all engines.

        Args:
            engines:     List of provider names. Defaults to ["openai"].
            max_prompts: How many prompts to test (capped at generated count).

        Returns:
            DiscoveryResult with mention rates, competitor data, etc.
        """
        engines = [e.lower() for e in (engines or ["openai"])]
        prompts = self.generate_candidate_prompts(max_prompts)

        cost = self.estimate_cost(len(prompts), engines)
        logger.info(
            "Discovery: %d prompts × %d engines ≈ $%.3f | domain=%s",
            len(prompts), len(engines), cost, self.target_domain,
        )

        prompt_results: List[PromptMentionResult] = []

        for i, prompt in enumerate(prompts):
            logger.info("Testing prompt %d/%d: %s", i + 1, len(prompts), prompt[:60])
            engine_results: dict = {}

            if len(engines) == 1:
                try:
                    result = await analyze_prompt(prompt, provider=engines[0])
                    engine_results[engines[0]] = result
                except Exception as exc:
                    logger.warning("Engine %s failed for prompt %r: %s", engines[0], prompt, exc)
            else:
                multi = await analyze_multi_engine(prompt, providers=engines)
                engine_results = multi.engines

            if engine_results:
                prompt_results.append(self._build_mention_result(prompt, engine_results))

            # Rate-limit buffer between prompts
            if i < len(prompts) - 1:
                await asyncio.sleep(1.5)

        return self._build_discovery_result(prompt_results, engines, cost)

    async def quick_discover(
        self,
        engines: Optional[List[str]] = None,
        count: int = 5,
    ) -> DiscoveryResult:
        """
        Fast discovery using only top-priority prompts (best_of + pricing + branded).
        Ideal for prospect scoring or quick checks.

        Args:
            engines: List of provider names. Defaults to ["openai"].
            count:   Number of prompts to test.
        """
        engines = engines or ["openai"]
        all_prompts = self.generate_candidate_prompts(50)

        # Prioritise: branded > pricing > best_of > rest
        priority_order = ["branded", "pricing", "best_of", "comparison", "local", "generic", "problem_solution"]
        bucketed: Dict[str, List[str]] = {c: [] for c in priority_order}
        for p in all_prompts:
            bucketed[classify_prompt_cluster(p)].append(p)

        selected: List[str] = []
        for cluster in priority_order:
            for p in bucketed[cluster]:
                if p not in selected:
                    selected.append(p)
                if len(selected) >= count:
                    break
            if len(selected) >= count:
                break

        # Temporarily override prompts list for the run
        original_generate = self.generate_candidate_prompts
        self.generate_candidate_prompts = lambda n=count: selected[:n]
        result = await self.discover(engines=engines, max_prompts=count)
        self.generate_candidate_prompts = original_generate
        return result


# ============================================================================
# ENDPOINT HELPERS (for fanout routes)
# ============================================================================

def discovery_result_to_dict(result: DiscoveryResult) -> dict:
    """Serialize DiscoveryResult to a JSON-safe dict for API responses."""
    return {
        "target_domain":       result.target_domain,
        "target_brand":        result.target_brand,
        "prompts_tested":      result.prompts_tested,
        "prompts_with_mention": result.prompts_with_mention,
        "mention_rate":        round(result.mention_rate, 4),
        "strongest_prompts":   result.strongest_prompts,
        "weakest_prompts":     result.weakest_prompts,
        "competitor_dominance": result.competitor_dominance,
        "total_cost_usd":      round(result.total_cost_usd, 4),
        "timestamp":           result.timestamp.isoformat(),
        "mentioned_in": [
            {
                "prompt":               r.prompt,
                "cluster":              r.cluster,
                "engines_with_mention": r.engines_with_mention,
                "position_per_engine":  r.position_per_engine,
                "total_sources":        r.total_sources,
                "top_competitors":      r.top_competitors,
            }
            for r in result.mentioned_in
        ],
        "not_mentioned_in": [
            {
                "prompt":          r.prompt,
                "cluster":         r.cluster,
                "engines_tested":  list(r.position_per_engine.keys()),
                "top_competitors": r.top_competitors,
            }
            for r in result.not_mentioned_in
        ],
    }


# ============================================================================
# SMART PROMPT GENERATION (LLM-powered)
# ============================================================================

CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "beauty_clinic":  ["beauty", "salon", "spa", "botox", "aesthetic", "laser", "skin"],
    "dental_clinic":  ["dental", "dentist", "teeth", "orthodont", "implant"],
    "seo_agency":     ["seo", "marketing", "agency", "digital", "ppc", "ads"],
    "restaurant":     ["restaurant", "food", "menu", "dining", "cuisine", "cafe"],
    "law_firm":       ["law", "attorney", "legal", "lawyer", "litigation"],
    "real_estate":    ["real estate", "property", "realtor", "apartment", "house for sale"],
    "saas":           ["software", "saas", "platform", "subscription", "dashboard", "api"],
}

EXCLUDED_DOMAINS = {
    "wikipedia.org", "youtube.com", "reddit.com", "yelp.com", "google.com",
    "facebook.com", "linkedin.com", "twitter.com", "instagram.com",
    "amazon.com", "tripadvisor.com", "x.com",
}


async def generate_smart_prompts(
    target_domain: str,
    target_brand: str,
    category: str,
    location: str = "",
    count: int = 30,
) -> List[str]:
    """
    Generate LLM-powered prompts by scraping the homepage and calling Claude Haiku.

    Falls back to template-only prompts if scraping or Claude call fails.
    Merges with generate_candidate_prompts() output and deduplicates.
    """
    import anthropic as _anthropic

    # --- 1. Scrape homepage ---
    title = ""
    meta_description = ""
    h1 = ""
    extracted_services = ""
    usps = ""

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(f"https://{target_domain}")
            resp.raise_for_status()

        if _BS4_AVAILABLE:
            soup = BeautifulSoup(resp.text, "html.parser")

            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else ""

            meta_tag = soup.find("meta", attrs={"name": "description"})
            if meta_tag:
                meta_description = (meta_tag.get("content") or "")[:150]
            usps = meta_description

            h1_tag = soup.find("h1")
            h1 = h1_tag.get_text(strip=True) if h1_tag else ""

            h2_tags = soup.find_all("h2")[:5]
            h2_texts = [h.get_text(strip=True) for h in h2_tags]

            paragraphs = soup.find_all("p")[:3]
            para_text = " ".join(p.get_text(strip=True) for p in paragraphs)

            service_text = " ".join(h2_texts) + " " + para_text
            extracted_services = service_text[:200]
        else:
            logger.warning("BeautifulSoup not available — skipping homepage parse")

    except Exception as exc:
        logger.warning("Homepage scrape failed for %s: %s", target_domain, exc)

    # --- 2. Template-based fallback prompts ---
    discovery = PromptDiscovery(
        target_domain=target_domain,
        target_brand=target_brand,
        category=category,
        location=location,
    )
    template_prompts = discovery.generate_candidate_prompts(count)

    # --- 3. Claude API call ---
    llm_prompts: List[str] = []
    try:
        aclient = _anthropic.Anthropic()

        system = (
            f"Generate {count} realistic prompts real users would type into ChatGPT/Perplexity/Google AI "
            f"about this business.\n"
            f"Mix: informational + commercial + navigational intents.\n"
            f"Include: branded, non-branded, comparison, local, pricing, review, problem-solution.\n"
            f"Include negative prompts ('complaints', 'is X worth it').\n"
            f"Short (3-5 words) AND conversational (full sentences).\n"
            f"Return ONLY a JSON array of strings, no markdown, no explanation."
        )

        user = (
            f"Brand: {target_brand} | Domain: {target_domain} | Category: {category}\n"
            f"Location: {location} | Services: {extracted_services}\n"
            f"Title: {title} | USPs: {usps}\n"
            f"Generate {count} prompts."
        )

        message = aclient.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": user}],
            system=system,
        )

        raw = message.content[0].text.strip()

        # Strip markdown fences if present
        raw = _re.sub(r"^```(?:json)?\s*", "", raw)
        raw = _re.sub(r"\s*```$", "", raw)

        parsed = _json.loads(raw)
        if isinstance(parsed, list):
            llm_prompts = [str(p) for p in parsed if isinstance(p, str) and p.strip()]

    except Exception as exc:
        logger.warning("Claude smart prompt generation failed: %s", exc)

    # --- 4. Merge + deduplicate ---
    seen: set = set()
    merged: List[str] = []
    for p in llm_prompts + template_prompts:
        key = p.lower().strip()
        if key and key not in seen:
            seen.add(key)
            merged.append(p)

    return merged


async def auto_discover_category(url: str) -> str:
    """
    Scrape the given URL and detect the business category via keyword matching.

    Returns the best-matching category key from CATEGORY_KEYWORDS, or "generic"
    if scraping fails or no keywords match.
    """
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        if _BS4_AVAILABLE:
            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(separator=" ", strip=True).lower()
        else:
            text = resp.text.lower()

        scores: Dict[str, int] = {}
        for cat, keywords in CATEGORY_KEYWORDS.items():
            score = sum(text.count(kw) for kw in keywords)
            scores[cat] = score

        best_cat = max(scores, key=lambda c: scores[c])
        if scores[best_cat] == 0:
            return "generic"
        return best_cat

    except Exception as exc:
        logger.warning("auto_discover_category failed for %s: %s", url, exc)
        return "generic"


def extract_competitors_from_fanout(
    fanout_results: list,
    target_domain: str,
) -> List[str]:
    """
    Extract top competitor domains from a list of FanoutResult objects or dicts.

    Args:
        fanout_results: List of FanoutResult objects or dicts with a "sources" key.
        target_domain:  The target domain to exclude from competitors.

    Returns:
        Top 10 competitor domains by frequency, excluding generic/social domains
        and the target domain itself.
    """
    target = target_domain.lower().lstrip("www.")
    domain_counts: Dict[str, int] = {}

    for result in fanout_results:
        # Support both FanoutResult objects and plain dicts
        if hasattr(result, "sources"):
            sources = result.sources
        elif isinstance(result, dict):
            sources = result.get("sources", [])
        else:
            continue

        for src in sources:
            if hasattr(src, "url"):
                url = src.url
            elif isinstance(src, dict):
                url = src.get("url", "")
            else:
                url = str(src)

            try:
                domain = urlparse(url).netloc.lower().lstrip("www.")
            except Exception:
                continue

            if not domain:
                continue
            if domain == target:
                continue

            # Check against excluded domains (exact match or suffix)
            excluded = False
            for ex in EXCLUDED_DOMAINS:
                if domain == ex or domain.endswith("." + ex):
                    excluded = True
                    break
            if excluded:
                continue

            domain_counts[domain] = domain_counts.get(domain, 0) + 1

    sorted_domains = sorted(domain_counts, key=lambda d: -domain_counts[d])
    return sorted_domains[:10]


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="WLA Prompt Discovery — find which prompts mention your brand")
    parser.add_argument("--domain",   required=True, help="Target domain, e.g. example.com")
    parser.add_argument("--brand",    required=True, help='Brand name, e.g. "Example Agency"')
    parser.add_argument("--category", default="generic", choices=list(TEMPLATES.keys()), help="Business category")
    parser.add_argument("--location", default="", help='Location string, e.g. "Bucharest, Romania"')
    parser.add_argument("--engines",  default="openai", help="Comma-separated engines, e.g. openai,gemini")
    parser.add_argument("--max",      type=int, default=20, help="Max prompts to test")
    parser.add_argument("--quick",    action="store_true", help="Quick mode: test only top 5 priority prompts")
    parser.add_argument("--smart",    action="store_true", help="Use LLM-powered smart prompt generation")
    parser.add_argument("--count",    type=int, default=20, help="Number of prompts (for --smart)")
    args = parser.parse_args()

    engines = [e.strip() for e in args.engines.split(",")]

    discovery = PromptDiscovery(
        target_domain=args.domain,
        target_brand=args.brand,
        category=args.category,
        location=args.location,
    )

    cost = discovery.estimate_cost(5 if args.quick else args.max, engines)
    print(f"\n≈ ${cost:.3f} for {'5 (quick)' if args.quick else args.max} prompts × {engines}")
    confirm = input("Continue? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        exit(0)

    async def main():
        if args.smart:
            prompts = await generate_smart_prompts(
                args.domain, args.brand, args.category, args.location, count=args.count
            )
            print(f"Generated {len(prompts)} smart prompts")
            for p in prompts[:10]:
                print(f"  {p}")
            return

        if args.quick:
            result = await discovery.quick_discover(engines=engines, count=5)
        else:
            result = await discovery.discover(engines=engines, max_prompts=args.max)

        print(f"\n{'='*50}")
        print(result.summary())
        print(f"\nStrongest prompts:")
        for p in result.strongest_prompts:
            print(f"  ✓ {p}")
        print(f"\nWeakest prompts (no mention):")
        for p in result.weakest_prompts:
            print(f"  ✗ {p}")

    asyncio.run(main())
