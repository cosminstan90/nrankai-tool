"""
Audit API routes for creating, listing, and managing audits.
"""

import os
import sys
import uuid
import json
import asyncio
from datetime import datetime
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from api.models.database import Audit, AuditResult, AuditLog, get_db
from api.models.schemas import (
    AuditCreate, AuditResponse, AuditListResponse, 
    AuditTypeInfo, StatsResponse, SingleAuditRequest
)
from bs4 import BeautifulSoup
import httpx
from pydantic import BaseModel
from api.workers.audit_worker import start_audit_pipeline, get_active_audit_count

router = APIRouter(prefix="/api/audits", tags=["audits"])


# ============================================================================
# AUDIT CRUD OPERATIONS
# ============================================================================

@router.post("", response_model=AuditResponse, status_code=201)
async def create_audit(
    audit_data: AuditCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new audit and start the pipeline in the background.
    """
    # Check concurrent audit limit
    active_count = await get_active_audit_count(db)
    if active_count >= 10:
        raise HTTPException(
            status_code=429,
            detail="Maximum concurrent audits reached (10). Please wait for running audits to complete."
        )
    
    # Determine model based on provider
    from prompt_loader import list_available_audits
    
    # Validate audit type
    available_audits = list_available_audits()
    valid_types = [a['type'] for a in available_audits]
    if audit_data.audit_type.upper() not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid audit type. Must be one of: {', '.join(valid_types)}"
        )
    
    # Get default model for provider (from centralized registry)
    try:
        from api.provider_registry import get_default_model as _get_default
        model = audit_data.model or _get_default(audit_data.provider)
    except ImportError:
        provider_models = {
            "google": "gemini-2.5-flash",
            "anthropic": "claude-sonnet-4-20250514",
            "openai": "gpt-4o",
            "mistral": "mistral-large-latest"
        }
        model = audit_data.model or provider_models.get(audit_data.provider, "claude-sonnet-4-20250514")
    
    # Create audit record
    audit_id = str(uuid.uuid4())
    audit = Audit(
        id=audit_id,
        website=audit_data.website,
        sitemap_url=audit_data.sitemap_url,
        audit_type=audit_data.audit_type.upper(),
        provider=audit_data.provider,
        model=model,
        status="pending",
        current_step="Queued",
        progress_percent=0,
        language=audit_data.language or "English",
        webhook_url=audit_data.webhook_url or None,
        prompt_version=audit_data.prompt_version or "v3",
    )

    db.add(audit)
    await db.commit()
    await db.refresh(audit)

    # Start the audit pipeline in background
    background_tasks.add_task(
        start_audit_pipeline,
        audit_id=audit_id,
        website=audit_data.website,
        sitemap_url=audit_data.sitemap_url,
        audit_type=audit_data.audit_type.upper(),
        provider=audit_data.provider,
        model=model,
        max_chars=audit_data.max_chars,
        use_direct_mode=audit_data.use_direct_mode,
        concurrency=audit_data.concurrency,
        use_perplexity=audit_data.use_perplexity,
        language=audit_data.language,
        webhook_url=audit_data.webhook_url,
        prompt_version=audit_data.prompt_version or "v3",
    )

    return AuditResponse(**audit.to_dict())


@router.post("/single", status_code=200)
async def single_page_audit(
    request: SingleAuditRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Perform an instant audit on a single URL.
    Returns the JSON results directly and saves to DB.
    """
    try:
        # Fetch HTML
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            headers = {"User-Agent": "GEO-Analyzer/2.1 (Single Page Audit)"}
            response = await client.get(request.url, headers=headers)
            response.raise_for_status()
            html_content = response.text
            
        # Basic text extraction
        soup = BeautifulSoup(html_content, "html.parser")
        for script in soup(["script", "style"]):
            script.extract()
            
        text_content = soup.get_text(separator=' ', strip=True)

        if not text_content:
            raise HTTPException(status_code=400, detail="Could not extract text from the provided URL.")
            
        # Truncate to reasonable limits to avoid massive token costs
        max_chars = 30000
        text_content = text_content[:max_chars]

        from prompt_loader import list_available_audits, load_prompt
        from direct_analyzer import AsyncLLMClient, clean_json_response

        # Determine audits to run
        audits_to_run = []
        if request.audit_type.lower() == "god_mode":
            available = list_available_audits()
            audits_to_run = [a['type'] for a in available]
        else:
            audits_to_run = [request.audit_type.upper()]

        # Determine Provider/Model
        try:
            from api.provider_registry import get_default_model
            provider = request.provider or "anthropic"
            model = request.model or get_default_model(provider)
        except ImportError:
            provider = request.provider or "anthropic"
            provider_models = {
                "google": "gemini-2.5-flash",
                "anthropic": "claude-sonnet-4-20250514",
                "openai": "gpt-4o",
                "mistral": "mistral-large-latest"
            }
            model = request.model or provider_models.get(provider.lower(), "claude-sonnet-4-20250514")

        # Helper function for one audit
        async def run_single(audit_type):
            try:
                system_message = load_prompt(audit_type)
                if request.language and request.language.lower() != "english":
                    system_message += (
                        f"\n\nLANGUAGE INSTRUCTION: Write ALL text values in your JSON response in {request.language}. "
                        f"Keep JSON keys, field names, enum values, and numbers in English."
                    )
                
                llm_client = AsyncLLMClient(provider=provider.upper(), model_name=model)
                text, in_tokens, out_tokens = await llm_client.complete(
                    system_message=system_message,
                    user_content=text_content
                )
                await llm_client.close()
                cleaned = clean_json_response(text)
                return audit_type, json.loads(cleaned)
            except Exception as e:
                return audit_type, {"error": str(e), "status": "failed"}

        # Run concurrently
        tasks = [run_single(a_type) for a_type in audits_to_run]
        results = await asyncio.gather(*tasks)

        # Calculate average score
        total_score = 0
        score_count = 0
        for audit_type, data in results:
            if isinstance(data, dict):
                score = data.get('overall_score') or data.get('score')
                if score is None and audit_type.lower() in data and isinstance(data[audit_type.lower()], dict):
                    inner = data[audit_type.lower()]
                    score = inner.get('overall_score') or inner.get('score')
                
                if score is not None and isinstance(score, (int, float)):
                    if score <= 10 and score > 0:
                        score = round(score * 10)
                    total_score += score
                    score_count += 1
        
        avg_score = total_score / score_count if score_count > 0 else None

        # Save to DB
        audit_id = f"single_{uuid.uuid4().hex[:8]}"
        new_audit = Audit(
            id=audit_id,
            website=request.url,
            audit_type=f"SINGLE_PAGE_GOD_MODE" if request.audit_type.lower() == "god_mode" else f"SINGLE_{request.audit_type.upper()}",
            provider=provider,
            model=model,
            status="completed",
            total_pages=1,
            pages_scraped=1,
            pages_analyzed=1,
            average_score=avg_score,
            current_step="finished",
            progress_percent=100,
            started_at=datetime.utcnow(),
            completed_at=datetime.utcnow(),
            language=request.language
        )
        db.add(new_audit)
        
        for audit_type, data in results:
            classification = "N/A"
            score = None
            if isinstance(data, dict):
                classification = data.get('classification', "N/A")
                score = data.get('overall_score') or data.get('score')
                
                if classification == "N/A" and audit_type.lower() in data and isinstance(data[audit_type.lower()], dict):
                    inner = data[audit_type.lower()]
                    classification = inner.get('classification', "N/A")
                    if score is None:
                        score = inner.get('overall_score') or inner.get('score')
                
                if score is not None and isinstance(score, (int, float)):
                    if score <= 10 and score > 0:
                        score = round(score * 10)
            
            result_record = AuditResult(
                audit_id=audit_id,
                page_url=request.url,
                filename=f"{audit_type}.json",
                result_json=json.dumps(data),
                score=score,
                classification=str(classification)
            )
            db.add(result_record)
            
        await db.commit()

        final_response = {
            "id": audit_id,
            "url": request.url,
            "provider": provider,
            "model": model,
            "results": {a_type: data for a_type, data in results}
        }
        
        return final_response

    except httpx.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"Error fetching URL: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=AuditListResponse)
async def list_audits(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    website: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """
    List all audits with pagination and filtering.
    """
    # Build query
    query = select(Audit)
    count_query = select(func.count(Audit.id))
    
    # Apply filters
    if status:
        query = query.where(Audit.status == status)
        count_query = count_query.where(Audit.status == status)
    if website:
        query = query.where(Audit.website.contains(website))
        count_query = count_query.where(Audit.website.contains(website))
    
    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar()
    
    # Apply pagination and ordering
    query = query.order_by(desc(Audit.created_at))
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    result = await db.execute(query)
    audits = result.scalars().all()
    
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    
    return AuditListResponse(
        audits=[AuditResponse(**a.to_dict()) for a in audits],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """
    Get dashboard statistics.
    """
    # Total audits
    total_result = await db.execute(select(func.count(Audit.id)))
    total_audits = total_result.scalar()
    
    # Status counts
    pending_result = await db.execute(
        select(func.count(Audit.id)).where(Audit.status == "pending")
    )
    pending_audits = pending_result.scalar()
    
    running_result = await db.execute(
        select(func.count(Audit.id)).where(Audit.status.in_(["scraping", "converting", "analyzing"]))
    )
    running_audits = running_result.scalar()
    
    completed_result = await db.execute(
        select(func.count(Audit.id)).where(Audit.status == "completed")
    )
    completed_audits = completed_result.scalar()
    
    failed_result = await db.execute(
        select(func.count(Audit.id)).where(Audit.status == "failed")
    )
    failed_audits = failed_result.scalar()
    
    # Total pages analyzed
    pages_result = await db.execute(
        select(func.sum(Audit.pages_analyzed))
    )
    total_pages = pages_result.scalar() or 0
    
    # Average score
    avg_result = await db.execute(
        select(func.avg(Audit.average_score)).where(Audit.average_score.isnot(None))
    )
    average_score = avg_result.scalar()
    
    return StatsResponse(
        total_audits=total_audits,
        pending_audits=pending_audits,
        running_audits=running_audits,
        completed_audits=completed_audits,
        failed_audits=failed_audits,
        total_pages_analyzed=total_pages,
        average_score=round(average_score, 1) if average_score else None
    )


@router.get("/{audit_id}", response_model=AuditResponse)
async def get_audit(audit_id: str, db: AsyncSession = Depends(get_db)):
    """
    Get details for a specific audit.
    """
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()
    
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    
    return AuditResponse(**audit.to_dict())


class SitemapCountResponse(BaseModel):
    url: str
    count: int
    error: Optional[str] = None

async def _count_sitemap_urls(url: str, visited: set) -> tuple[int, str]:
    """Helper to recursively parse sitemaps and indexes."""
    if url in visited:
        return 0, ""
    visited.add(url)
    
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "GEO-Analyzer/1.0"})
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, features="xml")
            
            # Is this a sitemap index?
            sitemaps = soup.find_all("sitemap")
            if sitemaps:
                total = 0
                for s in sitemaps:
                    loc = s.find("loc")
                    if loc and loc.text:
                        sub_count, _ = await _count_sitemap_urls(loc.text.strip(), visited)
                        total += sub_count
                return total, ""
                
            # Or a regular sitemap?
            urls = soup.find_all("url")
            if urls:
                return len(urls), ""
                
            return 0, "No <url> or <sitemap> tags found in XML."
    except Exception as e:
        return 0, f"Error fetching sitemap: {str(e)}"

