"""
Visibility tracking — unified citation + GEO-mention monitoring.

Etapa 3 of the consolidation (docs/CONSOLIDATION_PLAN.md). Replaces
api/routes/citation_tracker.py and api/routes/geo_monitor.py, which were the
same feature written twice: create a target -> generate tracking queries ->
query LLM providers -> analyze responses for mentions/citations -> trend ->
alert on drop. citation_tracker.py tracked URL citations specifically
(url_patterns); geo_monitor.py tracked broader brand mentions
(brand_keywords) plus competitor mentions, which citation tracking had no
equivalent of. api/routes/ai_visibility.py existed solely to merge the two
back together after the fact -- the tell that they should have been one
thing.

Data model: extends CitationTracker/CitationScan (api/models/content.py)
rather than GeoMonitorProject/GeoMonitorScan, because the former already had
real data (1 tracker, 5 scans) and the latter had none (confirmed via
docs/audit/FINDINGS.md) -- see migrations/versions/0010_unify_visibility_
tracking.py for the column additions and full rationale.

Along the way, two previously-undiscovered bugs in geo_monitor.py were
confirmed while building this replacement (not just "unused" -- structurally
broken):
  1. GeoMonitorProject has no brand_keywords or language column at all, yet
     create_project() set them as plain instance attributes -- silently
     discarded on commit, never persisted. Any scan run on a freshly-loaded
     project would then crash on json.loads(None) trying to read them back.
  2. _run_geo_scan() wrote to scan.total_checks / .completed_checks /
     .mention_count / .citation_count -- none of which exist as columns on
     GeoMonitorScan (which has total_queries / mentioned_count instead) --
     also silently discarded.
This is why geo_monitor_projects/geo_monitor_scans have zero rows in
production: the feature could not have completed a real scan. That also
means there is no working prior behavior to preserve for the scan engine
itself -- only the URL/response *shapes* below are kept compatible with
whatever templates/JS already call them.

Regla de aur (docs/CONSOLIDATION_PLAN.md principle #2): public URLs and
response shapes don't change. Both prefixes stay mounted:
  /api/citations/*    -- unchanged from citation_tracker.py
  /api/geo-monitor/*  -- unchanged from geo_monitor.py, now backed by the
                         same CitationTracker/CitationScan rows, and with
                         brand_keywords/language actually persisted
/api/ai-visibility/* was simplified separately (api/routes/ai_visibility.py)
since it now reads one table pair instead of merging two.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.limiter import limiter
from api.models.database import AsyncSessionLocal, CitationScan, CitationTracker, get_db
from api.provider_registry import get_default_model
from api.routes.costs import track_cost
from api.utils.errors import raise_bad_request, raise_not_found
from core.direct_analyzer import AsyncLLMClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/citations", tags=["citations"])
router_geo = APIRouter(prefix="/api/geo-monitor", tags=["geo_monitor"])

LLM_SEMAPHORE = asyncio.Semaphore(3)


# ============================================================================
# PYDANTIC MODELS -- /api/citations
# ============================================================================

def _validate_domain(v: str) -> str:
    v = v.strip().lower()
    bare = re.sub(r'^https?://', '', v)
    bare = bare.split('/')[0]
    if not bare or "." not in bare:
        raise ValueError("Invalid website format")
    if not re.match(r'^[a-z0-9]([a-z0-9\-\.]{0,250}[a-z0-9])?$', bare):
        raise ValueError("Invalid website format — must be a domain name")
    if '..' in bare:
        raise ValueError("Invalid website — path traversal not allowed")
    return v


class CreateTrackerRequest(BaseModel):
    """Request to create a new citation tracker."""
    name: str = Field(..., min_length=3, max_length=500)
    website: str = Field(..., min_length=1, max_length=500)
    url_patterns: List[str] = Field(..., max_length=100)
    tracking_queries: List[str] = Field(..., max_length=100)
    providers_config: Dict[str, bool]
    schedule_cron: Optional[str] = Field(None, max_length=100)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or len(v) < 3:
            raise ValueError("Name must be at least 3 characters")
        return v

    @field_validator("website")
    @classmethod
    def validate_website(cls, v: str) -> str:
        return _validate_domain(v)

    @field_validator("url_patterns")
    @classmethod
    def validate_url_patterns(cls, v: List[str]) -> List[str]:
        if not v or len(v) == 0:
            raise ValueError("At least one URL pattern required")
        if len(v) > 100:
            raise ValueError("Maximum 100 URL patterns allowed")
        return v

    @field_validator("tracking_queries")
    @classmethod
    def validate_tracking_queries(cls, v: List[str]) -> List[str]:
        if not v or len(v) < 5:
            raise ValueError("At least 5 tracking queries required")
        if len(v) > 100:
            raise ValueError("Maximum 100 tracking queries allowed")
        return v

    @field_validator("providers_config")
    @classmethod
    def validate_providers_config(cls, v: Dict[str, bool]) -> Dict[str, bool]:
        valid_providers = {"chatgpt", "claude", "perplexity"}
        if not any(v.get(p, False) for p in valid_providers):
            raise ValueError("At least one provider must be enabled")
        return v


class GenerateQueriesRequest(BaseModel):
    website: str = Field(..., min_length=1, max_length=500)
    industry: Optional[str] = Field(None, max_length=200)

    @field_validator("website")
    @classmethod
    def validate_website(cls, v: str) -> str:
        return _validate_domain(v)


class QuerySuggestion(BaseModel):
    query: str
    category: str  # informational, commercial, comparative


# ============================================================================
# PYDANTIC MODELS -- /api/geo-monitor
# ============================================================================

class CompetitorConfig(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    brand_keywords: List[str] = Field(..., min_length=1)
    website: str = Field(..., min_length=1, max_length=255)


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=500)
    website: str = Field(..., min_length=1, max_length=500)
    brand_keywords: List[str] = Field(..., max_length=100)
    target_queries: List[str] = Field(..., max_length=100)
    providers: Dict[str, bool]
    language: str = Field("English", max_length=50)
    competitors: Optional[List[CompetitorConfig]] = Field(default_factory=list)

    @field_validator('website')
    @classmethod
    def validate_website(cls, v: str) -> str:
        return _validate_domain(v)

    @field_validator('brand_keywords')
    @classmethod
    def validate_keywords(cls, v):
        if not v:
            raise ValueError("At least one brand keyword is required")
        if len(v) > 100:
            raise ValueError("Maximum 100 brand keywords allowed")
        return v

    @field_validator('target_queries')
    @classmethod
    def validate_queries(cls, v):
        if not v:
            raise ValueError("At least one target query is required")
        if len(v) > 100:
            raise ValueError("Maximum 100 target queries allowed")
        return v

    @field_validator('providers')
    @classmethod
    def validate_providers(cls, v):
        if not any(v.values()):
            raise ValueError("At least one provider must be enabled")
        return v


class ScanRequest(BaseModel):
    providers: Optional[Dict[str, bool]] = None


class UpdateProjectRequest(BaseModel):
    alert_threshold: Optional[float] = None
    alert_webhook_url: Optional[str] = None
    competitors: Optional[List[CompetitorConfig]] = None


# ============================================================================
# ANALYSIS HELPERS
# ============================================================================

def extract_cited_urls(response_text: str, domain: str) -> List[str]:
    """Extract all URLs matching the tracked domain from response."""
    domain_escaped = re.escape(domain)
    pattern = rf'https?://(?:www\.)?{domain_escaped}[^\s\)\]\"\'\<\>]*'
    urls = re.findall(pattern, response_text, re.IGNORECASE)
    cleaned = [re.sub(r'[.,;:!?]+$', '', u) for u in urls]
    return list(set(cleaned))


def check_brand_mention(response_text: str, website: str) -> bool:
    """Check if brand/domain is mentioned (without necessarily having a URL)."""
    brand = website.split('.')[0]
    return bool(re.search(rf'\b{re.escape(brand)}\b', response_text, re.IGNORECASE))


def extract_citation_context(response_text: str, url: str, context_chars: int = 200) -> str:
    """Extract context around a cited URL."""
    try:
        url_escaped = re.escape(url)
        match = re.search(url_escaped, response_text, re.IGNORECASE)
        if not match:
            return ""
        start = match.start()
        context_start = max(0, start - context_chars // 2)
        context_end = min(len(response_text), start + len(url) + context_chars // 2)
        context = response_text[context_start:context_end]
        if context_start > 0:
            context = "..." + context
        if context_end < len(response_text):
            context = context + "..."
        return context.strip()
    except Exception:
        return ""


def analyze_mentions(response_text: str, brand_keywords: List[str], website: str) -> Dict:
    """Analyze an LLM response for brand mentions, sentiment, and position.

    brand_keywords: explicit keywords to match (geo_monitor's approach --
    broader than a bare domain check). Falls back to check_brand_mention()
    against the website domain if no keywords are configured, preserving
    citation_tracker.py's original (narrower) behavior for trackers that
    never set brand_keywords.
    """
    text_lower = response_text.lower()

    mentioned = False
    matched_keyword = None
    keywords = brand_keywords or [website.split('.')[0]]
    for keyword in keywords:
        if keyword.lower() in text_lower:
            mentioned = True
            matched_keyword = keyword
            break

    context = ""
    if mentioned and matched_keyword:
        idx = text_lower.find(matched_keyword.lower())
        start = max(0, idx - 80)
        end = min(len(response_text), idx + len(matched_keyword) + 120)
        context = response_text[start:end].strip()
        if start > 0:
            context = "..." + context
        if end < len(response_text):
            context = context + "..."

    position = "not_found"
    if mentioned:
        first_quarter = text_lower[:len(text_lower) // 4]
        if matched_keyword.lower() in first_quarter:
            position = "primary_recommendation"
        elif text_lower.count(matched_keyword.lower()) >= 2:
            position = "listed"
        else:
            position = "mentioned_in_passing"

    sentiment = "neutral"
    if context:
        positive_words = ["best", "recommend", "excellent", "great", "top", "leading", "popular", "trusted",
                           "cel mai bun", "recomandat", "excelent", "foarte bun", "recomandate"]
        negative_words = ["avoid", "worst", "bad", "poor", "issues", "problems", "evitați", "probleme",
                           "slab", "prost", "nesigur"]
        context_l = context.lower()
        if any(w in context_l for w in positive_words):
            sentiment = "positive"
        elif any(w in context_l for w in negative_words):
            sentiment = "negative"

    return {
        "mentioned": mentioned,
        "matched_keyword": matched_keyword,
        "context": context,
        "sentiment": sentiment,
        "position": position,
    }


async def _query_provider(
    provider: str, query: str, model: Optional[str] = None
) -> tuple[str, int, int, str]:
    """Query a single LLM provider with a plain conversational query.

    Shared by both the /api/citations and /api/geo-monitor scan engines --
    previously each module instantiated its own provider SDK clients here
    (docs/CONSOLIDATION_PLAN.md Etapa 2.2).
    """
    async with LLM_SEMAPHORE:
        try:
            if provider == "claude":
                api_key = os.getenv("ANTHROPIC_API_KEY")
                _model = model or get_default_model("anthropic")
                if not api_key:
                    return "", 0, 0, _model
                client = AsyncLLMClient(provider="ANTHROPIC", model_name=_model)
                text, in_tok, out_tok = await client.complete(
                    system_message="", user_content=query, max_tokens=1500,
                    content_prefix="", force_json=False,
                )
                await client.close()
                return text or "", in_tok, out_tok, _model

            elif provider == "chatgpt":
                api_key = os.getenv("OPENAI_API_KEY")
                _model = model or get_default_model("openai")
                if not api_key:
                    return "", 0, 0, _model
                client = AsyncLLMClient(provider="OPENAI", model_name=_model)
                text, in_tok, out_tok = await client.complete(
                    system_message="", user_content=query, max_tokens=1500,
                    content_prefix="", force_json=False,
                )
                await client.close()
                return text or "", in_tok, out_tok, _model

            elif provider == "perplexity":
                api_key = os.getenv("PERPLEXITY_API_KEY")
                _model = model or "sonar"
                if not api_key:
                    return "", 0, 0, _model
                client = AsyncLLMClient(provider="PERPLEXITY", model_name=_model)
                text, in_tok, out_tok = await client.complete(
                    system_message="", user_content=query, max_tokens=1500,
                    content_prefix="", force_json=False,
                )
                await client.close()
                return text or "", in_tok, out_tok, _model

            elif provider == "gemini":
                import google.generativeai as genai
                _model = model or "gemini-2.0-flash"
                api_key = os.getenv("GEMINI_API_KEY")
                if not api_key:
                    return "", 0, 0, _model
                genai.configure(api_key=api_key)
                gemini_model = genai.GenerativeModel(_model)
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, lambda: gemini_model.generate_content(query))
                response_text = response.text
                input_tokens = int(len(query.split()) * 1.3)
                output_tokens = int(len(response_text.split()) * 1.3)
                return response_text, input_tokens, output_tokens, _model

            else:
                return "", 0, 0, provider

        except Exception as e:
            logger.warning("Error querying %s: %s", provider, e)
            return "", 0, 0, model or provider


# ============================================================================
# QUERY SUGGESTIONS
# ============================================================================

def generate_citation_queries(website: str, industry: Optional[str] = None) -> List[QuerySuggestion]:
    """Generate suggested tracking queries for a website (citation-tracker style)."""
    brand = website.split('.')[0].capitalize()
    industry_context = industry or "servicii"
    queries = []

    informational = [
        f"Ce este {brand}", f"Cum funcționează {brand}", f"Ghid {brand}",
        f"Informații despre {brand}", f"{brand} explicat", f"Tutoriale {brand}",
        f"Cum să folosesc {brand}", f"Detalii {brand}", f"Overview {brand}", f"Introducere {brand}",
    ]
    for q in informational:
        queries.append(QuerySuggestion(query=q, category="informational"))

    commercial = [
        f"Cel mai bun {industry_context} România", f"Recomandări {industry_context}",
        f"{industry_context} review", f"Top {industry_context} {datetime.now().year}",
        f"{brand} review", f"{brand} opinie", f"Merită {brand}", f"{brand} alternative",
        f"Avantaje {brand}", f"De ce {brand}",
    ]
    for q in commercial:
        queries.append(QuerySuggestion(query=q, category="commercial"))

    comparative = [
        f"{brand} vs competitori", f"Alternative la {brand}", f"{brand} sau alternative",
        f"Comparație {industry_context} România", f"Top 10 {industry_context}",
        f"Cel mai bun vs {brand}", f"{brand} diferențe", f"Care este mai bun {brand}",
        f"Lista {industry_context} România", f"Ranking {industry_context}",
    ]
    for q in comparative:
        queries.append(QuerySuggestion(query=q, category="comparative"))

    return queries


def generate_suggested_queries(website: str, brand: str, language: str = "English") -> List[str]:
    """Generate 10 suggested queries based on domain and language (geo-monitor style)."""
    domain_lower = website.lower()

    if language.lower() in ["romanian", "română"]:
        if any(x in domain_lower for x in ["bank", "banca", "ing", "bcr", "brd"]):
            return [
                "Care sunt cele mai bune bănci din România?", "Ce bancă recomandați pentru cont curent?",
                f"{brand} recenzii și opinii", "Cel mai bun credit ipotecar România",
                f"{brand} vs BCR vs BRD - comparație", "Cum deschid cont bancar online România?",
                f"Ce dobândă oferă {brand} la depozit?", "Aplicație banking recomandată România",
                "Servicii bancare pentru freelanceri România", "Care e cea mai sigură bancă din România?",
            ]
        elif any(x in domain_lower for x in ["shop", "magazin", "store"]):
            return [
                "Cele mai bune magazine online România", f"Unde cumpăr {brand} cu livrare rapidă?",
                f"{brand} review și recenzii", "Magazine online de încredere România",
                f"{brand} vs competitori - comparație", f"Oferte și promoții {brand}",
                f"Returnare produse la {brand}", f"Livrare gratuită {brand}",
                f"Cod reducere {brand}", f"Opinii clienți despre {brand}",
            ]
        else:
            return [
                f"Cele mai bune companii {brand} România", f"Recenzii {brand}",
                f"Ce servicii oferă {brand}?", f"{brand} prețuri și tarife",
                f"Alternative la {brand} România", f"Recomandări {brand}",
                f"Contact și suport {brand}", f"Opinii despre {brand}",
                f"Cum funcționează {brand}?", f"Avantaje {brand} vs competitori",
            ]
    else:
        if any(x in domain_lower for x in ["bank", "finance", "credit"]):
            return [
                f"What are the best banks for {brand}?", f"Is {brand} a good bank?",
                f"{brand} reviews and ratings", f"Best mortgage rates from {brand}",
                f"{brand} vs competitors comparison", f"How to open account at {brand}?",
                f"What interest rates does {brand} offer?", "Best banking app recommendations",
                "Banking services for freelancers", "Most trusted banks in the region",
            ]
        elif any(x in domain_lower for x in ["shop", "store", "retail"]):
            return [
                f"Best online stores like {brand}", f"Where to buy from {brand}?",
                f"{brand} review and customer feedback", "Trusted online retailers",
                f"{brand} vs competitors", f"Deals and promotions at {brand}",
                f"Return policy for {brand}", f"Free shipping from {brand}",
                f"Discount codes for {brand}", f"Customer reviews about {brand}",
            ]
        else:
            return [
                f"Best {brand} recommendations", f"Reviews of {brand}",
                f"What services does {brand} offer?", f"{brand} pricing and rates",
                f"Alternatives to {brand}", f"Recommendations for {brand}",
                f"Contact and support for {brand}", f"Opinions about {brand}",
                f"How does {brand} work?", f"Advantages of {brand} vs competitors",
            ]


# ============================================================================
# ALERTING
# ============================================================================

async def _check_and_alert_drop(tracker: CitationTracker, current_scan: CitationScan, db: AsyncSession):
    """Fire a webhook if citation_rate or visibility_score dropped significantly."""
    if not tracker.alert_webhook_url:
        return

    result = await db.execute(
        select(CitationScan)
        .where(CitationScan.tracker_id == tracker.id)
        .where(CitationScan.status == "completed")
        .where(CitationScan.id != current_scan.id)
        .order_by(CitationScan.completed_at.desc())
        .limit(1)
    )
    prev = result.scalar_one_or_none()
    if not prev:
        return

    threshold = tracker.alert_threshold or 15.0
    drops = []
    if prev.citation_rate is not None and current_scan.citation_rate is not None:
        drop = prev.citation_rate - current_scan.citation_rate
        if drop >= threshold:
            drops.append(("citation_rate", prev.citation_rate, current_scan.citation_rate, drop))
    if prev.visibility_score is not None and current_scan.visibility_score is not None:
        drop = prev.visibility_score - current_scan.visibility_score
        if drop >= threshold:
            drops.append(("visibility_score", prev.visibility_score, current_scan.visibility_score, drop))

    if not drops:
        return

    metric, prev_val, cur_val, drop = drops[0]
    payload = {
        "type": f"{metric}_drop",
        "tracker_name": tracker.name,
        "website": tracker.website,
        "previous_value": round(prev_val, 1),
        "current_value": round(cur_val, 1),
        "drop": round(drop, 1),
        "threshold": threshold,
        "scan_url": f"/citations/trackers/{tracker.id}",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(tracker.alert_webhook_url, json=payload)
        logger.info("Drop alert sent for %s: %s -%.1f", tracker.website, metric, drop)
    except Exception as e:
        logger.warning("Failed to send drop alert: %s", e)


# ============================================================================
# UNIFIED SCAN ENGINE
# ============================================================================

async def _run_visibility_scan(
    tracker_id: str,
    providers_override: Optional[Dict[str, bool]] = None,
    scan_id: Optional[str] = None,
):
    """Run a complete visibility scan for a tracker: queries every enabled
    provider for every tracking query, and computes BOTH a citation rate
    (URL-pattern based, if url_patterns is set) and a visibility/mention
    rate (keyword based, via brand_keywords or a bare domain check) in the
    same pass -- replacing the two separate, near-identical scan engines
    that citation_tracker.py and geo_monitor.py each had.

    scan_id: if the caller already created a CitationScan row (geo-monitor's
        /projects/{id}/scan returns a scan_id immediately, before the scan
        actually runs, matching geo_monitor.py's original contract), pass it
        here to update that row in place. Otherwise a new one is created --
        citation-tracker's /trackers/{id}/scan has no such pre-created row.
        Passing tracker_id alone and letting this function mint its own
        scan_id here would silently orphan the caller's pre-created row --
        exactly the bug this parameter exists to avoid.
    """
    async with AsyncSessionLocal() as db:
        scan_id = scan_id or str(uuid.uuid4())
        try:
            result = await db.execute(select(CitationTracker).where(CitationTracker.id == tracker_id))
            tracker = result.scalar_one_or_none()
            if not tracker:
                logger.error("Tracker %s not found", tracker_id)
                return

            url_patterns = json.loads(tracker.url_patterns) if tracker.url_patterns else []
            tracking_queries = json.loads(tracker.tracking_queries)
            providers_config = json.loads(tracker.providers_config)
            if providers_override:
                providers_config = providers_override
            brand_keywords = json.loads(tracker.brand_keywords) if tracker.brand_keywords else []
            competitors = tracker.competitors or []

            enabled_providers = [p for p, enabled in providers_config.items() if enabled]
            if not enabled_providers:
                logger.error("No providers enabled for tracker %s", tracker_id)
                return

            scan_result = await db.execute(select(CitationScan).where(CitationScan.id == scan_id))
            scan = scan_result.scalar_one_or_none()
            if scan is None:
                scan = CitationScan(id=scan_id, tracker_id=tracker_id)
                db.add(scan)
            scan.status = "running"
            scan.total_queries = len(tracking_queries) * len(enabled_providers)
            scan.started_at = datetime.now(timezone.utc)
            await db.commit()

            all_results = []
            url_citation_counts: Dict[str, int] = {}
            provider_stats = {p: {"citations": 0, "mentions": 0, "queries": 0, "responses": 0} for p in enabled_providers}
            competitor_results: Dict[str, Dict] = {}

            for query_idx, query in enumerate(tracking_queries):
                query_result = {"query": query, "query_index": query_idx + 1, "providers": {}}

                for provider in enabled_providers:
                    await asyncio.sleep(1)  # spread out same-provider requests

                    response, in_tok, out_tok, model_name = await _query_provider(provider, query)
                    if in_tok or out_tok:
                        # Awaited, not fire-and-forget via asyncio.create_task: track_cost()
                        # opens its own AsyncSessionLocal(), and firing it concurrently while
                        # this function's own long-lived `db` session is mid-scan caused the
                        # final "completed" commit below to silently not persist -- both
                        # sessions share one physical SQLite connection (StaticPool), and
                        # interleaving separate transactions on it raced. Reproduced live
                        # during Etapa 3 testing; the original citation_tracker.py /
                        # geo_monitor.py had the same fire-and-forget pattern, so this was a
                        # latent bug, not something introduced by the unification.
                        await track_cost(
                            source="visibility_scan", provider=provider, model=model_name,
                            input_tokens=in_tok, output_tokens=out_tok,
                            source_id=scan_id, website=tracker.website,
                        )

                    if not response:
                        query_result["providers"][provider] = {
                            "cited": False, "mentioned": False, "cited_urls": [], "error": "No response",
                        }
                        continue

                    # Citation analysis (URL-pattern based)
                    cited_urls: List[str] = []
                    for pattern in url_patterns:
                        cited_urls.extend(extract_cited_urls(response, pattern))
                    cited_urls = list(set(cited_urls))
                    is_cited = len(cited_urls) > 0
                    for url in cited_urls:
                        url_citation_counts[url] = url_citation_counts.get(url, 0) + 1
                    citation_context = extract_citation_context(response, cited_urls[0]) if cited_urls else ""

                    # Mention analysis (keyword based)
                    analysis = analyze_mentions(response, brand_keywords, tracker.website)

                    query_result["providers"][provider] = {
                        "cited": is_cited,
                        "mentioned": analysis["mentioned"],
                        "cited_urls": cited_urls,
                        "context": citation_context or analysis["context"],
                        "sentiment": analysis["sentiment"],
                        "position": analysis["position"],
                    }

                    provider_stats[provider]["queries"] += 1
                    provider_stats[provider]["responses"] += 1
                    if is_cited:
                        provider_stats[provider]["citations"] += 1
                    if analysis["mentioned"]:
                        provider_stats[provider]["mentions"] += 1

                    # Competitor mentions in the same response
                    for comp in competitors:
                        comp_website = comp.get("website", "")
                        comp_keywords = comp.get("brand_keywords", [])
                        if not comp_keywords or not comp_website:
                            continue
                        comp_analysis = analyze_mentions(response, comp_keywords, comp_website)
                        bucket = competitor_results.setdefault(
                            comp_website, {"name": comp.get("name", comp_website), "mention_count": 0, "total": 0}
                        )
                        bucket["total"] += 1
                        if comp_analysis["mentioned"]:
                            bucket["mention_count"] += 1

                all_results.append(query_result)

            total_citations = sum(1 for r in all_results if any(p.get("cited") for p in r["providers"].values()))
            total_mentions = sum(1 for r in all_results if any(p.get("mentioned") for p in r["providers"].values()))
            n_queries = len(tracking_queries) or 1
            citation_rate = (total_citations / n_queries * 100) if url_patterns else None
            visibility_score = (total_mentions / n_queries * 100)

            top_cited_urls = [
                {"url": url, "count": count}
                for url, count in sorted(url_citation_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            ]

            for provider, stats in provider_stats.items():
                if stats["queries"] > 0:
                    stats["citation_rate"] = (stats["citations"] / stats["queries"]) * 100
                    stats["mention_rate"] = (stats["mentions"] / stats["queries"]) * 100
                else:
                    stats["citation_rate"] = 0
                    stats["mention_rate"] = 0

            competitor_scores = {
                website: {"name": data["name"], "mention_rate": round(data["mention_count"] / data["total"] * 100, 1)}
                for website, data in competitor_results.items() if data["total"] > 0
            }

            scan.status = "completed"
            scan.total_citations = total_citations
            scan.total_mentions = total_mentions
            scan.citation_rate = round(citation_rate, 2) if citation_rate is not None else None
            scan.visibility_score = round(visibility_score, 2)
            scan.results_json = json.dumps(all_results)
            scan.provider_breakdown = json.dumps(provider_stats)
            scan.top_cited_urls = json.dumps(top_cited_urls)
            scan.competitor_scores = competitor_scores
            scan.completed_at = datetime.now(timezone.utc)
            tracker.last_scan_at = datetime.now(timezone.utc)
            await db.commit()

            await _check_and_alert_drop(tracker, scan, db)

            logger.info(
                "Visibility scan %s completed for %s: citation_rate=%s visibility_score=%.1f",
                scan_id, tracker.website, scan.citation_rate, visibility_score,
            )

        except Exception as e:
            logger.error("Visibility scan error: %s", e)
            try:
                result = await db.execute(select(CitationScan).where(CitationScan.id == scan_id))
                scan = result.scalar_one_or_none()
                if scan:
                    scan.status = "failed"
                    scan.completed_at = datetime.now(timezone.utc)
                    await db.commit()
            except Exception:
                pass


# ============================================================================
# /api/citations ENDPOINTS -- unchanged from citation_tracker.py
# ============================================================================

@router.post("/trackers")
async def create_tracker(request: CreateTrackerRequest, db: AsyncSession = Depends(get_db)):
    """Create a new citation tracker."""
    tracker_id = str(uuid.uuid4())
    tracker = CitationTracker(
        id=tracker_id,
        name=request.name,
        website=request.website,
        url_patterns=json.dumps(request.url_patterns),
        tracking_queries=json.dumps(request.tracking_queries),
        providers_config=json.dumps(request.providers_config),
        schedule_cron=request.schedule_cron,
        is_active=1,
    )
    db.add(tracker)
    await db.commit()
    return {"success": True, "tracker_id": tracker_id, "tracker": tracker.to_dict()}


@router.get("/trackers")
async def list_trackers(db: AsyncSession = Depends(get_db)):
    """List all citation trackers with their latest scan metrics."""
    result = await db.execute(select(CitationTracker).order_by(desc(CitationTracker.created_at)))
    trackers = result.scalars().all()

    trackers_data = []
    for tracker in trackers:
        tracker_dict = tracker.to_dict()
        scan_result = await db.execute(
            select(CitationScan).where(CitationScan.tracker_id == tracker.id)
            .order_by(desc(CitationScan.created_at)).limit(1)
        )
        latest_scan = scan_result.scalar_one_or_none()
        if latest_scan:
            tracker_dict["latest_citation_rate"] = latest_scan.citation_rate
            tracker_dict["latest_scan_at"] = latest_scan.created_at.isoformat()
        else:
            tracker_dict["latest_citation_rate"] = None
            tracker_dict["latest_scan_at"] = None

        count_result = await db.execute(
            select(func.count(CitationScan.id)).where(CitationScan.tracker_id == tracker.id)
        )
        tracker_dict["scan_count"] = count_result.scalar()
        trackers_data.append(tracker_dict)

    return {"success": True, "trackers": trackers_data}


@router.get("/trackers/{tracker_id}")
async def get_tracker(tracker_id: str, db: AsyncSession = Depends(get_db)):
    """Get detailed tracker information with scan history."""
    result = await db.execute(select(CitationTracker).where(CitationTracker.id == tracker_id))
    tracker = result.scalar_one_or_none()
    if not tracker:
        raise_not_found("Tracker")

    tracker_dict = tracker.to_dict()
    scans_result = await db.execute(
        select(CitationScan).where(CitationScan.tracker_id == tracker_id)
        .order_by(desc(CitationScan.created_at)).limit(20)
    )
    tracker_dict["scans"] = [s.to_dict() for s in scans_result.scalars().all()]
    return {"success": True, "tracker": tracker_dict}


@router.post("/trackers/{tracker_id}/scan")
@limiter.limit("20/hour")
async def start_scan(
    request: Request, tracker_id: str, background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Start a manual citation scan."""
    result = await db.execute(select(CitationTracker).where(CitationTracker.id == tracker_id))
    tracker = result.scalar_one_or_none()
    if not tracker:
        raise_not_found("Tracker")

    running_result = await db.execute(
        select(CitationScan).where(CitationScan.tracker_id == tracker_id).where(CitationScan.status == "running")
    )
    if running_result.scalar_one_or_none():
        raise_bad_request("Scan already in progress")

    background_tasks.add_task(_run_visibility_scan, tracker_id)
    return {"success": True, "message": "Citation scan started", "tracker_id": tracker_id}


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: str, db: AsyncSession = Depends(get_db)):
    """Get detailed scan results."""
    result = await db.execute(select(CitationScan).where(CitationScan.id == scan_id))
    scan = result.scalar_one_or_none()
    if not scan:
        raise_not_found("Scan")
    return {"success": True, "scan": scan.to_dict()}


