"""
Dashboard chart data endpoints -- plain SQL aggregation, no LLM.

Split out of api/routes/compare.py (Etapa 5.2 of the consolidation,
docs/CONSOLIDATION_PLAN.md): compare.py bundled three unrelated feature
groups under one router (dashboard charts, ad-hoc audit comparison, and
single-page re-run). URL paths are unchanged (still under /api/charts/...)
-- this is a file reorganization, not a behavior change.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import Audit, AuditResult, get_db

router = APIRouter(prefix="/api", tags=["dashboard-charts"])


@router.get("/charts/score-distribution")
async def chart_score_distribution(
    audit_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Get score distribution data for charts.
    If audit_id is provided, returns distribution for that audit.
    Otherwise, returns aggregate distribution across all completed audits.
    """
    query = select(
        AuditResult.classification,
        func.count(AuditResult.id)
    ).where(AuditResult.classification.isnot(None))

    if audit_id:
        query = query.where(AuditResult.audit_id == audit_id)

    query = query.group_by(AuditResult.classification)

    result = await db.execute(query)
    distribution = dict(result.fetchall())

    return {
        "labels": ["Excellent (85-100)", "Good (70-84)", "Needs Work (50-69)", "Poor (0-49)"],
        "values": [
            distribution.get("excellent", 0),
            distribution.get("good", 0),
            distribution.get("needs_work", 0),
            distribution.get("poor", 0)
        ],
        "colors": ["#22c55e", "#3b82f6", "#eab308", "#ef4444"]
    }


@router.get("/charts/score-trend")
async def chart_score_trend(
    website: Optional[str] = None,
    limit: int = Query(20, ge=5, le=50),
    db: AsyncSession = Depends(get_db)
):
    """
    Get average score trend over recent audits for line chart.
    """
    query = select(
        Audit.id,
        Audit.website,
        Audit.audit_type,
        Audit.average_score,
        Audit.completed_at
    ).where(
        Audit.status == "completed",
        Audit.average_score.isnot(None)
    )

    if website:
        query = query.where(Audit.website == website)

    query = query.order_by(desc(Audit.completed_at)).limit(limit)

    result = await db.execute(query)
    rows = result.fetchall()

    # Reverse to get chronological order
    rows = list(reversed(rows))

    return {
        "labels": [
            f"{r.audit_type[:3]}·{r.website[:12]}" for r in rows
        ],
        "values": [r.average_score for r in rows],
        "dates": [r.completed_at.strftime("%Y-%m-%d %H:%M") if r.completed_at else "" for r in rows],
        "details": [
            {"id": r.id, "website": r.website, "audit_type": r.audit_type}
            for r in rows
        ]
    }


@router.get("/charts/audits-by-type")
async def chart_audits_by_type(db: AsyncSession = Depends(get_db)):
    """
    Get count of audits grouped by audit type.
    """
    query = select(
        Audit.audit_type,
        func.count(Audit.id),
        func.avg(Audit.average_score)
    ).where(
        Audit.status == "completed"
    ).group_by(Audit.audit_type)

    result = await db.execute(query)
    rows = result.fetchall()

    return {
        "types": [r[0] for r in rows],
        "counts": [r[1] for r in rows],
        "avg_scores": [round(r[2], 1) if r[2] else None for r in rows]
    }


@router.get("/charts/websites-overview")
async def chart_websites_overview(db: AsyncSession = Depends(get_db)):
    """
    Get overview of all websites with their latest audit scores.
    """
    # Get distinct websites with their latest audit
    subquery = select(
        Audit.website,
        func.max(Audit.completed_at).label("latest_completed")
    ).where(
        Audit.status == "completed"
    ).group_by(Audit.website).subquery()

    query = select(
        Audit.website,
        Audit.audit_type,
        Audit.average_score,
        Audit.pages_analyzed,
        Audit.completed_at
    ).join(
        subquery,
        (Audit.website == subquery.c.website) &
        (Audit.completed_at == subquery.c.latest_completed)
    )

    result = await db.execute(query)
    rows = result.fetchall()

    return {
        "websites": [
            {
                "website": r.website,
                "audit_type": r.audit_type,
                "average_score": r.average_score,
                "pages_analyzed": r.pages_analyzed,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None
            }
            for r in rows
        ]
    }