@router.get("/sitemap/count", response_model=SitemapCountResponse)
async def get_sitemap_count(url: str = Query(..., description="The full URL to the sitemap.xml file")):
    """
    Fetch a sitemap (or sitemap index) and count the total number of pages it contains.
    """
    if not url.startswith(('http://', 'https://')):
        return SitemapCountResponse(url=url, count=0, error="URL must start with http:// or https://")
        
    count, error = await _count_sitemap_urls(url, set())
    
    return SitemapCountResponse(
        url=url,
        count=count,
        error=error if error else None
    )


@router.post("/{audit_id}/cancel", status_code=200)
async def cancel_audit(audit_id: str, db: AsyncSession = Depends(get_db)):
    """
    Cancel a running or pending audit by marking it as failed.
    Does not delete the audit record or its data — use DELETE to remove entirely.
    """
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()

    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    if audit.status in ["completed", "failed"]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel an audit that is already '{audit.status}'"
        )

    audit.status = "failed"
    audit.current_step = "Cancelled"
    audit.error_message = "Audit cancelled by user"
    audit.completed_at = datetime.utcnow()

    await db.commit()

    return {"message": "Audit cancelled successfully", "audit_id": audit_id}


@router.post("/{audit_id}/retry", status_code=200)
async def retry_audit(
    audit_id: str,
    background_tasks: BackgroundTasks,
    skip_scraping: bool = Query(False),
    provider: Optional[str] = Query(None, description="Override provider (anthropic/openai/mistral/google)"),
    model: Optional[str] = Query(None, description="Override model name"),
    db: AsyncSession = Depends(get_db),
):
    """
    Retry a failed or completed audit.

    Resets the audit to pending and re-runs the full pipeline.
    Use ?skip_scraping=true to skip the scraping + conversion steps and only
    re-run the LLM analysis (faster when the site hasn't changed).
    """
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()

    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    if audit.status in ["pending", "scraping", "converting", "analyzing"]:
        raise HTTPException(
            status_code=400,
            detail=f"Audit is already running (status: '{audit.status}'). "
                   "Cancel it first before retrying."
        )

    # Resolve provider/model — use overrides or fall back to original
    effective_provider = provider or audit.provider
    if model:
        effective_model = model
    elif provider and provider != audit.provider:
        # Provider changed — pick its default model
        try:
            from api.provider_registry import get_default_model as _gdm
            effective_model = _gdm(effective_provider)
        except Exception:
            effective_model = audit.model
    else:
        effective_model = audit.model

    # Reset audit state (update provider/model if changed)
    audit.status = "pending"
    audit.current_step = "Queued"
    audit.progress_percent = 0
    audit.error_message = None
    audit.started_at = None
    audit.completed_at = None
    audit.pages_analyzed = 0
    audit.provider = effective_provider
    audit.model = effective_model
    await db.commit()

    # Snapshot values before session expires
    language = (audit.language or "English")
    webhook_url = audit.webhook_url
    prompt_version = (audit.prompt_version or "v3")

    # OpenAI has strict TPM limits — lower concurrency to avoid 429s
    effective_concurrency = 3 if effective_provider.lower() == "openai" else 5

    background_tasks.add_task(
        start_audit_pipeline,
        audit_id=audit_id,
        website=audit.website,
        sitemap_url=None if skip_scraping else audit.sitemap_url,
        audit_type=audit.audit_type,
        provider=effective_provider,
        model=effective_model,
        max_chars=30000,
        use_direct_mode=True,
        concurrency=effective_concurrency,
        use_perplexity=False,
        language=language,
        webhook_url=webhook_url,
        prompt_version=prompt_version,
    )

    return {
        "message": "Audit retry started",
        "audit_id": audit_id,
        "skip_scraping": skip_scraping,
        "provider": effective_provider,
        "model": effective_model,
    }


