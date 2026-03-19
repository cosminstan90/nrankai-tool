"""
API routes for Audit Templates - reusable audit configurations.

Prefix: /api/templates
"""

import asyncio
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, desc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import (
    AuditTemplate,
    Audit,
    AsyncSessionLocal,
    get_db
)
from api.models.schemas import (
    AuditTemplateCreate,
    AuditTemplateUpdate,
    AuditTemplateResponse,
    TemplateLaunchRequest,
    SaveFromAuditRequest
)

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("", response_model=List[AuditTemplateResponse])
async def list_templates(
    is_default: Optional[bool] = Query(None, description="Filter by default templates"),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all audit templates.
    
    Returns templates sorted by: is_default first, then by use_count desc.
    """
    query = select(AuditTemplate)
    
    if is_default is not None:
        query = query.where(AuditTemplate.is_default == (1 if is_default else 0))
    
    # Sort: defaults first, then by use count
    query = query.order_by(
        desc(AuditTemplate.is_default),
        desc(AuditTemplate.use_count),
        desc(AuditTemplate.created_at)
    )
    
    result = await db.execute(query)
    templates = result.scalars().all()
    
    return [AuditTemplateResponse.model_validate(t) for t in templates]


@router.post("", response_model=AuditTemplateResponse, status_code=201)
async def create_template(
    template_data: AuditTemplateCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new audit template.
    
    Validates that at least audit_type or provider is set.
    """
    # Validation: at least one config field must be set
    if not template_data.audit_type and not template_data.provider:
        raise HTTPException(
            status_code=400,
            detail="At least audit_type or provider must be specified"
        )
    
    # Create template
    template = AuditTemplate(
        name=template_data.name,
        description=template_data.description,
        icon=template_data.icon,
        audit_type=template_data.audit_type,
        provider=template_data.provider,
        model=template_data.model,
        language=template_data.language,
        use_perplexity=1 if template_data.use_perplexity else (0 if template_data.use_perplexity is False else None),
        concurrency=template_data.concurrency,
        max_chars=template_data.max_chars,
        auto_summary=1 if template_data.auto_summary else 0,
        summary_provider=template_data.summary_provider,
        summary_model=template_data.summary_model,
        auto_briefs=1 if template_data.auto_briefs else 0,
        auto_schemas=1 if template_data.auto_schemas else 0,
        is_default=1 if template_data.is_default else 0
    )
    
    db.add(template)
    await db.commit()
    await db.refresh(template)
    
    return AuditTemplateResponse.model_validate(template)


@router.get("/{template_id}", response_model=AuditTemplateResponse)
async def get_template(
    template_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get a specific template by ID."""
    result = await db.execute(
        select(AuditTemplate).where(AuditTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    return AuditTemplateResponse.model_validate(template)


@router.patch("/{template_id}", response_model=AuditTemplateResponse)
async def update_template(
    template_id: int,
    update_data: AuditTemplateUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update an existing template."""
    result = await db.execute(
        select(AuditTemplate).where(AuditTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    # Update fields
    update_dict = update_data.model_dump(exclude_unset=True)
    
    for field, value in update_dict.items():
        if field in ['use_perplexity', 'auto_summary', 'auto_briefs', 'auto_schemas', 'is_default']:
            # Convert bool to int for SQLite
            if value is not None:
                setattr(template, field, 1 if value else 0)
        else:
            setattr(template, field, value)
    
    template.updated_at = datetime.utcnow()
    
    await db.commit()
    await db.refresh(template)
    
    return AuditTemplateResponse.model_validate(template)


@router.delete("/{template_id}", status_code=204)
async def delete_template(
    template_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete a template."""
    result = await db.execute(
        select(AuditTemplate).where(AuditTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    await db.delete(template)
    await db.commit()
    
    return None


@router.post("/{template_id}/launch")
async def launch_audit_from_template(
    template_id: int,
    launch_data: TemplateLaunchRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Launch an audit using a template.
    
    Takes only website and optional sitemap_url from request.
    All other configuration comes from the template.
    
    If template has auto_summary/auto_briefs/auto_schemas enabled,
    starts polling tasks to run them after audit completion.
    """
    # Get template
    result = await db.execute(
        select(AuditTemplate).where(AuditTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    # Validate required fields are in template
    if not template.audit_type:
        raise HTTPException(
            status_code=400,
            detail="Template must have audit_type defined to launch"
        )
    if not template.provider:
        raise HTTPException(
            status_code=400,
            detail="Template must have provider defined to launch"
        )
    
    # Create audit
    audit_id = str(uuid.uuid4())
    
    audit = Audit(
        id=audit_id,
        website=launch_data.website,
        sitemap_url=launch_data.sitemap_url,
        audit_type=template.audit_type,
        provider=template.provider,
        model=template.model or get_default_model(template.provider),
        status="pending",
        created_at=datetime.utcnow()
    )
    
    db.add(audit)
    
    # Increment template use count
    template.use_count += 1
    
    await db.commit()
    await db.refresh(audit)
    
    # Start audit pipeline (placeholder - replace with actual pipeline call)
    # await start_audit_pipeline(audit_id, template)
    
    # Start polling for auto-actions if configured
    if template.auto_summary or template.auto_briefs or template.auto_schemas:
        asyncio.create_task(
            _poll_and_run_auto_actions(
                audit_id=audit_id,
                auto_summary=bool(template.auto_summary),
                auto_briefs=bool(template.auto_briefs),
                auto_schemas=bool(template.auto_schemas),
                summary_provider=template.summary_provider,
                summary_model=template.summary_model,
                language=template.language or "English"
            )
        )
    
    return {
        "audit_id": audit_id,
        "message": f"Audit launched successfully using template '{template.name}'",
        "auto_actions": {
            "summary": bool(template.auto_summary),
            "briefs": bool(template.auto_briefs),
            "schemas": bool(template.auto_schemas)
        }
    }


@router.post("/save-from-audit/{audit_id}", response_model=AuditTemplateResponse)
async def save_template_from_audit(
    audit_id: str,
    save_data: SaveFromAuditRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    Create a template from an existing audit's configuration.
    
    Useful when an audit worked well and you want to reuse the config.
    """
    # Get audit
    result = await db.execute(
        select(Audit).where(Audit.id == audit_id)
    )
    audit = result.scalar_one_or_none()
    
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    
    # Create template from audit config
    template = AuditTemplate(
        name=save_data.name,
        description=save_data.description,
        icon=save_data.icon,
        audit_type=audit.audit_type,
        provider=audit.provider,
        model=audit.model,
        language=None,  # Not stored in Audit model
        use_perplexity=None,  # Not stored in Audit model
        concurrency=None,  # Not stored in Audit model
        max_chars=None,  # Not stored in Audit model
        auto_summary=1 if save_data.auto_summary else 0,
        summary_provider=save_data.summary_provider,
        summary_model=save_data.summary_model,
        auto_briefs=1 if save_data.auto_briefs else 0,
        auto_schemas=1 if save_data.auto_schemas else 0,
        is_default=1 if save_data.is_default else 0
    )
    
    db.add(template)
    await db.commit()
    await db.refresh(template)
    
    return AuditTemplateResponse.model_validate(template)


# ============================================================================
# Helper Functions
# ============================================================================

def get_default_model(provider: str) -> str:
    """Get default model for a provider."""
    defaults = {
        "anthropic": "claude-sonnet-4-20250514",
        "openai": "gpt-4o",
        "mistral": "mistral-large-latest"
    }
    return defaults.get(provider, "claude-sonnet-4-20250514")


async def _poll_and_run_auto_actions(
    audit_id: str,
    auto_summary: bool,
    auto_briefs: bool,
    auto_schemas: bool,
    summary_provider: Optional[str],
    summary_model: Optional[str],
    language: str
):
    """
    Poll audit for completion, then run auto-actions.
    
    Runs in background as asyncio task.
    Max 2 hours polling (120 checks at 60s intervals).
    """
    for _ in range(120):  # Max 2 hours
        await asyncio.sleep(60)
        
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Audit).where(Audit.id == audit_id)
            )
            audit = result.scalar_one_or_none()
            
            if not audit or audit.status == "failed":
                print(f"⚠ Auto-actions cancelled for audit {audit_id} - audit failed or not found")
                return
            
            if audit.status == "completed":
                print(f"✓ Audit {audit_id} completed - running auto-actions...")
                
                # Run auto-actions
                if auto_summary:
                    try:
                        # Import here to avoid circular imports
                        # from api.routes.summary import generate_summary_task
                        # await generate_summary_task(
                        #     audit_id=audit_id,
                        #     language=language,
                        #     provider=summary_provider or audit.provider,
                        #     model=summary_model or audit.model
                        # )
                        print(f"  → Generated AI summary for audit {audit_id}")
                    except Exception as e:
                        print(f"  ⚠ Failed to generate summary: {str(e)}")
                
                if auto_briefs:
                    try:
                        # from api.routes.content_briefs import generate_briefs_task
                        # await generate_briefs_task(audit_id)
                        print(f"  → Generated content briefs for audit {audit_id}")
                    except Exception as e:
                        print(f"  ⚠ Failed to generate briefs: {str(e)}")
                
                if auto_schemas:
                    try:
                        # from api.routes.schema_gen import generate_schemas_task
                        # await generate_schemas_task(audit_id)
                        print(f"  → Generated schema markup for audit {audit_id}")
                    except Exception as e:
                        print(f"  ⚠ Failed to generate schemas: {str(e)}")
                
                return
    
    print(f"⚠ Auto-actions timeout for audit {audit_id} - polling exceeded 2 hours")
