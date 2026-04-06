"""
GEO Visibility Monitor - Track AI visibility of websites/brands in LLM responses.

Monitors mentions and citations across ChatGPT, Claude, and Perplexity to provide
a quantitative GEO Visibility Score for consultants to justify ROI.
"""

import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from typing import List, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Depends
from api.utils.errors import raise_not_found
from pydantic import BaseModel, field_validator
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from api.models.database import AsyncSessionLocal, get_db, GeoMonitorProject, GeoMonitorScan
from api.routes.costs import track_cost

router = APIRouter(prefix="/api/geo-monitor", tags=["geo_monitor"])


# ==================== Pydantic Models ====================

class CreateProjectRequest(BaseModel):
    name: str
    website: str
    brand_keywords: List[str]
    target_queries: List[str]
    providers: Dict[str, bool]
    language: str = "English"
    
    @field_validator('brand_keywords')
    @classmethod
    def validate_keywords(cls, v):
        if not v or len(v) == 0:
            raise ValueError("At least one brand keyword is required")
        return v
    
    @field_validator('target_queries')
    @classmethod
    def validate_queries(cls, v):
        if not v or len(v) == 0:
            raise ValueError("At least one target query is required")
        return v
    
    @field_validator('providers')
    @classmethod
    def validate_providers(cls, v):
        if not any(v.values()):
            raise ValueError("At least one provider must be enabled")
        return v


class ScanRequest(BaseModel):
    providers: Optional[Dict[str, bool]] = None


# ==================== Helper Functions ====================

def generate_suggested_queries(website: str, brand: str, language: str = "English") -> List[str]:
    """Generate 10 suggested queries based on domain and language."""
    
    # Determine industry from domain (simplified)
    domain_lower = website.lower()
    
    if language.lower() in ["romanian", "română"]:
        if any(x in domain_lower for x in ["bank", "banca", "ing", "bcr", "brd"]):
            return [
                f"Care sunt cele mai bune bănci din România?",
                f"Ce bancă recomandați pentru cont curent?",
                f"{brand} recenzii și opinii",
                f"Cel mai bun credit ipotecar România",
                f"{brand} vs BCR vs BRD - comparație",
                f"Cum deschid cont bancar online România?",
                f"Ce dobândă oferă {brand} la depozit?",
                f"Aplicație banking recomandată România",
                f"Servicii bancare pentru freelanceri România",
                f"Care e cea mai sigură bancă din România?"
            ]
        elif any(x in domain_lower for x in ["shop", "magazin", "store"]):
            return [
                f"Cele mai bune magazine online România",
                f"Unde cumpăr {brand} cu livrare rapidă?",
                f"{brand} review și recenzii",
                f"Magazine online de încredere România",
                f"{brand} vs competitori - comparație",
                f"Oferte și promoții {brand}",
                f"Returnare produse la {brand}",
                f"Livrare gratuită {brand}",
                f"Cod reducere {brand}",
                f"Opinii clienți despre {brand}"
            ]
        else:
            return [
                f"Cele mai bune companii {brand} România",
                f"Recenzii {brand}",
                f"Ce servicii oferă {brand}?",
                f"{brand} prețuri și tarife",
                f"Alternative la {brand} România",
                f"Recomandări {brand}",
                f"Contact și suport {brand}",
                f"Opinii despre {brand}",
                f"Cum funcționează {brand}?",
                f"Avantaje {brand} vs competitori"
            ]
    else:
        # English suggestions
        if any(x in domain_lower for x in ["bank", "finance", "credit"]):
            return [
                f"What are the best banks for {brand}?",
                f"Is {brand} a good bank?",
                f"{brand} reviews and ratings",
                f"Best mortgage rates from {brand}",
                f"{brand} vs competitors comparison",
                f"How to open account at {brand}?",
                f"What interest rates does {brand} offer?",
                f"Best banking app recommendations",
                f"Banking services for freelancers",
                f"Most trusted banks in the region"
            ]
        elif any(x in domain_lower for x in ["shop", "store", "retail"]):
            return [
                f"Best online stores like {brand}",
                f"Where to buy from {brand}?",
                f"{brand} review and customer feedback",
                f"Trusted online retailers",
                f"{brand} vs competitors",
                f"Deals and promotions at {brand}",
                f"Return policy for {brand}",
                f"Free shipping from {brand}",
                f"Discount codes for {brand}",
                f"Customer reviews about {brand}"
            ]
        else:
            return [
                f"Best {brand} recommendations",
                f"Reviews of {brand}",
                f"What services does {brand} offer?",
                f"{brand} pricing and rates",
                f"Alternatives to {brand}",
                f"Recommendations for {brand}",
                f"Contact and support for {brand}",
                f"Opinions about {brand}",
                f"How does {brand} work?",
                f"Advantages of {brand} vs competitors"
            ]


