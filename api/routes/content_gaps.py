"""
Content Gap Finder - Identifies missing content opportunities.

Discovers what content should be created on the website based on:
1. GEO Monitor queries where client is not mentioned
2. Citation Tracker queries where client URLs are not cited
3. Competitor pages that client doesn't have
4. LLM analysis of content coverage vs industry best practices

Output: Prioritized list of content gaps with creation briefs.
"""

import asyncio
import json
import uuid
import re
from datetime import datetime
from typing import Optional, List, Dict, Any
from difflib import SequenceMatcher

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from api.utils.errors import raise_not_found
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import (
    AsyncSessionLocal,
    Audit,
    AuditResult,
    ContentGap,
    GeoMonitorScan,
    CitationScan,
)
from api.provider_registry import get_default_model, calculate_cost
from api.routes.summary import call_llm_for_summary, clean_json_response

router = APIRouter(prefix="/api/content-gaps", tags=["content_gaps"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class AnalyzeSourcesRequest(BaseModel):
    """Request to analyze content gaps."""
    website: str = Field(..., description="Target website domain")
    sources: Dict[str, Any] = Field(
        default={},
        description="Gap sources: geo_monitor_project_id, citation_tracker_id, competitor_audit_ids, manual_queries"
    )
    language: str = Field("Romanian", description="Content language")
    industry_hint: Optional[str] = Field(None, description="Industry context for better briefs")
    provider: str = Field("anthropic", description="LLM provider for brief generation")
    model: Optional[str] = Field(None, description="LLM model (uses provider default if not set)")
    max_gaps: int = Field(20, ge=1, le=50, description="Maximum gaps to identify (1-50)")


class UpdateGapStatusRequest(BaseModel):
    """Request to update content gap status."""
    status: str = Field(..., description="New status: identified, approved, in_progress, published, dismissed")


# ============================================================================
# HELPER FUNCTIONS - RULES-BASED GAP IDENTIFICATION
# ============================================================================

def normalize_topic(topic: str) -> str:
    """Normalize topic string for comparison."""
    # Lowercase, remove special chars, collapse whitespace
    normalized = topic.lower()
    normalized = re.sub(r'[^\w\s]', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def topics_similar(topic1: str, topic2: str, threshold: float = 0.75) -> bool:
    """Check if two topics are similar enough to be considered duplicates."""
    norm1 = normalize_topic(topic1)
    norm2 = normalize_topic(topic2)
    
    # Exact match
    if norm1 == norm2:
        return True
    
    # Fuzzy match using sequence matcher
    similarity = SequenceMatcher(None, norm1, norm2).ratio()
    return similarity >= threshold


def deduplicate_gaps(gaps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate similar gaps and merge their sources.
    
    Returns:
        Deduplicated list with merged confidence scores
    """
    if not gaps:
        return []
    
    deduplicated = []
    
    for gap in gaps:
        # Check if similar gap already exists
        found_similar = False
        for existing in deduplicated:
            if topics_similar(gap["topic"], existing["topic"]):
                # Merge sources
                existing["sources"].extend(gap["sources"])
                # Boost confidence if multiple sources agree
                existing["confidence"] = min(1.0, existing["confidence"] + 0.1)
                found_similar = True
                break
        
        if not found_similar:
            # New unique gap
            gap["sources"] = [gap["source"]]  # Convert to list
            deduplicated.append(gap)
    
    return deduplicated


def prioritize_gaps(gaps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Calculate priority scores and sort gaps.
    
    Priority score formula:
    - confidence (0-1) × 40
    - source_count × 15
    - source_type_bonus: +20 for geo_monitor, +15 for citation_tracker, +10 for competitor, +5 for manual
    
    Returns:
        Sorted list with priority_score and priority label
    """
    for gap in gaps:
        source_count = len(gap["sources"])
        confidence = gap["confidence"]
        
        # Base score from confidence and source count
        score = (confidence * 40) + (min(source_count, 3) * 15)
        
        # Bonus based on source types
        source_types = set(gap["sources"])
        if "geo_monitor" in source_types:
            score += 20  # AI not mentioning you = critical
        if "citation_tracker" in source_types:
            score += 15  # No citations = high priority
        if "competitor" in source_types:
            score += 10  # Competitor has it = important
        if "manual" in source_types:
            score += 5   # User knows it's needed
        
        gap["priority_score"] = min(100.0, score)
        
        # Assign priority label
        if gap["priority_score"] >= 80:
            gap["priority"] = "critical"
        elif gap["priority_score"] >= 60:
            gap["priority"] = "high"
        elif gap["priority_score"] >= 40:
            gap["priority"] = "medium"
        else:
            gap["priority"] = "low"
    
    # Sort by score descending
    gaps.sort(key=lambda x: x["priority_score"], reverse=True)
    
    return gaps


def extract_topic_from_url(url: str) -> str:
    """Extract topic/subject from a URL path."""
    # Remove domain and protocol
    path = re.sub(r'^https?://[^/]+', '', url)
    # Remove file extensions
    path = re.sub(r'\.(html|php|aspx?)$', '', path)
    # Extract last meaningful segment
    segments = [s for s in path.split('/') if s and s not in ['index', 'default']]
    
    if not segments:
        return "homepage"
    
    # Take last segment and clean it
    topic = segments[-1]
    # Convert hyphens/underscores to spaces
    topic = re.sub(r'[-_]', ' ', topic)
    # Remove numbers at start
    topic = re.sub(r'^\d+\s*', '', topic)
    
    return topic.strip()


async def collect_gap_signals(
    website: str,
    sources: Dict[str, Any],
    db: AsyncSession
) -> List[Dict[str, Any]]:
    """
    Collect gap signals from all configured sources (rules-based, zero LLM cost).
    
    Returns:
        List of gap signals with metadata
    """
    gaps = []
    
    # Source 1: Manual queries (user-provided topics they know they need)
    manual_queries = sources.get("manual_queries", [])
    if manual_queries:
        if isinstance(manual_queries, str):
            # Split by newlines if provided as text
            manual_queries = [q.strip() for q in manual_queries.split('\n') if q.strip()]
        
        for query in manual_queries:
            gaps.append({
                "topic": query,
                "source": "manual",
                "detail": f"User-identified content need: '{query}'",
                "confidence": 0.95  # High confidence - user knows their business
            })
    
    # Source 2: GEO Monitor - queries where client is NOT mentioned by any provider
    geo_project_id = sources.get("geo_monitor_project_id")
    if geo_project_id:
        scan_row = await db.execute(
            select(GeoMonitorScan)
            .where(
                GeoMonitorScan.project_id == geo_project_id,
                GeoMonitorScan.status == "completed",
            )
            .order_by(GeoMonitorScan.created_at.desc())
            .limit(1)
        )
        latest_geo_scan = scan_row.scalar_one_or_none()
        if latest_geo_scan and latest_geo_scan.results_json:
            # results_json is a flat list: [{query, provider, mentioned, cited, ...}, ...]
            # Group by query; a gap = query where NO provider mentioned the site
            try:
                scan_data = json.loads(latest_geo_scan.results_json)
            except json.JSONDecodeError as _je:
                print(f"[content_gaps] Warning: malformed JSON in GEO scan {latest_geo_scan.id} "
                      f"(project {geo_project_id}): {_je} — skipping GEO Monitor gaps")
                scan_data = []
            query_mentioned: Dict[str, bool] = {}
            for item in scan_data:
                q = item.get("query", "").strip()
                if not q:
                    continue
                if q not in query_mentioned:
                    query_mentioned[q] = False
                if item.get("mentioned", False):
                    query_mentioned[q] = True
            for query, mentioned in query_mentioned.items():
                if not mentioned:
                    gaps.append({
                        "topic": query,
                        "source": "geo_monitor",
                        "detail": f"AI does not mention {website} when asked: '{query}'",
                        "confidence": 0.9,
                    })
    
    # Source 3: Citation Tracker - queries where the URL was NOT cited by any provider
    citation_tracker_id = sources.get("citation_tracker_id")
    if citation_tracker_id:
        cite_row = await db.execute(
            select(CitationScan)
            .where(
                CitationScan.tracker_id == citation_tracker_id,
                CitationScan.status == "completed",
            )
            .order_by(CitationScan.created_at.desc())
            .limit(1)
        )
        latest_cite_scan = cite_row.scalar_one_or_none()
        if latest_cite_scan and latest_cite_scan.results_json:
            # results_json structure: [{query, query_index, providers: {name: {cited, mentioned, ...}}}]
            # A gap = query where NO provider returned cited=True
            try:
                scan_data = json.loads(latest_cite_scan.results_json)
            except json.JSONDecodeError as _je:
                print(f"[content_gaps] Warning: malformed JSON in citation scan {latest_cite_scan.id} "
                      f"(tracker {citation_tracker_id}): {_je} — skipping Citation Tracker gaps")
                scan_data = []
            for item in scan_data:
                query = item.get("query", "").strip()
                if not query:
                    continue
                providers_data = item.get("providers", {})
                any_cited = any(
                    p.get("cited", False) for p in providers_data.values()
                )
                if not any_cited:
                    gaps.append({
                        "topic": query,
                        "source": "citation_tracker",
                        "detail": f"URL never cited for: '{query}'",
                        "confidence": 0.85,
                    })
    
    # Source 4: Competitor pages not matched by client
    competitor_audit_ids = sources.get("competitor_audit_ids", [])
    if competitor_audit_ids:
        # Load client audit to get their page URLs
        client_results = await db.execute(
            select(AuditResult)
            .join(Audit)
            .where(
                and_(
                    Audit.website == website,
                    Audit.status == "completed"
                )
            )
            .order_by(Audit.created_at.desc())
        )
        client_pages = client_results.scalars().all()
        client_topics = set([extract_topic_from_url(p.page_url) for p in client_pages])
        
        # Load competitor pages
        for comp_audit_id in competitor_audit_ids:
            comp_results = await db.execute(
                select(AuditResult)
                .where(AuditResult.audit_id == comp_audit_id)
            )
            comp_pages = comp_results.scalars().all()
            
            for comp_page in comp_pages:
                # Only consider high-scoring competitor pages
                if comp_page.score and comp_page.score >= 70:
                    comp_topic = extract_topic_from_url(comp_page.page_url)
                    
                    # Check if client has similar topic
                    has_equivalent = any(
                        topics_similar(comp_topic, client_topic, threshold=0.7)
                        for client_topic in client_topics
                    )
                    
                    if not has_equivalent and comp_topic != "homepage":
                        # Get competitor domain for detail
                        comp_audit = await db.execute(
                            select(Audit).where(Audit.id == comp_audit_id)
                        )
                        comp = comp_audit.scalar_one_or_none()
                        comp_domain = comp.website if comp else "competitor"
                        
                        gaps.append({
                            "topic": comp_topic,
                            "source": "competitor",
                            "detail": f"{comp_domain} has '{comp_page.page_url}' (score: {comp_page.score}) — {website} has no equivalent",
                            "confidence": 0.7
                        })
    
    return gaps


async def generate_briefs_batch(
    gaps: List[Dict[str, Any]],
    website: str,
    language: str,
    industry_hint: Optional[str],
    provider: str,
    model: str
) -> List[Dict[str, Any]]:
    """
    Generate content briefs for gaps using LLM (batched for efficiency).
    
    Args:
        gaps: List of gap dictionaries
        website: Target website
        language: Content language
        industry_hint: Industry context
        provider: LLM provider
        model: LLM model
    
    Returns:
        Gaps with added brief data
    """
    if not gaps:
        return gaps
    
    # Process in batches of 5 to stay within token limits
    batch_size = 5
    enriched_gaps = []
    
    for i in range(0, len(gaps), batch_size):
        batch = gaps[i:i + batch_size]
        
        # Build prompt for this batch
        gaps_text = "\n\n".join([
            f"Gap {idx + 1}:\n"
            f"- Topic: {gap['topic']}\n"
            f"- Source: {gap['source']}\n"
            f"- Detail: {gap['detail']}\n"
            f"- Priority: {gap['priority']}"
            for idx, gap in enumerate(batch)
        ])
        
        industry_context = f" in the {industry_hint} industry" if industry_hint else ""
        
        system_prompt = f"""You are a content strategist for {website}{industry_context}.

Your task is to create content creation briefs for identified content gaps.
For each gap, generate a comprehensive brief that will help content creators write the page.

Return ONLY a JSON array with one object per gap. Each object must have these fields:

- suggested_title: SEO-optimized, compelling title that matches user intent
- suggested_url_slug: Clean URL path (e.g., "/comparatie-credite-ipotecare")
- content_type: one of "article", "faq_page", "landing_page", "comparison_page", "guide", "calculator", "tool", "listicle"
- target_keywords: Array of 3-5 target keywords for SEO/GEO
- outline: Array of 4-6 main sections, each with "heading" and "description" fields
- geo_optimization: Object with fields:
  - entities_to_mention: Key entities/brands to reference
  - questions_to_answer: Specific questions to address
  - citation_opportunities: Where to cite authoritative sources
- estimated_word_count: Recommended content length (number)
- estimated_effort: Time estimate (e.g., "2-3 hours", "1 day", "3-4 days")

Language: {language}

IMPORTANT: Return ONLY the JSON array, no markdown code blocks, no explanation."""

        user_prompt = f"""Create content briefs for these {len(batch)} content gaps:

{gaps_text}

Return JSON array with {len(batch)} brief objects."""

        try:
            # Call LLM
            response = await call_llm_for_summary(
                content=user_prompt,
                system_prompt=system_prompt,
                provider=provider,
                model=model
            )
            
            # Parse response
            cleaned = clean_json_response(response)
            briefs = json.loads(cleaned)
            
            if not isinstance(briefs, list):
                # Fallback if single object returned
                briefs = [briefs]
            
            # Match briefs to gaps
            for gap, brief in zip(batch, briefs):
                gap["suggested_title"] = brief.get("suggested_title", "")
                gap["suggested_url_slug"] = brief.get("suggested_url_slug", "")
                gap["content_type"] = brief.get("content_type", "article")
                gap["target_keywords"] = brief.get("target_keywords", [])
                gap["brief_json"] = brief  # Store full brief
                gap["estimated_word_count"] = brief.get("estimated_word_count")
                gap["estimated_effort"] = brief.get("estimated_effort", "1-2 days")
                
                enriched_gaps.append(gap)
        
        except Exception as e:
            print(f"Error generating briefs for batch: {e}")
            # Add gaps without briefs
            for gap in batch:
                gap["suggested_title"] = gap["topic"]
                gap["suggested_url_slug"] = "/" + re.sub(r'[^\w]+', '-', gap["topic"].lower()).strip('-')
                gap["content_type"] = "article"
                gap["target_keywords"] = []
                gap["brief_json"] = None
                gap["estimated_word_count"] = 1500
                gap["estimated_effort"] = "1-2 days"
                enriched_gaps.append(gap)
    
    return enriched_gaps


# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def analyze_content_gaps_task(
    analysis_id: str,
    website: str,
    sources: Dict[str, Any],
    language: str,
    industry_hint: Optional[str],
    provider: str,
    model: str,
    max_gaps: int
):
    """
    Background task to analyze content gaps and generate briefs.
    
    This is a multi-step process:
    1. Collect gap signals (rules-based, zero LLM cost)
    2. Deduplicate and prioritize
    3. Generate briefs (LLM, batched)
    4. Save to database
    """
    async with AsyncSessionLocal() as db:
        try:
            print(f"[Content Gap Analysis {analysis_id}] Starting...")
            
            # Step 1: Collect gap signals
            print(f"[Content Gap Analysis {analysis_id}] Collecting gap signals...")
            raw_gaps = await collect_gap_signals(website, sources, db)
            print(f"[Content Gap Analysis {analysis_id}] Found {len(raw_gaps)} raw gap signals")
            
            if not raw_gaps:
                print(f"[Content Gap Analysis {analysis_id}] No gaps found")
                return
            
            # Step 2: Deduplicate and prioritize
            print(f"[Content Gap Analysis {analysis_id}] Deduplicating...")
            unique_gaps = deduplicate_gaps(raw_gaps)
            print(f"[Content Gap Analysis {analysis_id}] {len(unique_gaps)} unique gaps after deduplication")
            
            print(f"[Content Gap Analysis {analysis_id}] Prioritizing...")
            prioritized_gaps = prioritize_gaps(unique_gaps)
            
            # Take top N
            top_gaps = prioritized_gaps[:max_gaps]
            print(f"[Content Gap Analysis {analysis_id}] Top {len(top_gaps)} gaps selected for brief generation")
            
            # Step 3: Generate briefs with LLM
            print(f"[Content Gap Analysis {analysis_id}] Generating content briefs...")
            enriched_gaps = await generate_briefs_batch(
                gaps=top_gaps,
                website=website,
                language=language,
                industry_hint=industry_hint,
                provider=provider,
                model=model
            )
            
            # Step 4: Save to database
            print(f"[Content Gap Analysis {analysis_id}] Saving {len(enriched_gaps)} gaps to database...")
            for gap in enriched_gaps:
                # Prepare source detail JSON
                source_detail = {
                    "sources": gap["sources"],
                    "detail": gap["detail"],
                    "confidence": gap["confidence"]
                }
                
                new_gap = ContentGap(
                    analysis_id=analysis_id,
                    website=website,
                    topic=gap["topic"],
                    gap_source=gap["source"],  # Primary source
                    source_detail=json.dumps(source_detail),
                    priority=gap["priority"],
                    priority_score=gap["priority_score"],
                    suggested_title=gap.get("suggested_title"),
                    suggested_url_slug=gap.get("suggested_url_slug"),
                    content_type=gap.get("content_type"),
                    target_keywords=json.dumps(gap.get("target_keywords", [])),
                    brief_json=json.dumps(gap.get("brief_json")) if gap.get("brief_json") else None,
                    estimated_word_count=gap.get("estimated_word_count"),
                    estimated_effort=gap.get("estimated_effort"),
                    status="identified",
                    provider=provider,
                    model=model,
                    created_at=datetime.utcnow()
                )
                
                db.add(new_gap)
            
            await db.commit()
            print(f"[Content Gap Analysis {analysis_id}] Completed successfully!")
            
        except Exception as e:
            print(f"[Content Gap Analysis {analysis_id}] Error: {e}")
            import traceback
            traceback.print_exc()


async def generate_full_brief_task(gap_id: int, provider: str, model: str):
    """
    Generate a more detailed content brief for a specific gap.
    
    This is an enhanced version with more detail than the initial batch brief.
    """
    async with AsyncSessionLocal() as db:
        try:
            # Load gap
            result = await db.execute(
                select(ContentGap).where(ContentGap.id == gap_id)
            )
            gap = result.scalar_one_or_none()
            
            if not gap:
                print(f"Gap {gap_id} not found")
                return
            
            print(f"[Full Brief Generation] Starting for gap {gap_id}: {gap.topic}")
            
            # Build enhanced prompt
            system_prompt = f"""You are a senior content strategist creating a comprehensive content brief.

Generate a detailed content creation brief that includes:

1. **Content Strategy:**
   - Primary objective of this content
   - Target audience persona
   - User intent and journey stage
   - Success metrics

2. **SEO/GEO Optimization:**
   - Primary and secondary keywords
   - Entities to mention (people, places, organizations)
   - Semantic keywords and related terms
   - Questions to answer (People Also Ask)
   - Internal linking opportunities

3. **Content Structure:**
   - Detailed outline with 6-10 sections
   - Each section should have: heading, description, key points to cover, word count target
   - Recommended H2/H3 structure

4. **AI Search Optimization (GEO):**
   - How to structure content for AI citation
   - Authoritative sources to reference
   - Data points and statistics to include
   - Entities and attributions to use

5. **Production Details:**
   - Estimated word count (with range)
   - Content type and format
   - Visual elements needed (images, charts, videos)
   - Expert quotes or interviews needed
   - Time and effort estimate

Return a comprehensive JSON object with these sections.
Be specific and actionable."""

            user_prompt = f"""Create a comprehensive content brief for this topic:

Topic: {gap.topic}
Website: {gap.website}
Context: {gap.source_detail}

Current brief summary (if any): {gap.suggested_title or 'None'}

Generate the complete, detailed brief."""

            # Call LLM
            response = await call_llm_for_summary(
                content=user_prompt,
                system_prompt=system_prompt,
                provider=provider,
                model=model
            )
            
            # Parse and save
            cleaned = clean_json_response(response)
            full_brief = json.loads(cleaned)
            
            # Update gap with full brief
            gap.brief_json = json.dumps(full_brief)
            gap.provider = provider
            gap.model = model
            gap.updated_at = datetime.utcnow()
            
            await db.commit()
            print(f"[Full Brief Generation] Completed for gap {gap_id}")
            
        except Exception as e:
            print(f"[Full Brief Generation] Error for gap {gap_id}: {e}")
            import traceback
            traceback.print_exc()


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/analyze")
async def analyze_content_gaps(
    request: AnalyzeSourcesRequest,
    background_tasks: BackgroundTasks
):
    """
    Analyze content gaps and generate creation briefs.
    
    This endpoint:
    1. Collects gap signals from configured sources (rules-based)
    2. Deduplicates and prioritizes gaps
    3. Generates content briefs using LLM (batched)
    4. Returns immediately and runs in background
    
    Returns:
        {
            "analysis_id": "uuid",
            "status": "analyzing",
            "message": "..."
        }
    """
    # Generate analysis ID
    analysis_id = str(uuid.uuid4())
    
    # Get model
    model = request.model or get_default_model(request.provider)
    
    # Schedule background task
    background_tasks.add_task(
        analyze_content_gaps_task,
        analysis_id=analysis_id,
        website=request.website,
        sources=request.sources,
        language=request.language,
        industry_hint=request.industry_hint,
        provider=request.provider,
        model=model,
        max_gaps=request.max_gaps
    )
    
    return {
        "analysis_id": analysis_id,
        "status": "analyzing",
        "message": f"Content gap analysis started for {request.website}. Check GET /api/content-gaps?analysis_id={analysis_id} for results."
    }


@router.get("")
async def list_content_gaps(
    website: Optional[str] = Query(None, description="Filter by website"),
    analysis_id: Optional[str] = Query(None, description="Filter by analysis ID"),
    status: Optional[str] = Query(None, description="Filter by status"),
    priority: Optional[str] = Query(None, description="Filter by priority"),
    limit: int = Query(100, ge=1, le=500, description="Max results to return")
):
    """
    List content gaps with optional filters.
    
    Returns gaps sorted by priority_score descending.
    """
    async with AsyncSessionLocal() as db:
        # Build query
        query = select(ContentGap).order_by(ContentGap.priority_score.desc())
        
        # Apply filters
        if website:
            query = query.where(ContentGap.website == website)
        
        if analysis_id:
            query = query.where(ContentGap.analysis_id == analysis_id)
        
        if status:
            query = query.where(ContentGap.status == status)
        
        if priority:
            query = query.where(ContentGap.priority == priority)
        
        query = query.limit(limit)
        
        # Execute
        result = await db.execute(query)
        gaps = result.scalars().all()
        
        return {
            "total": len(gaps),
            "gaps": [gap.to_dict() for gap in gaps]
        }


@router.get("/stats")
async def get_content_gap_stats(
    website: Optional[str] = Query(None, description="Filter by website"),
    analysis_id: Optional[str] = Query(None, description="Filter by analysis ID")
):
    """
    Get aggregate statistics about content gaps.
    
    Returns:
        - Total gaps
        - Breakdown by priority
        - Breakdown by status
        - Breakdown by source
        - Breakdown by content type
    """
    async with AsyncSessionLocal() as db:
        # Build base query
        base_query = select(ContentGap)
        
        if website:
            base_query = base_query.where(ContentGap.website == website)
        
        if analysis_id:
            base_query = base_query.where(ContentGap.analysis_id == analysis_id)
        
        # Get all gaps
        result = await db.execute(base_query)
        gaps = result.scalars().all()
        
        # Calculate stats
        total = len(gaps)
        
        priority_breakdown = {}
        status_breakdown = {}
        source_breakdown = {}
        content_type_breakdown = {}
        
        for gap in gaps:
            # Priority
            priority_breakdown[gap.priority] = priority_breakdown.get(gap.priority, 0) + 1
            
            # Status
            status_breakdown[gap.status] = status_breakdown.get(gap.status, 0) + 1
            
            # Source
            source_breakdown[gap.gap_source] = source_breakdown.get(gap.gap_source, 0) + 1
            
            # Content type
            if gap.content_type:
                content_type_breakdown[gap.content_type] = content_type_breakdown.get(gap.content_type, 0) + 1
        
        return {
            "total_gaps": total,
            "by_priority": priority_breakdown,
            "by_status": status_breakdown,
            "by_source": source_breakdown,
            "by_content_type": content_type_breakdown
        }


@router.get("/{gap_id}")
async def get_content_gap(gap_id: int):
    """
    Get detailed content gap with full brief.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ContentGap).where(ContentGap.id == gap_id)
        )
        gap = result.scalar_one_or_none()
        
        if not gap:
            raise_not_found("Content gap")
        
        return gap.to_dict()


@router.patch("/{gap_id}")
async def update_content_gap(gap_id: int, request: UpdateGapStatusRequest):
    """
    Update content gap status.
    
    Status flow: identified → approved → in_progress → published (or dismissed)
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ContentGap).where(ContentGap.id == gap_id)
        )
        gap = result.scalar_one_or_none()
        
        if not gap:
            raise_not_found("Content gap")
        
        # Validate status
        valid_statuses = ["identified", "approved", "in_progress", "published", "dismissed"]
        if request.status not in valid_statuses:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
            )
        
        # Update
        gap.status = request.status
        gap.updated_at = datetime.utcnow()
        
        await db.commit()
        
        return gap.to_dict()


