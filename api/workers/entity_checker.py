"""
Entity Authority Checker (Prompt 31)
Checks Wikipedia, Wikidata, Schema markup, Crunchbase (via Serper),
Google Knowledge Panel (via Serper), LinkedIn (via Serper).
All checks run in parallel with asyncio.gather + per-check 10s timeout.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger("entity_checker")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EntityCheckResult:
    source: str
    found: bool
    url: Optional[str] = None
    description: Optional[str] = None
    quality_score: Optional[str] = None  # "good"|"basic"|"missing" for schema; "en"/"ro" for wikipedia
    extra: dict = field(default_factory=dict)  # wikidata_id, schema_types, panel_type, etc.


@dataclass
class EntityReport:
    target_domain: str
    target_brand: str
    checks: Dict[str, EntityCheckResult]
    entity_authority_score: float
    recommendations: List[Dict]
    analyzed_at: str  # ISO datetime

    def to_dict(self) -> dict:
        return {
            "target_domain": self.target_domain,
            "target_brand": self.target_brand,
            "checks": {
                k: {
                    "source": v.source,
                    "found": v.found,
                    "url": v.url,
                    "description": v.description,
                    "quality_score": v.quality_score,
                    "extra": v.extra,
                }
                for k, v in self.checks.items()
            },
            "entity_authority_score": self.entity_authority_score,
            "recommendations": self.recommendations,
            "analyzed_at": self.analyzed_at,
        }


# ---------------------------------------------------------------------------
# Score weights
# ---------------------------------------------------------------------------

SCORE_WEIGHTS = {
    "wikipedia_en": 25,
    "wikipedia_ro": 10,
    "wikidata": 20,
    "knowledge_panel": 20,
    "schema_good": 15,
    "schema_basic": 7,
    "crunchbase": 5,
    "linkedin": 5,
}


# ---------------------------------------------------------------------------
# Individual checkers
# ---------------------------------------------------------------------------

async def _check_wikipedia(brand: str, session: httpx.AsyncClient) -> EntityCheckResult:
    """Try EN Wikipedia, then RO Wikipedia."""
    for lang, variant_label in [("en", "en"), ("ro", "ro")]:
        url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(brand)}"
        try:
            resp = await session.get(url, follow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                description = data.get("extract", "")[:300] or None
                page_url = data.get("content_urls", {}).get("desktop", {}).get("page")
                return EntityCheckResult(
                    source=f"wikipedia_{variant_label}",
                    found=True,
                    url=page_url,
                    description=description,
                    quality_score=variant_label,
                )
        except Exception as exc:
            logger.debug("Wikipedia %s check error for %r: %s", lang, brand, exc)

    return EntityCheckResult(source="wikipedia_en", found=False)


async def _check_wikidata(domain: str, session: httpx.AsyncClient) -> EntityCheckResult:
    """SPARQL query to find Wikidata entity by official website (P856)."""
    # Normalise domain to full https URL
    base_url = domain if domain.startswith("http") else f"https://{domain}"
    sparql = f"""