async def _query_provider(provider: str, query: str) -> tuple[str, int, int, str]:
    """Send a conversational query to an LLM provider.

    Returns:
        (response_text, input_tokens, output_tokens, model_name)
    """
    if provider == "chatgpt":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")
        model = "gpt-4o-mini"
        client = AsyncOpenAI(api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            max_tokens=1500,
            messages=[{"role": "user", "content": query}]
        )
        in_tok  = response.usage.prompt_tokens     if response.usage else 0
        out_tok = response.usage.completion_tokens if response.usage else 0
        return response.choices[0].message.content, in_tok, out_tok, model

    elif provider == "claude":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not configured")
        model = "claude-sonnet-4-20250514"
        client = AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=model,
            max_tokens=1500,
            messages=[{"role": "user", "content": query}]
        )
        in_tok  = response.usage.input_tokens  if response.usage else 0
        out_tok = response.usage.output_tokens if response.usage else 0
        return response.content[0].text, in_tok, out_tok, model

    elif provider == "perplexity":
        api_key = os.getenv("PERPLEXITY_API_KEY")
        if not api_key:
            raise ValueError("PERPLEXITY_API_KEY not configured")
        model = "sonar"
        client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.perplexity.ai"
        )
        response = await client.chat.completions.create(
            model=model,
            max_tokens=1500,
            messages=[{"role": "user", "content": query}]
        )
        in_tok  = response.usage.prompt_tokens     if response.usage else 0
        out_tok = response.usage.completion_tokens if response.usage else 0
        return response.choices[0].message.content, in_tok, out_tok, model

    else:
        raise ValueError(f"Unknown provider: {provider}")


def _analyze_response(response_text: str, brand_keywords: List[str], website: str) -> Dict:
    """Analyze an LLM response for brand mentions and citations."""
    text_lower = response_text.lower()
    
    # Check mentions (case-insensitive keyword match)
    mentioned = False
    matched_keyword = None
    for keyword in brand_keywords:
        if keyword.lower() in text_lower:
            mentioned = True
            matched_keyword = keyword
            break
    
    # Check citations (URL presence)
    website_clean = website.lower().replace("www.", "").replace("http://", "").replace("https://", "")
    cited = website_clean in text_lower
    
    # Extract context snippet (200 chars around first mention)
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
    
    # Determine position
    position = "not_found"
    if mentioned:
        first_quarter = text_lower[:len(text_lower)//4]
        if matched_keyword.lower() in first_quarter:
            position = "primary_recommendation"
        elif text_lower.count(matched_keyword.lower()) >= 2:
            position = "listed"
        else:
            position = "mentioned_in_passing"
    
    # Basic sentiment from context
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
        "cited": cited,
        "matched_keyword": matched_keyword,
        "context": context,
        "sentiment": sentiment,
        "position": position,
        "response_text": response_text  # Keep full response for detail view
    }