@router.get("/trackers/{tracker_id}/trend")
async def get_tracker_trend(tracker_id: str, db: AsyncSession = Depends(get_db)):
    """Get citation rate trend data for Chart.js."""
    result = await db.execute(select(CitationTracker).where(CitationTracker.id == tracker_id))
    tracker = result.scalar_one_or_none()
    if not tracker:
        raise_not_found("Tracker")

    scans_result = await db.execute(
        select(CitationScan).where(CitationScan.tracker_id == tracker_id)
        .where(CitationScan.status == "completed").order_by(CitationScan.created_at)
    )
    scans = scans_result.scalars().all()
    if not scans:
        return {"success": True, "chart_data": {"labels": [], "datasets": []}}

    providers_config = json.loads(tracker.providers_config)
    enabled_providers = [p for p, enabled in providers_config.items() if enabled]

    labels, overall_data = [], []
    provider_data = {p: [] for p in enabled_providers}
    for scan in scans:
        labels.append(scan.created_at.strftime("%Y-%m-%d %H:%M"))
        overall_data.append(scan.citation_rate or 0)
        breakdown = json.loads(scan.provider_breakdown) if scan.provider_breakdown else {}
        for provider in enabled_providers:
            provider_data[provider].append(breakdown.get(provider, {}).get("citation_rate", 0))

    datasets = [{
        "label": "Overall Citation Rate", "data": overall_data,
        "borderColor": "rgb(59, 130, 246)", "backgroundColor": "rgba(59, 130, 246, 0.1)", "tension": 0.3,
    }]
    provider_colors = {"chatgpt": "rgb(16, 163, 127)", "claude": "rgb(168, 85, 247)", "perplexity": "rgb(245, 158, 11)"}
    for provider in enabled_providers:
        color = provider_colors.get(provider, "rgb(107, 114, 128)")
        datasets.append({
            "label": provider.capitalize(), "data": provider_data[provider],
            "borderColor": color, "backgroundColor": color.replace("rgb", "rgba").replace(")", ", 0.1)"),
            "tension": 0.3,
        })

    return {"success": True, "chart_data": {"labels": labels, "datasets": datasets}}