@router.delete("/{audit_id}", status_code=204)
async def delete_audit(audit_id: str, force: bool = False, db: AsyncSession = Depends(get_db)):
    """
    Delete an audit and all its results.
    Use ?force=true to delete even running/stuck audits.
    """
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()
    
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    
    # Don't allow deletion of running audits (unless forced or stuck > 30 min)
    if audit.status in ["scraping", "converting", "analyzing"] and not force:
        # Check if audit is stuck (started > 30 minutes ago)
        from datetime import datetime, timedelta, timezone
        if audit.started_at:
            started = audit.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - started
            if age > timedelta(minutes=30):
                # Allow deletion of stuck audits
                pass
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot delete a running audit. Wait for completion, failure, or use ?force=true"
                )
        else:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete a running audit. Wait for completion, failure, or use ?force=true"
            )
    
    await db.delete(audit)
    await db.commit()
    
    # Also clean up the data directory
    data_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        audit.website
    )
    if os.path.exists(data_dir):
        import shutil
        try:
            shutil.rmtree(data_dir)
        except Exception:
            pass  # Best effort cleanup


@router.get("/{audit_id}/export")
async def export_audit(audit_id: str, db: AsyncSession = Depends(get_db)):
    """
    Export audit results as Excel file.
    """
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()
    
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    
    if audit.status != "completed":
        raise HTTPException(status_code=400, detail="Audit is not completed yet")
    
    # Generate Excel file
    import pandas as pd
    from io import BytesIO
    
    # Get results
    results_query = select(AuditResult).where(AuditResult.audit_id == audit_id)
    results_result = await db.execute(results_query)
    results = results_result.scalars().all()
    
    if not results:
        raise HTTPException(status_code=404, detail="No results found for this audit")
    
    # Create DataFrame
    data = []
    for r in results:
        row = {
            "URL": r.page_url,
            "Filename": r.filename,
            "Score": r.score,
            "Classification": r.classification
        }
        if r.result_json:
            try:
                result_data = json.loads(r.result_json)
                # Flatten the JSON for Excel
                for key, value in result_data.items():
                    if isinstance(value, (str, int, float, bool)):
                        row[key] = value
                    elif isinstance(value, (list, dict)):
                        row[key] = json.dumps(value, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        data.append(row)
    
    df = pd.DataFrame(data)
    
    # Create Excel file in memory
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Results', index=False)
    output.seek(0)
    
    # Save to temp file (cross-platform)
    import tempfile
    filename = f"{audit.website}_{audit.audit_type}_{audit_id[:8]}.xlsx"
    temp_path = os.path.join(tempfile.gettempdir(), filename)
    with open(temp_path, "wb") as f:
        f.write(output.getvalue())
    
    return FileResponse(
        temp_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename
    )


# ============================================================================
# AUDIT TYPES
# ============================================================================

@router.get("/types/list", response_model=list[AuditTypeInfo])
async def list_audit_types():
    """
    List all available audit types.
    """
    from prompt_loader import list_available_audits
    
    audits = list_available_audits()
    return [AuditTypeInfo(**a) for a in audits]