SELECT ?item WHERE {{
  ?item wdt:P856 <{base_url}> .
}} LIMIT 1
"""
    try:
        resp = await session.get(
            "https://query.wikidata.org/sparql",
            params={"query": sparql, "format": "json"},
            headers={
                "Accept": "application/json",
                "User-Agent": "nrankai-bot/1.0 (https://nrankai.com)",
            },
        )
        if resp.status_code == 200:
            results = resp.json().get("results", {}).get("bindings", [])
            if results:
                item_uri = results[0]["item"]["value"]
                wikidata_id = item_uri.rsplit("/", 1)[-1]
                return EntityCheckResult(
                    source="wikidata",
                    found=True,
                    url=item_uri,
                    extra={"wikidata_id": wikidata_id},
                )
    except Exception as exc:
        logger.debug("Wikidata check error for %r: %s", domain, exc)
        return EntityCheckResult(source="wikidata", found=False, extra={"error": str(exc)})

    return EntityCheckResult(source="wikidata", found=False)


async def _check_schema_markup(domain: str, session: httpx.AsyncClient) -> EntityCheckResult:
    """Fetch homepage and extract JSON-LD schema markup."""
    base = domain if domain.startswith("http") else f"https://{domain}"
    for url_attempt in [base, f"https://www.{domain}" if not domain.startswith("www.") else None]:
        if url_attempt is None:
            continue
        try:
            resp = await session.get(url_attempt, follow_redirects=True)
            if resp.status_code == 200:
                html = resp.text
                # Extract all JSON-LD blocks
                ld_blocks = re.findall(
                    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    html,
                    re.DOTALL | re.IGNORECASE,
                )
                schema_types: list[str] = []
                has_same_as = False
                for block in ld_blocks:
                    try:
                        obj = json.loads(block.strip())
                        # Handle both single objects and arrays
                        items = obj if isinstance(obj, list) else [obj]
                        for item in items:
                            t = item.get("@type", "")
                            if isinstance(t, list):
                                schema_types.extend(t)
                            elif t:
                                schema_types.append(t)
                            if item.get("sameAs"):
                                has_same_as = True
                    except (json.JSONDecodeError, AttributeError):
                        continue

                org_types = {"Organization", "LocalBusiness", "Corporation", "NGO",
                             "GovernmentOrganization", "EducationalOrganization"}
                has_org = bool(org_types.intersection(set(schema_types)))

                if has_org and has_same_as:
                    quality = "good"
                elif has_org:
                    quality = "basic"
                else:
                    quality = "missing"

                return EntityCheckResult(
                    source="schema_markup",
                    found=has_org,
                    url=url_attempt,
                    quality_score=quality,
                    extra={"schema_types": schema_types, "has_same_as": has_same_as},
                )
        except Exception as exc:
            logger.debug("Schema markup check error for %r: %s", url_attempt, exc)

    return EntityCheckResult(
        source="schema_markup",
        found=False,
        quality_score="missing",
        extra={"schema_types": [], "has_same_as": False},
    )


async def _check_serper(query: str, api_key: str, session: httpx.AsyncClient) -> dict:
    """POST to Serper.dev search API and return raw response dict."""
    resp = await session.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        content=json.dumps({"q": query, "num": 5}),
    )
    resp.raise_for_status()
    return resp.json()


async def _check_crunchbase(
    brand: str, api_key: Optional[str], session: httpx.AsyncClient
) -> EntityCheckResult:
    """Search for brand presence on Crunchbase via Serper."""
    if not api_key:
        return EntityCheckResult(source="crunchbase", found=False, extra={"skipped": True})
    try:
        data = await _check_serper(f'site:crunchbase.com "{brand}"', api_key, session)
        organic = data.get("organic", [])
        if organic:
            first = organic[0]
            return EntityCheckResult(
                source="crunchbase",
                found=True,
                url=first.get("link"),
                description=first.get("snippet"),
            )
    except Exception as exc:
        logger.debug("Crunchbase check error for %r: %s", brand, exc)
        return EntityCheckResult(source="crunchbase", found=False, extra={"error": str(exc)})

    return EntityCheckResult(source="crunchbase", found=False)


async def _check_knowledge_panel(
    brand: str, api_key: Optional[str], session: httpx.AsyncClient
) -> EntityCheckResult:
    """Search for Google Knowledge Panel via Serper knowledgeGraph field."""
    if not api_key:
        return EntityCheckResult(source="knowledge_panel", found=False, extra={"skipped": True})
    try:
        data = await _check_serper(brand, api_key, session)
        kg = data.get("knowledgeGraph")
        if kg:
            return EntityCheckResult(
                source="knowledge_panel",
                found=True,
                url=kg.get("website") or kg.get("descriptionLink"),
                description=kg.get("description"),
                extra={
                    "panel_type": kg.get("type"),
                    "title": kg.get("title"),
                },
            )
    except Exception as exc:
        logger.debug("Knowledge panel check error for %r: %s", brand, exc)
        return EntityCheckResult(
            source="knowledge_panel", found=False, extra={"error": str(exc)}
        )

    return EntityCheckResult(source="knowledge_panel", found=False)


async def _check_linkedin(
    brand: str, api_key: Optional[str], session: httpx.AsyncClient
) -> EntityCheckResult:
    """Search for brand LinkedIn company page via Serper."""
    if not api_key:
        return EntityCheckResult(source="linkedin", found=False, extra={"skipped": True})
    try:
        data = await _check_serper(f'site:linkedin.com/company "{brand}"', api_key, session)
        organic = data.get("organic", [])
        if organic:
            first = organic[0]
            return EntityCheckResult(
                source="linkedin",
                found=True,
                url=first.get("link"),
                description=first.get("snippet"),
            )
    except Exception as exc:
        logger.debug("LinkedIn check error for %r: %s", brand, exc)
        return EntityCheckResult(source="linkedin", found=False, extra={"error": str(exc)})

    return EntityCheckResult(source="linkedin", found=False)


# ---------------------------------------------------------------------------
# Scoring and recommendations
# ---------------------------------------------------------------------------

def _compute_score(checks: Dict[str, EntityCheckResult]) -> float:
    """Sum weights for found/quality signals."""
    score = 0.0

    wiki = checks.get("wikipedia_en")
    if wiki and wiki.found:
        lang = wiki.quality_score  # "en" or "ro"
        if lang == "en":
            score += SCORE_WEIGHTS["wikipedia_en"]
        else:
            score += SCORE_WEIGHTS["wikipedia_ro"]

    if checks.get("wikidata", EntityCheckResult("", False)).found:
        score += SCORE_WEIGHTS["wikidata"]

    if checks.get("knowledge_panel", EntityCheckResult("", False)).found:
        score += SCORE_WEIGHTS["knowledge_panel"]

    schema = checks.get("schema_markup")
    if schema:
        if schema.quality_score == "good":
            score += SCORE_WEIGHTS["schema_good"]
        elif schema.quality_score == "basic":
            score += SCORE_WEIGHTS["schema_basic"]

    if checks.get("crunchbase", EntityCheckResult("", False)).found:
        score += SCORE_WEIGHTS["crunchbase"]

    if checks.get("linkedin", EntityCheckResult("", False)).found:
        score += SCORE_WEIGHTS["linkedin"]

    return min(round(score, 1), 100.0)


def _build_recommendations(checks: Dict[str, EntityCheckResult]) -> List[Dict]:
    """Generate ordered recommendations based on missing/weak signals."""
    recs: List[Dict] = []

    wiki = checks.get("wikipedia_en")
    if not wiki or not wiki.found:
        recs.append(
            {
                "priority": "high",
                "title": "Create a Wikipedia article",
                "action": (
                    "Create a Wikipedia article for your brand — this is the single highest-impact "
                    "action for AI entity recognition. Ensure the article meets Wikipedia notability "
                    "guidelines and cites reliable third-party sources."
                ),
            }
        )

    if not checks.get("wikidata", EntityCheckResult("", False)).found:
        recs.append(
            {
                "priority": "high",
                "title": "Add your brand to Wikidata",
                "action": (
                    "Create a Wikidata item for your brand and link it to your official website "
                    "via property P856. LLMs use Wikidata extensively to build entity graphs."
                ),
            }
        )

    schema = checks.get("schema_markup")
    if not schema or schema.quality_score == "missing":
        recs.append(
            {
                "priority": "high",
                "title": "Add Organization schema markup",
                "action": (
                    "Add a JSON-LD Organization (or LocalBusiness) schema block to your homepage. "
                    "Include name, url, logo, contactPoint and sameAs properties."
                ),
            }
        )
    elif schema.quality_score == "basic":
        recs.append(
            {
                "priority": "medium",
                "title": "Add sameAs links to your Organization schema",
                "action": (
                    "Your Organization schema exists but lacks sameAs. Add sameAs links pointing "
                    "to your Wikipedia page, Wikidata entity, Crunchbase profile and LinkedIn "
                    "company page to strengthen entity disambiguation."
                ),
            }
        )

    if not checks.get("knowledge_panel", EntityCheckResult("", False)).found:
        recs.append(
            {
                "priority": "medium",
                "title": "Build toward a Google Knowledge Panel",
                "action": (
                    "Focus on Wikipedia and Wikidata presence first — these are the primary signals "
                    "Google uses to trigger a Knowledge Panel for your brand."
                ),
            }
        )

    if not checks.get("crunchbase", EntityCheckResult("", False)).found:
        recs.append(
            {
                "priority": "medium",
                "title": "Create a Crunchbase profile",
                "action": (
                    "A Crunchbase profile adds a high-authority backlink and is used by several AI "
                    "systems to verify business legitimacy."
                ),
            }
        )

    if not checks.get("linkedin", EntityCheckResult("", False)).found:
        recs.append(
            {
                "priority": "medium",
                "title": "Create a LinkedIn Company page",
                "action": (
                    "A LinkedIn Company page strengthens entity authority and is referenced by "
                    "several AI models when identifying business entities."
                ),
            }
        )

    return recs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def check_entity(
    target_domain: str,
    target_brand: str,
    serper_api_key: Optional[str] = None,
) -> EntityReport:
    """
    Run all entity checks in parallel with individual 10-second timeouts.
    Returns an EntityReport with score and recommendations.
    """
    if serper_api_key is None:
        serper_api_key = os.getenv("SERPER_API_KEY")

    timeout = httpx.Timeout(10.0)
    headers = {"User-Agent": "nrankai-bot/1.0 (https://nrankai.com)"}

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as session:

        async def safe(coro, source_name: str) -> EntityCheckResult:
            try:
                return await asyncio.wait_for(coro, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("Entity check %r timed out", source_name)
                return EntityCheckResult(source=source_name, found=False, extra={"error": "timeout"})
            except Exception as exc:
                logger.warning("Entity check %r failed: %s", source_name, exc)
                return EntityCheckResult(
                    source=source_name, found=False, extra={"error": str(exc)}
                )

        results = await asyncio.gather(
            safe(_check_wikipedia(target_brand, session), "wikipedia_en"),
            safe(_check_wikidata(target_domain, session), "wikidata"),
            safe(_check_schema_markup(target_domain, session), "schema_markup"),
            safe(_check_crunchbase(target_brand, serper_api_key, session), "crunchbase"),
            safe(_check_knowledge_panel(target_brand, serper_api_key, session), "knowledge_panel"),
            safe(_check_linkedin(target_brand, serper_api_key, session), "linkedin"),
        )

    checks: Dict[str, EntityCheckResult] = {r.source: r for r in results}

    score = _compute_score(checks)
    recommendations = _build_recommendations(checks)

    return EntityReport(
        target_domain=target_domain,
        target_brand=target_brand,
        checks=checks,
        entity_authority_score=score,
        recommendations=recommendations,
        analyzed_at=datetime.now(timezone.utc).isoformat(),
    )