@router.delete("/trackers/{tracker_id}")
async def delete_tracker(tracker_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a citation tracker and all its scans."""
    result = await db.execute(select(CitationTracker).where(CitationTracker.id == tracker_id))
    tracker = result.scalar_one_or_none()
    if not tracker:
        raise_not_found("Tracker")
    await db.delete(tracker)
    await db.commit()
    return {"success": True, "message": "Tracker deleted"}


@router.post("/generate-queries")
async def generate_queries(request: GenerateQueriesRequest):
    """Generate suggested tracking queries for a website."""
    suggestions = generate_citation_queries(request.website, request.industry)
    return {"success": True, "suggestions": [{"query": s.query, "category": s.category} for s in suggestions]}


@router.patch("/trackers/{tracker_id}/toggle")
async def toggle_tracker(tracker_id: str, db: AsyncSession = Depends(get_db)):
    """Toggle tracker active status."""
    result = await db.execute(select(CitationTracker).where(CitationTracker.id == tracker_id))
    tracker = result.scalar_one_or_none()
    if not tracker:
        raise_not_found("Tracker")
    tracker.is_active = 1 if tracker.is_active == 0 else 0
    await db.commit()
    return {
        "success": True, "is_active": bool(tracker.is_active),
        "message": "Tracker activated" if tracker.is_active else "Tracker paused",
    }


# ============================================================================
# /api/geo-monitor ENDPOINTS -- unchanged response shapes from geo_monitor.py,
# now backed by CitationTracker/CitationScan
# ============================================================================

def _as_project_dict(tracker: CitationTracker) -> dict:
    """CitationTracker.to_dict() using geo_monitor.py's original field names
    (target_queries instead of tracking_queries) so existing consumers of
    /api/geo-monitor/* don't see a shape change."""
    d = tracker.to_dict()
    d["target_queries"] = d.pop("tracking_queries")
    d.pop("url_patterns", None)
    return d


def _as_scan_dict(scan: CitationScan) -> dict:
    """CitationScan.to_dict() using geo_monitor.py's original field names
    (mentioned_count instead of total_mentions)."""
    d = scan.to_dict()
    d["project_id"] = d.pop("tracker_id")
    d["mentioned_count"] = d["total_mentions"]
    return d


@router_geo.post("/projects")
async def create_project(request: CreateProjectRequest, db: AsyncSession = Depends(get_db)):
    """Create a new GEO monitoring project."""
    project_id = str(uuid.uuid4())
    tracker = CitationTracker(
        id=project_id,
        name=request.name,
        website=request.website,
        # No URL patterns from this creation path -- geo-monitor projects
        # track brand mentions, not URL citations. citation_rate stays None
        # for these until/unless url_patterns is added via /api/citations.
        url_patterns=json.dumps([]),
        tracking_queries=json.dumps(request.target_queries),
        providers_config=json.dumps(request.providers),
        brand_keywords=json.dumps(request.brand_keywords),
        language=request.language,
        competitors=[c.model_dump() for c in request.competitors] if request.competitors else [],
        is_active=1,
    )
    db.add(tracker)
    await db.commit()

    suggested = generate_suggested_queries(request.website, request.name, request.language)
    return {"id": project_id, "project": _as_project_dict(tracker), "suggested_queries": suggested}


@router_geo.get("/projects")
async def list_projects(db: AsyncSession = Depends(get_db)):
    """List all GEO monitoring projects with latest scan scores."""
    result = await db.execute(select(CitationTracker).order_by(desc(CitationTracker.created_at)))
    trackers = result.scalars().all()

    projects_data = []
    for tracker in trackers:
        project_dict = _as_project_dict(tracker)
        scan_result = await db.execute(
            select(CitationScan).where(CitationScan.tracker_id == tracker.id)
            .order_by(desc(CitationScan.created_at)).limit(1)
        )
        latest_scan = scan_result.scalar_one_or_none()
        if latest_scan:
            project_dict["latest_scan"] = {
                "id": latest_scan.id,
                "visibility_score": latest_scan.visibility_score,
                "status": latest_scan.status,
                "completed_at": latest_scan.completed_at.isoformat() if latest_scan.completed_at else None,
            }
        else:
            project_dict["latest_scan"] = None

        count_result = await db.execute(select(CitationScan.id).where(CitationScan.tracker_id == tracker.id))
        project_dict["scan_count"] = len(count_result.all())
        projects_data.append(project_dict)

    return {"projects": projects_data}


@router_geo.get("/projects/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """Get project details with scan history."""
    result = await db.execute(select(CitationTracker).where(CitationTracker.id == project_id))
    tracker = result.scalar_one_or_none()
    if not tracker:
        raise_not_found("Project")

    scan_result = await db.execute(
        select(CitationScan).where(CitationScan.tracker_id == project_id).order_by(desc(CitationScan.created_at))
    )
    scans = scan_result.scalars().all()
    return {"project": _as_project_dict(tracker), "scans": [_as_scan_dict(s) for s in scans]}


@router_geo.delete("/projects/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a GEO monitoring project and all its scans."""
    result = await db.execute(select(CitationTracker).where(CitationTracker.id == project_id))
    tracker = result.scalar_one_or_none()
    if not tracker:
        raise_not_found("Project")
    await db.delete(tracker)
    await db.commit()
    return {"message": "Project deleted successfully"}


@router_geo.patch("/projects/{project_id}")
async def update_project(project_id: str, update: UpdateProjectRequest, db: AsyncSession = Depends(get_db)):
    """Update alert settings for a GEO monitoring project."""
    result = await db.execute(select(CitationTracker).where(CitationTracker.id == project_id))
    tracker = result.scalar_one_or_none()
    if not tracker:
        raise_not_found("Project")

    if update.alert_threshold is not None:
        tracker.alert_threshold = update.alert_threshold
    if update.alert_webhook_url is not None:
        tracker.alert_webhook_url = update.alert_webhook_url
    if update.competitors is not None:
        tracker.competitors = [c.model_dump() for c in update.competitors]

    await db.commit()
    return {"message": "Project updated", "project": _as_project_dict(tracker)}


@router_geo.post("/projects/{project_id}/scan")
@limiter.limit("20/hour")
async def start_geo_scan(
    request: Request, project_id: str,
    scan_request: ScanRequest = ScanRequest(),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
):
    """Start a new visibility scan for a project."""
    result = await db.execute(select(CitationTracker).where(CitationTracker.id == project_id))
    if not result.scalar_one_or_none():
        raise_not_found("Project")

    scan_id = str(uuid.uuid4())
    scan = CitationScan(id=scan_id, tracker_id=project_id, status="pending")
    db.add(scan)
    await db.commit()

    background_tasks.add_task(_run_visibility_scan, project_id, scan_request.providers, scan_id)
    return {"scan_id": scan_id, "status": "pending", "message": "Scan started in background"}


@router_geo.get("/scans/{scan_id}")
async def get_geo_scan(scan_id: str, db: AsyncSession = Depends(get_db)):
    """Get detailed results of a scan."""
    result = await db.execute(select(CitationScan).where(CitationScan.id == scan_id))
    scan = result.scalar_one_or_none()
    if not scan:
        raise_not_found("Scan")
    return {"scan": _as_scan_dict(scan)}


@router_geo.get("/projects/{project_id}/trend")
async def get_geo_trend(project_id: str, db: AsyncSession = Depends(get_db)):
    """Get trend data for Chart.js visualization."""
    result = await db.execute(
        select(CitationScan)
        .where(CitationScan.tracker_id == project_id, CitationScan.status == "completed")
        .order_by(CitationScan.completed_at)
    )
    scans = result.scalars().all()

    labels, overall_scores = [], []
    provider_data: Dict[str, List[float]] = {}
    for scan in scans:
        if not scan.completed_at:
            continue
        labels.append(scan.completed_at.strftime("%Y-%m-%d %H:%M"))
        overall_scores.append(scan.visibility_score or 0)
        if scan.provider_breakdown:
            try:
                breakdown = json.loads(scan.provider_breakdown)
                for provider, stats in breakdown.items():
                    provider_data.setdefault(provider, []).append(stats.get("mention_rate", 0))
            except Exception as ex:
                logger.warning("Failed to parse provider_breakdown for scan %s: %s", scan.id, ex)

    datasets = [{
        "label": "Overall Visibility", "data": overall_scores,
        "borderColor": "rgb(59, 130, 246)", "backgroundColor": "rgba(59, 130, 246, 0.1)", "tension": 0.4,
    }]
    provider_colors = {"chatgpt": "rgb(16, 163, 127)", "claude": "rgb(168, 85, 247)", "perplexity": "rgb(236, 72, 153)"}
    for provider, data in provider_data.items():
        datasets.append({
            "label": provider.capitalize(), "data": data,
            "borderColor": provider_colors.get(provider, "rgb(100, 100, 100)"),
            "backgroundColor": "transparent", "tension": 0.4, "borderDash": [5, 5],
        })

    return {"labels": labels, "datasets": datasets}


# ============================================================================
# SCHEDULER INTEGRATION -- called from api/main.py's lifespan scheduler loop
# ============================================================================

def _cron_matches(cron_expr: str, now: datetime) -> bool:
    """Format: minute hour day month weekday. * = any, */n, n-m, n,m."""
    try:
        parts = cron_expr.split()
        if len(parts) != 5:
            return False
        minute, hour, day, month, weekday = parts

        def matches_field(field: str, value: int) -> bool:
            if field == "*":
                return True
            if field.startswith("*/"):
                return value % int(field[2:]) == 0
            if "-" in field:
                start, end = map(int, field.split("-"))
                return start <= value <= end
            if "," in field:
                return value in list(map(int, field.split(",")))
            return int(field) == value

        return (
            matches_field(minute, now.minute) and matches_field(hour, now.hour)
            and matches_field(day, now.day) and matches_field(month, now.month)
            and matches_field(weekday, now.weekday())
        )
    except (ValueError, IndexError):
        return False


async def check_and_run_citation_scans():
    """Check for trackers with schedules and run scans if due.

    Called by the scheduler loop in api/main.py every 5 minutes.
    """
    async with AsyncSessionLocal() as db:
        try:
            now = datetime.now(timezone.utc)
            result = await db.execute(
                select(CitationTracker).where(CitationTracker.is_active == 1)
                .where(CitationTracker.schedule_cron.isnot(None))
            )
            for tracker in result.scalars().all():
                if not _cron_matches(tracker.schedule_cron, now):
                    continue
                if tracker.last_scan_at and (now - tracker.last_scan_at).total_seconds() < 3600:
                    continue
                running_result = await db.execute(
                    select(CitationScan).where(CitationScan.tracker_id == tracker.id)
                    .where(CitationScan.status == "running")
                )
                if running_result.scalar_one_or_none():
                    continue
                logger.info("Starting scheduled visibility scan for tracker: %s", tracker.name)
                asyncio.create_task(_run_visibility_scan(tracker.id))
        except Exception as e:
            logger.error("Error in visibility scan scheduler: %s", e)
