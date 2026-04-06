"""
Citation Tracker API - Monitors URL citations across AI platforms over time.

This module tracks how often a website is CITED WITH URL (not just mentioned)
in LLM responses. It provides quantitative metrics on citation frequency and trends.
"""

import os
import re
import json
import uuid
import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from api.utils.errors import raise_not_found, raise_bad_request
from pydantic import BaseModel, field_validator
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import CitationTracker, CitationScan, AsyncSessionLocal, get_db
from api.routes.costs import track_cost

# LLM clients
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

router = APIRouter(prefix="/api/citations", tags=["citations"])

# Rate limiting semaphore
LLM_SEMAPHORE = asyncio.Semaphore(3)


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class CreateTrackerRequest(BaseModel):
    """Request to create a new citation tracker."""
    name: str
    website: str
    url_patterns: List[str]
    tracking_queries: List[str]
    providers_config: Dict[str, bool]
    schedule_cron: Optional[str] = None
    
    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v or len(v) < 3:
            raise ValueError("Name must be at least 3 characters")
        return v
    
    @field_validator("website")
    @classmethod
    def validate_website(cls, v: str) -> str:
        if not v or "." not in v:
            raise ValueError("Invalid website format")
        return v.lower()
    
    @field_validator("url_patterns")
    @classmethod
    def validate_url_patterns(cls, v: List[str]) -> List[str]:
        if not v or len(v) == 0:
            raise ValueError("At least one URL pattern required")
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
    """Request to generate query suggestions."""
    website: str
    industry: Optional[str] = None


class QuerySuggestion(BaseModel):
    """A suggested query with category."""
    query: str
    category: str  # informational, commercial, comparative


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def extract_cited_urls(response_text: str, domain: str) -> List[str]:
    """
    Extract all URLs matching the tracked domain from response.
    
    Args:
        response_text: The LLM response text
        domain: The domain to match (e.g., "ing.ro")
    
    Returns:
        List of unique URLs found
    """
    # Build flexible pattern that matches various URL formats
    domain_escaped = re.escape(domain)
    pattern = rf'https?://(?:www\.)?{domain_escaped}[^\s\)\]\"\'\<\>]*'
    
    urls = re.findall(pattern, response_text, re.IGNORECASE)
    
    # Normalize: strip trailing punctuation
    cleaned = [re.sub(r'[.,;:!?]+$', '', u) for u in urls]
    
    return list(set(cleaned))


def check_brand_mention(response_text: str, website: str) -> bool:
    """
    Check if brand/domain is mentioned (without necessarily having a URL).
    
    Args:
        response_text: The LLM response text
        website: The website domain (e.g., "ing.ro")
    
    Returns:
        True if brand is mentioned
    """
    # Extract brand name (domain without TLD)
    brand = website.split('.')[0]
    
    # Case-insensitive search
    return bool(re.search(rf'\b{re.escape(brand)}\b', response_text, re.IGNORECASE))