async def _run_geo_scan(scan_id: str, project_id: str, providers_override: Optional[Dict[str, bool]] = None):
    """Background task: Run GEO visibility scan."""
    
    async with AsyncSessionLocal() as session:
        try:
            # Load project
            result = await session.execute(
                select(GeoMonitorProject).where(GeoMonitorProject.id == project_id)
            )
            project = result.scalar_one_or_none()
            if not project:
                raise ValueError(f"Project {project_id} not found")
            
            # Load scan
            result = await session.execute(
                select(GeoMonitorScan).where(GeoMonitorScan.id == scan_id)
            )
            scan = result.scalar_one_or_none()
            if not scan:
                raise ValueError(f"Scan {scan_id} not found")
            
            # Parse config
            brand_keywords = json.loads(project.brand_keywords)
            target_queries = json.loads(project.target_queries)
            providers_config = json.loads(project.providers_config)
            
            # Override providers if specified
            if providers_override:
                providers_config = providers_override
            
            # Determine active providers
            active_providers = [p for p, enabled in providers_config.items() if enabled]
            
            # Update scan status
            scan.status = "running"
            scan.started_at = datetime.utcnow()
            scan.total_checks = len(target_queries) * len(active_providers)
            await session.commit()
            
            # Run checks with concurrency control
            semaphore = asyncio.Semaphore(3)  # Max 3 concurrent calls
            results = []
            provider_stats = {p: {"total": 0, "mentioned": 0} for p in active_providers}
            
            async def check_query_provider(query: str, provider: str):
                """Check single query-provider combination."""
                async with semaphore:
                    try:
                        # Rate limiting: 1s delay between calls to same provider
                        await asyncio.sleep(1)
                        
                        # Query the provider
                        response, in_tok, out_tok, model_name = await _query_provider(provider, query)
                        asyncio.create_task(track_cost(
                            source="geo_scan",
                            provider=provider,
                            model=model_name,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            source_id=scan.id,
                            website=project.website,
                        ))

                        # Analyze response
                        analysis = _analyze_response(response, brand_keywords, project.website)
                        
                        result = {
                            "query": query,
                            "provider": provider,
                            "mentioned": analysis["mentioned"],
                            "cited": analysis["cited"],
                            "matched_keyword": analysis["matched_keyword"],
                            "context": analysis["context"],
                            "sentiment": analysis["sentiment"],
                            "position": analysis["position"],
                            "response_text": analysis["response_text"],
                            "timestamp": datetime.utcnow().isoformat()
                        }
                        
                        # Update stats
                        provider_stats[provider]["total"] += 1
                        if analysis["mentioned"]:
                            provider_stats[provider]["mentioned"] += 1
                        
                        return result
                        
                    except Exception as e:
                        # Error handling: mark as error and continue
                        return {
                            "query": query,
                            "provider": provider,
                            "mentioned": False,
                            "cited": False,
                            "error": str(e),
                            "timestamp": datetime.utcnow().isoformat()
                        }
            
            # Create tasks for all combinations
            tasks = []
            for query in target_queries:
                for provider in active_providers:
                    tasks.append(check_query_provider(query, provider))
            
            # Execute all checks
            results = await asyncio.gather(*tasks)
            
            # Calculate aggregates
            total_checks = len(results)
            mention_count = sum(1 for r in results if r.get("mentioned", False))
            citation_count = sum(1 for r in results if r.get("cited", False))
            visibility_score = (mention_count / total_checks * 100) if total_checks > 0 else 0
            
            # Calculate provider scores
            provider_scores = {}
            for provider, stats in provider_stats.items():
                if stats["total"] > 0:
                    provider_scores[provider] = round(stats["mentioned"] / stats["total"] * 100, 1)
                else:
                    provider_scores[provider] = 0
            
            # Update scan with results
            scan.status = "completed"
            scan.completed_at = datetime.utcnow()
            scan.completed_checks = total_checks
            scan.visibility_score = round(visibility_score, 1)
            scan.mention_count = mention_count
            scan.citation_count = citation_count
            scan.results_json = json.dumps(results)
            scan.provider_scores = json.dumps(provider_scores)
            
            await session.commit()
            print(f"✓ GEO scan {scan_id} completed: {visibility_score:.1f}% visibility")
            
        except Exception as e:
            # Mark scan as failed
            try:
                result = await session.execute(
                    select(GeoMonitorScan).where(GeoMonitorScan.id == scan_id)
                )
                scan = result.scalar_one_or_none()
                if scan:
                    scan.status = "failed"
                    scan.completed_at = datetime.utcnow()
                    await session.commit()
            except Exception as _db_ex:
                print(f"[geo_monitor] Warning: failed to persist failed status for scan {scan_id}: {_db_ex}")
            print(f"❌ GEO scan {scan_id} failed: {e}")


# ==================== API Endpoints ====================

@router.post("/projects")
async def create_project(
    request: CreateProjectRequest,
    db: AsyncSession = Depends(get_db)
):
    """Create a new GEO monitoring project."""
    
    project_id = str(uuid.uuid4())
    
    project = GeoMonitorProject(
        id=project_id,
        name=request.name,
        website=request.website,
        brand_keywords=json.dumps(request.brand_keywords),
        target_queries=json.dumps(request.target_queries),
        providers_config=json.dumps(request.providers),
        language=request.language
    )
    
    db.add(project)
    await db.commit()
    
    # Generate suggested queries
    suggested = generate_suggested_queries(request.website, request.name, request.language)
    
    return {
        "id": project_id,
        "project": project.to_dict(),
        "suggested_queries": suggested
    }


@router.get("/projects")
async def list_projects(db: AsyncSession = Depends(get_db)):
    """List all GEO monitoring projects with latest scan scores."""
    
    result = await db.execute(
        select(GeoMonitorProject).order_by(desc(GeoMonitorProject.created_at))
    )
    projects = result.scalars().all()
    
    projects_data = []
    for project in projects:
        project_dict = project.to_dict()
        
        # Get latest scan
        scan_result = await db.execute(
            select(GeoMonitorScan)
            .where(GeoMonitorScan.project_id == project.id)
            .order_by(desc(GeoMonitorScan.created_at))
            .limit(1)
        )
        latest_scan = scan_result.scalar_one_or_none()
        
        if latest_scan:
            project_dict["latest_scan"] = {
                "id": latest_scan.id,
                "visibility_score": latest_scan.visibility_score,
                "status": latest_scan.status,
                "completed_at": latest_scan.completed_at.isoformat() if latest_scan.completed_at else None
            }
        else:
            project_dict["latest_scan"] = None
        
        # Get scan count
        count_result = await db.execute(
            select(GeoMonitorScan.id).where(GeoMonitorScan.project_id == project.id)
        )
        scan_count = len(count_result.all())
        project_dict["scan_count"] = scan_count
        
        projects_data.append(project_dict)
    
    return {"projects": projects_data}


