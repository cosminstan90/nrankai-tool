"""
Cost Tracking API Routes

Provides cost tracking for LLM API calls, client billing management,
and margin calculation for the Website LLM Analyzer.

Author: Enhanced by Claude
Created: 2026-02-20
"""

import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException
from api.utils.errors import raise_not_found
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc, and_, extract
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import (
    get_db,
    CostRecord,
    ClientBilling,
    AsyncSessionLocal
)

# Initialize router
router = APIRouter(prefix="/api/costs", tags=["costs"])

# USD to EUR conversion rate (can be env var or updated via API)
USD_EUR_RATE = float(os.getenv("USD_EUR_RATE", "0.92"))

# Cost per million tokens (same as direct_analyzer.py)
COST_PER_MILLION_TOKENS = {
    "anthropic": {
        "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
        "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
        "claude-opus-4-5-20251101": {"input": 15.00, "output": 75.00},
    },
    "openai": {
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    },
    "mistral": {
        "mistral-large-latest": {"input": 2.00, "output": 6.00},
        "mistral-small-latest": {"input": 0.20, "output": 0.60},
    }
}


# ============================================================================
# Pydantic Models
# ============================================================================

class CostRecordCreate(BaseModel):
    """Schema for creating a cost record."""
    audit_id: Optional[str] = None
    source: str = Field(..., description="Source type: audit, summary, geo_scan, citation_scan, brief, schema")
    source_id: Optional[str] = None
    website: Optional[str] = None
    provider: str
    model: str
    input_tokens: int
    output_tokens: int


class ClientBillingCreate(BaseModel):
    """Schema for creating/updating client billing."""
    website: str
    client_name: Optional[str] = None
    monthly_fee_eur: Optional[float] = None
    notes: Optional[str] = None


class CostSummaryResponse(BaseModel):
    """Cost summary response."""
    total_cost_usd: float
    by_website: List[Dict[str, Any]]
    by_source: List[Dict[str, Any]]
    by_provider: List[Dict[str, Any]]
    by_month: List[Dict[str, Any]]
    daily_trend: List[Dict[str, Any]]


class MarginResponse(BaseModel):
    """Client margin response."""
    clients: List[Dict[str, Any]]


# ============================================================================
# Helper Functions
# ============================================================================