@router.post("/{gap_id}/generate-full-brief")
async def generate_full_brief(
    gap_id: int,
    background_tasks: BackgroundTasks,
    provider: str = Query("anthropic", description="LLM provider"),
    model: Optional[str] = Query(None, description="LLM model")
):
    """
    Generate a more detailed content brief for a specific gap.
    
    This creates an enhanced brief with more detail than the initial batch brief.
    Runs in background and updates the gap's brief_json field.
    """
    async with AsyncSessionLocal() as db:
        # Verify gap exists
        result = await db.execute(
            select(ContentGap).where(ContentGap.id == gap_id)
        )
        gap = result.scalar_one_or_none()
        
        if not gap:
            raise_not_found("Content gap")
    
    # Get model
    model_to_use = model or get_default_model(provider)
    
    # Schedule background task
    background_tasks.add_task(
        generate_full_brief_task,
        gap_id=gap_id,
        provider=provider,
        model=model_to_use
    )
    
    return {
        "status": "generating",
        "message": f"Full brief generation started for gap {gap_id}. Check GET /api/content-gaps/{gap_id} for updated brief."
    }


@router.get("/export/{analysis_id}")
async def export_content_gaps(
    analysis_id: str,
    format: str = Query("json", description="Export format: json or csv")
):
    """
    Export all content gaps from an analysis as JSON or CSV.
    
    CSV columns: Priority | Topic | Content Type | Title | URL Slug | Keywords | Effort | Status
    """
    async with AsyncSessionLocal() as db:
        # Get gaps
        result = await db.execute(
            select(ContentGap)
            .where(ContentGap.analysis_id == analysis_id)
            .order_by(ContentGap.priority_score.desc())
        )
        gaps = result.scalars().all()
        
        if not gaps:
            raise HTTPException(status_code=404, detail="No gaps found for this analysis ID")
        
        if format == "csv":
            # Generate CSV
            import csv
            import io
            
            output = io.StringIO()
            writer = csv.writer(output)
            
            # Header
            writer.writerow([
                "Priority",
                "Priority Score",
                "Topic",
                "Content Type",
                "Suggested Title",
                "URL Slug",
                "Keywords",
                "Estimated Words",
                "Estimated Effort",
                "Status",
                "Source"
            ])
            
            # Rows
            for gap in gaps:
                keywords = ", ".join(json.loads(gap.target_keywords)) if gap.target_keywords else ""
                
                writer.writerow([
                    gap.priority,
                    gap.priority_score,
                    gap.topic,
                    gap.content_type or "",
                    gap.suggested_title or "",
                    gap.suggested_url_slug or "",
                    keywords,
                    gap.estimated_word_count or "",
                    gap.estimated_effort or "",
                    gap.status,
                    gap.gap_source
                ])
            
            # Return as downloadable file
            output.seek(0)
            return StreamingResponse(
                iter([output.getvalue()]),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename=content_gaps_{analysis_id}.csv"
                }
            )
        
        else:
            # JSON export
            export_data = {
                "analysis_id": analysis_id,
                "website": gaps[0].website,
                "total_gaps": len(gaps),
                "exported_at": datetime.utcnow().isoformat(),
                "gaps": [gap.to_dict() for gap in gaps]
            }
            
            return JSONResponse(content=export_data)


@router.delete("/{gap_id}")
async def delete_content_gap(gap_id: int):
    """
    Delete a content gap.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ContentGap).where(ContentGap.id == gap_id)
        )
        gap = result.scalar_one_or_none()
        
        if not gap:
            raise_not_found("Content gap")
        
        await db.delete(gap)
        await db.commit()
        
        return {"status": "deleted", "id": gap_id}


@router.delete("/analysis/{analysis_id}")
async def delete_analysis(analysis_id: str):
    """
    Delete all gaps from a specific analysis.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ContentGap).where(ContentGap.analysis_id == analysis_id)
        )
        gaps = result.scalars().all()
        
        if not gaps:
            raise HTTPException(status_code=404, detail="No gaps found for this analysis ID")
        
        for gap in gaps:
            await db.delete(gap)
        
        await db.commit()
        
        return {
            "status": "deleted",
            "analysis_id": analysis_id,
            "gaps_deleted": len(gaps)
        }