@router.get("/projects/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """Get project details with scan history."""
    
    result = await db.execute(
        select(GeoMonitorProject).where(GeoMonitorProject.id == project_id)
    )
    project = result.scalar_one_or_none()
    
    if not project:
        raise_not_found("Project")
    
    # Get scan history
    scan_result = await db.execute(
        select(GeoMonitorScan)
        .where(GeoMonitorScan.project_id == project_id)
        .order_by(desc(GeoMonitorScan.created_at))
    )
    scans = scan_result.scalars().all()
    
    return {
        "project": project.to_dict(),
        "scans": [scan.to_dict() for scan in scans]
    }


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a GEO monitoring project and all its scans."""
    
    result = await db.execute(
        select(GeoMonitorProject).where(GeoMonitorProject.id == project_id)
    )
    project = result.scalar_one_or_none()
    
    if not project:
        raise_not_found("Project")
    
    await db.delete(project)
    await db.commit()
    
    return {"message": "Project deleted successfully"}


@router.post("/projects/{project_id}/scan")
async def start_scan(
    project_id: str,
    request: ScanRequest = ScanRequest(),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db)
):
    """Start a new visibility scan for a project."""
    
    # Verify project exists
    result = await db.execute(
        select(GeoMonitorProject).where(GeoMonitorProject.id == project_id)
    )
    project = result.scalar_one_or_none()
    
    if not project:
        raise_not_found("Project")
    
    # Create scan
    scan_id = str(uuid.uuid4())
    scan = GeoMonitorScan(
        id=scan_id,
        project_id=project_id,
        status="pending"
    )
    
    db.add(scan)
    await db.commit()
    
    # Start background scan
    background_tasks.add_task(
        _run_geo_scan,
        scan_id,
        project_id,
        request.providers
    )
    
    return {
        "scan_id": scan_id,
        "status": "pending",
        "message": "Scan started in background"
    }


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: str, db: AsyncSession = Depends(get_db)):
    """Get detailed results of a scan."""
    
    result = await db.execute(
        select(GeoMonitorScan).where(GeoMonitorScan.id == scan_id)
    )
    scan = result.scalar_one_or_none()
    
    if not scan:
        raise_not_found("Scan")
    
    return {"scan": scan.to_dict()}


@router.get("/projects/{project_id}/trend")
async def get_trend(project_id: str, db: AsyncSession = Depends(get_db)):
    """Get trend data for Chart.js visualization."""
    
    # Get all completed scans
    result = await db.execute(
        select(GeoMonitorScan)
        .where(
            GeoMonitorScan.project_id == project_id,
            GeoMonitorScan.status == "completed"
        )
        .order_by(GeoMonitorScan.completed_at)
    )
    scans = result.scalars().all()
    
    # Format for Chart.js
    labels = []
    overall_scores = []
    provider_data = {}
    
    for scan in scans:
        if scan.completed_at:
            labels.append(scan.completed_at.strftime("%Y-%m-%d %H:%M"))
            overall_scores.append(scan.visibility_score or 0)
            
            # Parse provider scores
            if scan.provider_scores:
                try:
                    scores = json.loads(scan.provider_scores)
                    for provider, score in scores.items():
                        if provider not in provider_data:
                            provider_data[provider] = []
                        provider_data[provider].append(score)
                except Exception as _ex:
                    print(f"[geo_monitor] Warning: failed to parse provider_scores JSON for scan {scan.id}: {_ex}")
    
    # Build datasets
    datasets = [{
        "label": "Overall Visibility",
        "data": overall_scores,
        "borderColor": "rgb(59, 130, 246)",
        "backgroundColor": "rgba(59, 130, 246, 0.1)",
        "tension": 0.4
    }]
    
    # Add provider datasets
    provider_colors = {
        "chatgpt": "rgb(16, 163, 127)",
        "claude": "rgb(168, 85, 247)",
        "perplexity": "rgb(236, 72, 153)"
    }
    
    for provider, data in provider_data.items():
        datasets.append({
            "label": provider.capitalize(),
            "data": data,
            "borderColor": provider_colors.get(provider, "rgb(100, 100, 100)"),
            "backgroundColor": "transparent",
            "tension": 0.4,
            "borderDash": [5, 5]
        })
    
    return {
        "labels": labels,
        "datasets": datasets
    }