def calculate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Calculate cost based on token usage.
    
    Args:
        provider: LLM provider (anthropic, openai, mistral)
        model: Model name
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
    
    Returns:
        Cost in USD
    """
    prices = COST_PER_MILLION_TOKENS.get(provider.lower(), {}).get(model, {"input": 3.0, "output": 15.0})
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000


async def track_cost(
    source: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    audit_id: Optional[str] = None,
    source_id: Optional[str] = None,
    website: Optional[str] = None
):
    """
    Fire-and-forget cost tracking helper.
    
    Call this after any LLM API call to record costs.
    Doesn't raise exceptions to avoid breaking main flow.
    """
    try:
        async with AsyncSessionLocal() as db:
            record = CostRecord(
                audit_id=audit_id,
                source=source,
                source_id=source_id,
                website=website,
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=calculate_cost(provider, model, input_tokens, output_tokens)
            )
            db.add(record)
            await db.commit()
    except Exception as e:
        # Silent fail - don't break main flow for cost tracking
        print(f"Warning: Failed to track cost: {e}")


def get_date_range(period: str) -> tuple[Optional[datetime], Optional[datetime]]:
    """
    Get date range for filtering based on period.
    
    Args:
        period: "7d", "30d", "90d", or "all"
    
    Returns:
        Tuple of (start_date, end_date)
    """
    if period == "all":
        return None, None
    
    days_map = {
        "7d": 7,
        "30d": 30,
        "90d": 90
    }
    
    days = days_map.get(period, 30)
    start_date = datetime.utcnow() - timedelta(days=days)
    return start_date, None


# ============================================================================
# API Endpoints
# ============================================================================

@router.post("/record")
async def record_cost(
    cost_data: CostRecordCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Record a cost entry (internal use by other modules).
    
    Automatically calculates estimated_cost_usd from token counts.
    """
    try:
        cost = calculate_cost(
            cost_data.provider,
            cost_data.model,
            cost_data.input_tokens,
            cost_data.output_tokens
        )
        
        record = CostRecord(
            audit_id=cost_data.audit_id,
            source=cost_data.source,
            source_id=cost_data.source_id,
            website=cost_data.website,
            provider=cost_data.provider,
            model=cost_data.model,
            input_tokens=cost_data.input_tokens,
            output_tokens=cost_data.output_tokens,
            estimated_cost_usd=cost
        )
        
        db.add(record)
        await db.commit()
        await db.refresh(record)
        
        return {
            "success": True,
            "cost_id": record.id,
            "estimated_cost_usd": record.estimated_cost_usd
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to record cost: {str(e)}")


@router.get("/summary")
async def get_cost_summary(
    period: str = "30d",
    website: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Get cost summary with aggregations.
    
    Query params:
        - period: "7d", "30d", "90d", "all" (default: "30d")
        - website: Optional filter by website
    """
    try:
        start_date, _ = get_date_range(period)
        
        # Base query
        query_filters = []
        if start_date:
            query_filters.append(CostRecord.created_at >= start_date)
        if website:
            query_filters.append(CostRecord.website == website)
        
        # Total cost
        total_query = select(func.sum(CostRecord.estimated_cost_usd))
        if query_filters:
            total_query = total_query.where(and_(*query_filters))
        
        result = await db.execute(total_query)
        total_cost = result.scalar() or 0.0
        
        # By website
        website_query = (
            select(
                CostRecord.website,
                func.sum(CostRecord.estimated_cost_usd).label("cost"),
                func.count(func.distinct(CostRecord.audit_id)).label("audits"),
                func.count(CostRecord.id).label("calls")
            )
            .where(CostRecord.website.isnot(None))
        )
        if query_filters:
            website_query = website_query.where(and_(*query_filters))
        website_query = website_query.group_by(CostRecord.website).order_by(desc("cost"))
        
        result = await db.execute(website_query)
        by_website = [
            {
                "website": row.website,
                "cost": round(row.cost, 2),
                "audits": row.audits,
                "calls": row.calls
            }
            for row in result.all()
        ]
        
        # By source
        source_query = (
            select(
                CostRecord.source,
                func.sum(CostRecord.estimated_cost_usd).label("cost"),
                func.count(CostRecord.id).label("calls")
            )
        )
        if query_filters:
            source_query = source_query.where(and_(*query_filters))
        source_query = source_query.group_by(CostRecord.source).order_by(desc("cost"))
        
        result = await db.execute(source_query)
        by_source = [
            {
                "source": row.source,
                "cost": round(row.cost, 2),
                "calls": row.calls
            }
            for row in result.all()
        ]
        
        # By provider
        provider_query = (
            select(
                CostRecord.provider,
                func.sum(CostRecord.estimated_cost_usd).label("cost"),
                func.count(CostRecord.id).label("calls")
            )
        )
        if query_filters:
            provider_query = provider_query.where(and_(*query_filters))
        provider_query = provider_query.group_by(CostRecord.provider).order_by(desc("cost"))
        
        result = await db.execute(provider_query)
        by_provider = [
            {
                "provider": row.provider,
                "cost": round(row.cost, 2),
                "calls": row.calls
            }
            for row in result.all()
        ]
        
        # By month (last 12 months)
        twelve_months_ago = datetime.utcnow() - timedelta(days=365)
        month_filters = [CostRecord.created_at >= twelve_months_ago]
        if website:
            month_filters.append(CostRecord.website == website)
        
        month_query = (
            select(
                func.strftime('%Y-%m', CostRecord.created_at).label("month"),
                func.sum(CostRecord.estimated_cost_usd).label("cost")
            )
            .where(and_(*month_filters))
            .group_by("month")
            .order_by("month")
        )
        
        result = await db.execute(month_query)
        by_month = [
            {
                "month": row.month,
                "cost": round(row.cost, 2)
            }
            for row in result.all()
        ]
        
        # Daily trend (last 30 days for selected period)
        days_for_trend = 30 if period in ["30d", "90d", "all"] else 7
        trend_start = datetime.utcnow() - timedelta(days=days_for_trend)
        trend_filters = [CostRecord.created_at >= trend_start]
        if website:
            trend_filters.append(CostRecord.website == website)
        
        daily_query = (
            select(
                func.date(CostRecord.created_at).label("date"),
                func.sum(CostRecord.estimated_cost_usd).label("cost")
            )
            .where(and_(*trend_filters))
            .group_by("date")
            .order_by("date")
        )
        
        result = await db.execute(daily_query)
        daily_trend = [
            {
                "date": row.date,
                "cost": round(row.cost, 2)
            }
            for row in result.all()
        ]
        
        return {
            "total_cost_usd": round(total_cost, 2),
            "by_website": by_website,
            "by_source": by_source,
            "by_provider": by_provider,
            "by_month": by_month,
            "daily_trend": daily_trend
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get cost summary: {str(e)}")


@router.get("/margins")
async def get_margins(db: AsyncSession = Depends(get_db)):
    """
    Calculate profit margins per client.
    
    Compares monthly fees with actual costs this month.
    """
    try:
        # Get all billing configs
        billing_query = select(ClientBilling)
        result = await db.execute(billing_query)
        billings = result.scalars().all()
        
        # Get current month's costs per website
        current_month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        clients = []
        
        for billing in billings:
            # Get costs for this website this month
            cost_query = (
                select(func.sum(CostRecord.estimated_cost_usd))
                .where(
                    and_(
                        CostRecord.website == billing.website,
                        CostRecord.created_at >= current_month_start
                    )
                )
            )
            result = await db.execute(cost_query)
            cost_usd = result.scalar() or 0.0
            cost_eur = cost_usd * USD_EUR_RATE
            
            # Calculate margin
            monthly_fee = billing.monthly_fee_eur or 0.0
            margin_eur = monthly_fee - cost_eur
            margin_percent = (margin_eur / monthly_fee * 100) if monthly_fee > 0 else 0.0
            
            clients.append({
                "website": billing.website,
                "client_name": billing.client_name,
                "monthly_fee_eur": round(monthly_fee, 2),
                "cost_this_month_usd": round(cost_usd, 2),
                "cost_this_month_eur": round(cost_eur, 2),
                "margin_eur": round(margin_eur, 2),
                "margin_percent": round(margin_percent, 1)
            })
        
        # Sort by margin desc
        clients.sort(key=lambda x: x["margin_eur"], reverse=True)
        
        return {"clients": clients}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to calculate margins: {str(e)}")


@router.post("/billing")
async def create_or_update_billing(
    billing_data: ClientBillingCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Create or update client billing configuration.
    """
    try:
        # Check if exists
        query = select(ClientBilling).where(ClientBilling.website == billing_data.website)
        result = await db.execute(query)
        existing = result.scalar_one_or_none()
        
        if existing:
            # Update
            if billing_data.client_name is not None:
                existing.client_name = billing_data.client_name
            if billing_data.monthly_fee_eur is not None:
                existing.monthly_fee_eur = billing_data.monthly_fee_eur
            if billing_data.notes is not None:
                existing.notes = billing_data.notes
            existing.updated_at = datetime.utcnow()
            
            await db.commit()
            await db.refresh(existing)
            
            return {
                "success": True,
                "action": "updated",
                "billing": existing.to_dict()
            }
        else:
            # Create new
            billing = ClientBilling(
                website=billing_data.website,
                client_name=billing_data.client_name,
                monthly_fee_eur=billing_data.monthly_fee_eur,
                notes=billing_data.notes
            )
            
            db.add(billing)
            await db.commit()
            await db.refresh(billing)
            
            return {
                "success": True,
                "action": "created",
                "billing": billing.to_dict()
            }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save billing config: {str(e)}")


@router.get("/billing")
async def list_billing_configs(db: AsyncSession = Depends(get_db)):
    """
    List all client billing configurations.
    """
    try:
        query = select(ClientBilling).order_by(ClientBilling.client_name)
        result = await db.execute(query)
        billings = result.scalars().all()
        
        return {
            "billings": [b.to_dict() for b in billings]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list billing configs: {str(e)}")


@router.delete("/billing/{website}")
async def delete_billing_config(
    website: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Delete a client billing configuration.
    """
    try:
        query = select(ClientBilling).where(ClientBilling.website == website)
        result = await db.execute(query)
        billing = result.scalar_one_or_none()
        
        if not billing:
            raise_not_found("Billing config")
        
        await db.delete(billing)
        await db.commit()
        
        return {"success": True, "message": "Billing config deleted"}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete billing config: {str(e)}")


@router.get("/audit/{audit_id}")
async def get_audit_costs(
    audit_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Get cost breakdown for a specific audit.
    """
    try:
        # Get all costs for this audit
        query = (
            select(CostRecord)
            .where(CostRecord.audit_id == audit_id)
            .order_by(CostRecord.created_at)
        )
        result = await db.execute(query)
        records = result.scalars().all()
        
        if not records:
            return {
                "audit_id": audit_id,
                "total_cost_usd": 0.0,
                "records": []
            }
        
        total_cost = sum(r.estimated_cost_usd for r in records)
        
        return {
            "audit_id": audit_id,
            "total_cost_usd": round(total_cost, 2),
            "records": [r.to_dict() for r in records]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get audit costs: {str(e)}")


@router.get("/recent")
async def get_recent_costs(
    limit: int = 50,
    source: Optional[str] = None,
    website: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Get recent cost records with optional filtering.
    
    Query params:
        - limit: Number of records (default: 50, max: 200)
        - source: Filter by source type
        - website: Filter by website
    """
    try:
        limit = min(limit, 200)  # Cap at 200
        
        query = select(CostRecord)
        
        filters = []
        if source:
            filters.append(CostRecord.source == source)
        if website:
            filters.append(CostRecord.website == website)
        
        if filters:
            query = query.where(and_(*filters))
        
        query = query.order_by(desc(CostRecord.created_at)).limit(limit)
        
        result = await db.execute(query)
        records = result.scalars().all()
        
        return {
            "records": [r.to_dict() for r in records],
            "count": len(records)
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get recent costs: {str(e)}")