def extract_citation_context(response_text: str, url: str, context_chars: int = 200) -> str:
    """
    Extract context around a cited URL.
    
    Args:
        response_text: The LLM response text
        url: The URL to find context for
        context_chars: Characters to include around the URL
    
    Returns:
        Context snippet
    """
    try:
        # Find URL position
        url_escaped = re.escape(url)
        match = re.search(url_escaped, response_text, re.IGNORECASE)
        
        if not match:
            return ""
        
        start = match.start()
        
        # Get context before and after
        context_start = max(0, start - context_chars // 2)
        context_end = min(len(response_text), start + len(url) + context_chars // 2)
        
        context = response_text[context_start:context_end]
        
        # Add ellipsis if truncated
        if context_start > 0:
            context = "..." + context
        if context_end < len(response_text):
            context = context + "..."
        
        return context.strip()
    except Exception:
        return ""


async def _query_provider(
    provider: str, query: str, model: Optional[str] = None
) -> tuple[str, int, int, str]:
    """
    Query a single LLM provider.

    Args:
        provider: Provider name (chatgpt, claude, perplexity)
        query: The query to send
        model: Optional model override

    Returns:
        (response_text, input_tokens, output_tokens, model_name)
    """
    async with LLM_SEMAPHORE:
        try:
            if provider == "claude":
                api_key = os.getenv("ANTHROPIC_API_KEY")
                if not api_key:
                    return "", 0, 0, model or "claude-3-5-sonnet-20241022"

                _model = model or "claude-3-5-sonnet-20241022"
                client = AsyncAnthropic(api_key=api_key)
                response = await client.messages.create(
                    model=_model,
                    max_tokens=2000,
                    messages=[{"role": "user", "content": query}]
                )
                in_tok  = response.usage.input_tokens  if response.usage else 0
                out_tok = response.usage.output_tokens if response.usage else 0
                return response.content[0].text, in_tok, out_tok, _model

            elif provider == "chatgpt":
                api_key = os.getenv("OPENAI_API_KEY")
                if not api_key:
                    return "", 0, 0, model or "gpt-4o"

                _model = model or "gpt-4o"
                client = AsyncOpenAI(api_key=api_key)
                response = await client.chat.completions.create(
                    model=_model,
                    messages=[{"role": "user", "content": query}],
                    max_tokens=2000
                )
                in_tok  = response.usage.prompt_tokens     if response.usage else 0
                out_tok = response.usage.completion_tokens if response.usage else 0
                return response.choices[0].message.content or "", in_tok, out_tok, _model

            elif provider == "perplexity":
                api_key = os.getenv("PERPLEXITY_API_KEY")
                if not api_key:
                    return "", 0, 0, model or "llama-3.1-sonar-large-128k-online"

                _model = model or "llama-3.1-sonar-large-128k-online"
                client = AsyncOpenAI(
                    api_key=api_key,
                    base_url="https://api.perplexity.ai"
                )
                response = await client.chat.completions.create(
                    model=_model,
                    messages=[{"role": "user", "content": query}]
                )
                in_tok  = response.usage.prompt_tokens     if response.usage else 0
                out_tok = response.usage.completion_tokens if response.usage else 0
                return response.choices[0].message.content or "", in_tok, out_tok, _model

            else:
                return "", 0, 0, provider

        except Exception as e:
            print(f"Error querying {provider}: {e}")
            return "", 0, 0, model or provider


async def _run_citation_scan(tracker_id: str):
    """
    Run a complete citation scan for a tracker.
    
    This function:
    1. Loads the tracker configuration
    2. Queries each LLM provider for each tracking query
    3. Analyzes responses for citations and mentions
    4. Aggregates results and calculates metrics
    5. Stores results in the database
    """
    async with AsyncSessionLocal() as db:
        try:
            # Load tracker
            result = await db.execute(
                select(CitationTracker).where(CitationTracker.id == tracker_id)
            )
            tracker = result.scalar_one_or_none()
            
            if not tracker:
                print(f"Tracker {tracker_id} not found")
                return
            
            # Parse config
            url_patterns = json.loads(tracker.url_patterns)
            tracking_queries = json.loads(tracker.tracking_queries)
            providers_config = json.loads(tracker.providers_config)
            
            # Get enabled providers
            enabled_providers = [p for p, enabled in providers_config.items() if enabled]
            
            if not enabled_providers:
                print(f"No providers enabled for tracker {tracker_id}")
                return
            
            # Create scan record
            scan_id = str(uuid.uuid4())
            scan = CitationScan(
                id=scan_id,
                tracker_id=tracker_id,
                status="running",
                total_queries=len(tracking_queries) * len(enabled_providers),
                started_at=datetime.utcnow()
            )
            db.add(scan)
            await db.commit()
            
            # Results storage
            all_results = []
            url_citation_counts = {}  # Track which URLs are cited
            provider_stats = {p: {"citations": 0, "mentions": 0, "queries": 0, "responses": 0} for p in enabled_providers}
            
            # Run queries
            for query_idx, query in enumerate(tracking_queries):
                query_results = {
                    "query": query,
                    "query_index": query_idx + 1,
                    "providers": {}
                }
                
                for provider in enabled_providers:
                    # Add delay between same provider requests
                    await asyncio.sleep(1)
                    
                    # Query provider
                    response, in_tok, out_tok, model_name = await _query_provider(provider, query)
                    if in_tok or out_tok:
                        asyncio.create_task(track_cost(
                            source="citation_scan",
                            provider=provider,
                            model=model_name,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            source_id=scan_id,
                            website=tracker.website,
                        ))

                    if not response:
                        query_results["providers"][provider] = {
                            "cited": False,
                            "mentioned": False,
                            "cited_urls": [],
                            "error": "No response"
                        }
                        continue
                    
                    # Analyze response
                    cited_urls = []
                    for pattern in url_patterns:
                        found_urls = extract_cited_urls(response, pattern)
                        cited_urls.extend(found_urls)
                    
                    cited_urls = list(set(cited_urls))  # Deduplicate
                    
                    is_cited = len(cited_urls) > 0
                    is_mentioned = check_brand_mention(response, tracker.website)
                    
                    # Update URL citation counts
                    for url in cited_urls:
                        url_citation_counts[url] = url_citation_counts.get(url, 0) + 1
                    
                    # Get context for first cited URL
                    context = ""
                    if cited_urls:
                        context = extract_citation_context(response, cited_urls[0])
                    
                    # Store result
                    query_results["providers"][provider] = {
                        "cited": is_cited,
                        "mentioned": is_mentioned,
                        "cited_urls": cited_urls,
                        "context": context
                    }
                    
                    # Update provider stats
                    provider_stats[provider]["queries"] += 1
                    provider_stats[provider]["responses"] += 1
                    if is_cited:
                        provider_stats[provider]["citations"] += 1
                    if is_mentioned:
                        provider_stats[provider]["mentions"] += 1
                
                all_results.append(query_results)
            
            # Calculate aggregated metrics
            total_citations = sum(1 for r in all_results if any(p.get("cited", False) for p in r["providers"].values()))
            total_mentions = sum(1 for r in all_results if any(p.get("mentioned", False) for p in r["providers"].values()))
            
            # Citation rate: percentage of queries that resulted in at least one citation
            citation_rate = (total_citations / len(tracking_queries) * 100) if tracking_queries else 0
            
            # Top cited URLs
            top_cited_urls = [
                {"url": url, "count": count}
                for url, count in sorted(url_citation_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            ]
            
            # Calculate per-provider citation rates
            for provider, stats in provider_stats.items():
                if stats["queries"] > 0:
                    stats["citation_rate"] = (stats["citations"] / stats["queries"]) * 100
                    stats["mention_rate"] = (stats["mentions"] / stats["queries"]) * 100
                else:
                    stats["citation_rate"] = 0
                    stats["mention_rate"] = 0
            
            # Update scan
            scan.status = "completed"
            scan.total_citations = total_citations
            scan.total_mentions = total_mentions
            scan.citation_rate = round(citation_rate, 2)
            scan.results_json = json.dumps(all_results)
            scan.provider_breakdown = json.dumps(provider_stats)
            scan.top_cited_urls = json.dumps(top_cited_urls)
            scan.completed_at = datetime.utcnow()
            
            # Update tracker last_scan_at
            tracker.last_scan_at = datetime.utcnow()
            
            await db.commit()
            
            print(f"✓ Citation scan {scan_id} completed: {citation_rate:.1f}% citation rate")
        
        except Exception as e:
            print(f"❌ Citation scan error: {e}")
            
            # Mark scan as failed
            try:
                result = await db.execute(
                    select(CitationScan).where(CitationScan.id == scan_id)
                )
                scan = result.scalar_one_or_none()
                if scan:
                    scan.status = "failed"
                    await db.commit()
            except Exception:
                pass


def generate_citation_queries(website: str, industry: Optional[str] = None) -> List[QuerySuggestion]:
    """
    Generate suggested tracking queries for a website.
    
    Args:
        website: The website domain
        industry: Optional industry context
    
    Returns:
        List of query suggestions with categories
    """
    brand = website.split('.')[0].capitalize()
    industry_context = industry or "servicii"
    
    queries = []
    
    # Informational queries (10)
    informational = [
        f"Ce este {brand}",
        f"Cum funcționează {brand}",
        f"Ghid {brand}",
        f"Informații despre {brand}",
        f"{brand} explicat",
        f"Tutoriale {brand}",
        f"Cum să folosesc {brand}",
        f"Detalii {brand}",
        f"Overview {brand}",
        f"Introducere {brand}"
    ]
    for q in informational:
        queries.append(QuerySuggestion(query=q, category="informational"))
    
    # Commercial queries (10)
    commercial = [
        f"Cel mai bun {industry_context} România",
        f"Recomandări {industry_context}",
        f"{industry_context} review",
        f"Top {industry_context} {datetime.now().year}",
        f"{brand} review",
        f"{brand} opinie",
        f"Merită {brand}",
        f"{brand} alternative",
        f"Avantaje {brand}",
        f"De ce {brand}"
    ]
    for q in commercial:
        queries.append(QuerySuggestion(query=q, category="commercial"))
    
    # Comparative queries (10)
    comparative = [
        f"{brand} vs competitori",
        f"Alternative la {brand}",
        f"{brand} sau alternative",
        f"Comparație {industry_context} România",
        f"Top 10 {industry_context}",
        f"Cel mai bun vs {brand}",
        f"{brand} diferențe",
        f"Care este mai bun {brand}",
        f"Lista {industry_context} România",
        f"Ranking {industry_context}"
    ]
    for q in comparative:
        queries.append(QuerySuggestion(query=q, category="comparative"))
    
    return queries


# ============================================================================
# API ENDPOINTS
# ============================================================================

@router.post("/trackers")
async def create_tracker(
    request: CreateTrackerRequest,
    db: AsyncSession = Depends(get_db)
):
    """Create a new citation tracker."""
    
    # Create tracker
    tracker_id = str(uuid.uuid4())
    tracker = CitationTracker(
        id=tracker_id,
        name=request.name,
        website=request.website,
        url_patterns=json.dumps(request.url_patterns),
        tracking_queries=json.dumps(request.tracking_queries),
        providers_config=json.dumps(request.providers_config),
        schedule_cron=request.schedule_cron,
        is_active=1
    )
    
    db.add(tracker)
    await db.commit()
    
    return {
        "success": True,
        "tracker_id": tracker_id,
        "tracker": tracker.to_dict()
    }


@router.get("/trackers")
async def list_trackers(db: AsyncSession = Depends(get_db)):
    """List all citation trackers with their latest scan metrics."""
    
    result = await db.execute(
        select(CitationTracker).order_by(desc(CitationTracker.created_at))
    )
    trackers = result.scalars().all()
    
    trackers_data = []
    for tracker in trackers:
        tracker_dict = tracker.to_dict()
        
        # Get latest scan
        scan_result = await db.execute(
            select(CitationScan)
            .where(CitationScan.tracker_id == tracker.id)
            .order_by(desc(CitationScan.created_at))
            .limit(1)
        )
        latest_scan = scan_result.scalar_one_or_none()
        
        if latest_scan:
            tracker_dict["latest_citation_rate"] = latest_scan.citation_rate
            tracker_dict["latest_scan_at"] = latest_scan.created_at.isoformat()
        else:
            tracker_dict["latest_citation_rate"] = None
            tracker_dict["latest_scan_at"] = None
        
        # Count total scans
        count_result = await db.execute(
            select(func.count(CitationScan.id))
            .where(CitationScan.tracker_id == tracker.id)
        )
        scan_count = count_result.scalar()
        tracker_dict["scan_count"] = scan_count
        
        trackers_data.append(tracker_dict)
    
    return {
        "success": True,
        "trackers": trackers_data
    }


@router.get("/trackers/{tracker_id}")
async def get_tracker(tracker_id: str, db: AsyncSession = Depends(get_db)):
    """Get detailed tracker information with scan history."""
    
    result = await db.execute(
        select(CitationTracker).where(CitationTracker.id == tracker_id)
    )
    tracker = result.scalar_one_or_none()
    
    if not tracker:
        raise_not_found("Tracker")
    
    tracker_dict = tracker.to_dict()
    
    # Get scan history
    scans_result = await db.execute(
        select(CitationScan)
        .where(CitationScan.tracker_id == tracker_id)
        .order_by(desc(CitationScan.created_at))
        .limit(20)
    )
    scans = scans_result.scalars().all()
    
    tracker_dict["scans"] = [s.to_dict() for s in scans]
    
    return {
        "success": True,
        "tracker": tracker_dict
    }


@router.post("/trackers/{tracker_id}/scan")
async def start_scan(
    tracker_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Start a manual citation scan."""
    
    result = await db.execute(
        select(CitationTracker).where(CitationTracker.id == tracker_id)
    )
    tracker = result.scalar_one_or_none()
    
    if not tracker:
        raise_not_found("Tracker")
    
    # Check for running scan
    running_result = await db.execute(
        select(CitationScan)
        .where(CitationScan.tracker_id == tracker_id)
        .where(CitationScan.status == "running")
    )
    running_scan = running_result.scalar_one_or_none()
    
    if running_scan:
        raise_bad_request("Scan already in progress")
    
    # Start scan in background
    background_tasks.add_task(_run_citation_scan, tracker_id)
    
    return {
        "success": True,
        "message": "Citation scan started",
        "tracker_id": tracker_id
    }


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: str, db: AsyncSession = Depends(get_db)):
    """Get detailed scan results."""
    
    result = await db.execute(
        select(CitationScan).where(CitationScan.id == scan_id)
    )
    scan = result.scalar_one_or_none()
    
    if not scan:
        raise_not_found("Scan")
    
    return {
        "success": True,
        "scan": scan.to_dict()
    }


@router.get("/trackers/{tracker_id}/trend")
async def get_tracker_trend(tracker_id: str, db: AsyncSession = Depends(get_db)):
    """Get citation rate trend data for Chart.js."""
    
    result = await db.execute(
        select(CitationTracker).where(CitationTracker.id == tracker_id)
    )
    tracker = result.scalar_one_or_none()
    
    if not tracker:
        raise_not_found("Tracker")
    
    # Get completed scans
    scans_result = await db.execute(
        select(CitationScan)
        .where(CitationScan.tracker_id == tracker_id)
        .where(CitationScan.status == "completed")
        .order_by(CitationScan.created_at)
    )
    scans = scans_result.scalars().all()
    
    if not scans:
        return {
            "success": True,
            "chart_data": {
                "labels": [],
                "datasets": []
            }
        }
    
    # Parse provider config to know which providers to track
    providers_config = json.loads(tracker.providers_config)
    enabled_providers = [p for p, enabled in providers_config.items() if enabled]
    
    # Build chart data
    labels = []
    overall_data = []
    provider_data = {p: [] for p in enabled_providers}
    
    for scan in scans:
        # Format date label
        date_str = scan.created_at.strftime("%Y-%m-%d %H:%M")
        labels.append(date_str)
        
        # Overall citation rate
        overall_data.append(scan.citation_rate or 0)
        
        # Per-provider citation rates
        if scan.provider_breakdown:
            breakdown = json.loads(scan.provider_breakdown)
            for provider in enabled_providers:
                rate = breakdown.get(provider, {}).get("citation_rate", 0)
                provider_data[provider].append(rate)
        else:
            for provider in enabled_providers:
                provider_data[provider].append(0)
    
    # Build datasets
    datasets = [
        {
            "label": "Overall Citation Rate",
            "data": overall_data,
            "borderColor": "rgb(59, 130, 246)",
            "backgroundColor": "rgba(59, 130, 246, 0.1)",
            "tension": 0.3
        }
    ]
    
    # Provider colors
    provider_colors = {
        "chatgpt": "rgb(16, 163, 127)",
        "claude": "rgb(168, 85, 247)",
        "perplexity": "rgb(245, 158, 11)"
    }
    
    for provider in enabled_providers:
        color = provider_colors.get(provider, "rgb(107, 114, 128)")
        datasets.append({
            "label": provider.capitalize(),
            "data": provider_data[provider],
            "borderColor": color,
            "backgroundColor": color.replace("rgb", "rgba").replace(")", ", 0.1)"),
            "tension": 0.3
        })
    
    return {
        "success": True,
        "chart_data": {
            "labels": labels,
            "datasets": datasets
        }
    }


@router.delete("/trackers/{tracker_id}")
async def delete_tracker(tracker_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a citation tracker and all its scans."""
    
    result = await db.execute(
        select(CitationTracker).where(CitationTracker.id == tracker_id)
    )
    tracker = result.scalar_one_or_none()
    
    if not tracker:
        raise_not_found("Tracker")
    
    await db.delete(tracker)
    await db.commit()
    
    return {
        "success": True,
        "message": "Tracker deleted"
    }


@router.post("/generate-queries")
async def generate_queries(request: GenerateQueriesRequest):
    """Generate suggested tracking queries for a website."""
    
    suggestions = generate_citation_queries(request.website, request.industry)
    
    return {
        "success": True,
        "suggestions": [{"query": s.query, "category": s.category} for s in suggestions]
    }


@router.patch("/trackers/{tracker_id}/toggle")
async def toggle_tracker(tracker_id: str, db: AsyncSession = Depends(get_db)):
    """Toggle tracker active status."""
    
    result = await db.execute(
        select(CitationTracker).where(CitationTracker.id == tracker_id)
    )
    tracker = result.scalar_one_or_none()
    
    if not tracker:
        raise_not_found("Tracker")
    
    tracker.is_active = 1 if tracker.is_active == 0 else 0
    await db.commit()
    
    return {
        "success": True,
        "is_active": bool(tracker.is_active),
        "message": "Tracker activated" if tracker.is_active else "Tracker paused"
    }


# ============================================================================
# SCHEDULER INTEGRATION
# ============================================================================

def _cron_matches(cron_expr: str, now: datetime) -> bool:
    """
    Check if a cron expression matches the current time.
    
    Format: minute hour day month weekday
    * = any, */n = every n, n-m = range, n,m = list
    
    Args:
        cron_expr: Cron expression string
        now: Current datetime
    
    Returns:
        True if expression matches current time
    """
    try:
        parts = cron_expr.split()
        if len(parts) != 5:
            return False
        
        minute, hour, day, month, weekday = parts
        
        # Helper to parse cron field
        def matches_field(field: str, value: int, min_val: int, max_val: int) -> bool:
            if field == "*":
                return True
            
            # Step values (e.g., */5)
            if field.startswith("*/"):
                step = int(field[2:])
                return value % step == 0
            
            # Range (e.g., 1-5)
            if "-" in field:
                start, end = map(int, field.split("-"))
                return start <= value <= end
            
            # List (e.g., 1,3,5)
            if "," in field:
                values = list(map(int, field.split(",")))
                return value in values
            
            # Single value
            return int(field) == value
        
        # Check each field
        if not matches_field(minute, now.minute, 0, 59):
            return False
        if not matches_field(hour, now.hour, 0, 23):
            return False
        if not matches_field(day, now.day, 1, 31):
            return False
        if not matches_field(month, now.month, 1, 12):
            return False
        if not matches_field(weekday, now.weekday(), 0, 6):
            return False
        
        return True
    
    except (ValueError, IndexError):
        return False


async def check_and_run_citation_scans():
    """
    Check for citation trackers with schedules and run scans if due.
    
    This function is called by the scheduler loop in main.py every 5 minutes.
    It checks all active trackers with schedule_cron set and runs scans if the
    cron expression matches the current time.
    """
    async with AsyncSessionLocal() as db:
        try:
            now = datetime.utcnow()
            
            # Get all active trackers with schedules
            result = await db.execute(
                select(CitationTracker)
                .where(CitationTracker.is_active == 1)
                .where(CitationTracker.schedule_cron.isnot(None))
            )
            trackers = result.scalars().all()
            
            for tracker in trackers:
                # Check if cron matches
                if not _cron_matches(tracker.schedule_cron, now):
                    continue
                
                # Check if already ran in the last hour (prevent duplicates)
                if tracker.last_scan_at:
                    time_since_last = (now - tracker.last_scan_at).total_seconds()
                    if time_since_last < 3600:  # Less than 1 hour
                        continue
                
                # Check for running scan
                running_result = await db.execute(
                    select(CitationScan)
                    .where(CitationScan.tracker_id == tracker.id)
                    .where(CitationScan.status == "running")
                )
                running_scan = running_result.scalar_one_or_none()
                
                if running_scan:
                    continue
                
                # Start scan
                print(f"📊 Starting scheduled citation scan for tracker: {tracker.name}")
                asyncio.create_task(_run_citation_scan(tracker.id))
        
        except Exception as e:
            print(f"❌ Error in citation scan scheduler: {e}")
