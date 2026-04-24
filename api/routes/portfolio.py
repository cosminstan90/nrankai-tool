"""
Portfolio Dashboard Router - Command center for multi-client website monitoring.

Provides comprehensive overview of all websites with scores, trends, alerts,
and aggregated metrics across audits, GEO visibility, citations, briefs, and schemas.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from api.utils.errors import raise_not_found
from sqlalchemy import desc, func, and_, distinct, case
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.database import (
    AsyncSessionLocal, get_db, Audit, GeoMonitorProject, GeoMonitorScan,
    CitationTracker, CitationScan, ContentBrief, SchemaMarkup, AuditResult
)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


async def calculate_trend(current: float, previous: float) -> str:
    """Calculate trend direction."""
    if previous is None or previous == 0:
        return "stable"
    
    delta = current - previous
    if delta > 5:
        return "improving"
    elif delta < -5:
        return "declining"
    else:
        return "stable"


async def determine_health_status(
    audit_score: Optional[float],
    audit_trend: str,
    geo_score: Optional[float],
    citation_rate: Optional[float]
) -> str:
    """
    Determine overall website health status.
    
    Logic:
    - excellent: audit_score >= 80 and improving
    - good: audit_score >= 60 or all metrics stable
    - needs_work: audit_score < 60 or declining trend
    - poor: audit_score < 40 or severe decline
    """
    if audit_score is None:
        return "unknown"
    
    if audit_score >= 80 and audit_trend == "improving":
        return "excellent"
    elif audit_score >= 70 and audit_trend != "declining":
        return "good"
    elif audit_score >= 60 and audit_trend != "declining":
        return "good"
    elif audit_score >= 40:
        if audit_trend == "declining":
            return "needs_work"
        return "good"
    else:
        return "poor"


async def get_website_audits_data(db: AsyncSession, website: str) -> Dict:
    """Get audit metrics for a specific website."""
    # Get last 2 completed audits for trend calculation
    from sqlalchemy import select
    
    stmt = (
        select(Audit)
        .where(and_(
            Audit.website == website,
            Audit.status == "completed",
            Audit.average_score.isnot(None)
        ))
        .order_by(desc(Audit.completed_at))
        .limit(2)
    )
    
    result = await db.execute(stmt)
    audits = result.scalars().all()
    
    if not audits:
        return {
            "latest_score": None,
            "previous_score": None,
            "delta": None,
            "trend": "no_data",
            "audit_count": 0,
            "last_audit_date": None,
            "last_audit_type": None
        }
    
    latest = audits[0]
    previous = audits[1] if len(audits) > 1 else None
    
    latest_score = latest.average_score
    previous_score = previous.average_score if previous else None
    delta = latest_score - previous_score if previous_score else 0
    
    # Get total audit count
    count_stmt = select(func.count(Audit.id)).where(
        and_(
            Audit.website == website,
            Audit.status == "completed"
        )
    )
    count_result = await db.execute(count_stmt)
    audit_count = count_result.scalar() or 0
    
    trend = await calculate_trend(latest_score, previous_score) if previous_score else "stable"
    
    return {
        "latest_score": round(latest_score, 1) if latest_score else None,
        "previous_score": round(previous_score, 1) if previous_score else None,
        "delta": round(delta, 1) if delta else 0,
        "trend": trend,
        "audit_count": audit_count,
        "last_audit_date": latest.completed_at.isoformat() if latest.completed_at else None,
        "last_audit_type": latest.audit_type
    }


async def get_website_geo_data(db: AsyncSession, website: str) -> Dict:
    """Get GEO visibility metrics for a specific website."""
    from sqlalchemy import select
    
    # Find GEO project for this website
    stmt = select(GeoMonitorProject).where(GeoMonitorProject.website == website)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    
    if not project:
        return {
            "latest_score": None,
            "previous_score": None,
            "delta": None,
            "scans_count": 0
        }
    
    # Get last 2 completed scans
    scans_stmt = (
        select(GeoMonitorScan)
        .where(and_(
            GeoMonitorScan.project_id == project.id,
            GeoMonitorScan.status == "completed",
            GeoMonitorScan.visibility_score.isnot(None)
        ))
        .order_by(desc(GeoMonitorScan.completed_at))
        .limit(2)
    )
    
    scans_result = await db.execute(scans_stmt)
    scans = scans_result.scalars().all()
    
    if not scans:
        return {
            "latest_score": None,
            "previous_score": None,
            "delta": None,
            "scans_count": 0
        }
    
    latest = scans[0]
    previous = scans[1] if len(scans) > 1 else None
    
    latest_score = latest.visibility_score
    previous_score = previous.visibility_score if previous else None
    delta = latest_score - previous_score if previous_score else 0
    
    # Get total scans count
    count_stmt = select(func.count(GeoMonitorScan.id)).where(
        and_(
            GeoMonitorScan.project_id == project.id,
            GeoMonitorScan.status == "completed"
        )
    )
    count_result = await db.execute(count_stmt)
    scans_count = count_result.scalar() or 0
    
    return {
        "latest_score": round(latest_score, 1) if latest_score else None,
        "previous_score": round(previous_score, 1) if previous_score else None,
        "delta": round(delta, 1) if delta else 0,
        "scans_count": scans_count
    }


async def get_website_citations_data(db: AsyncSession, website: str) -> Dict:
    """Get citation metrics for a specific website."""
    from sqlalchemy import select
    
    # Find citation tracker for this website
    stmt = select(CitationTracker).where(CitationTracker.website == website)
    result = await db.execute(stmt)
    tracker = result.scalar_one_or_none()
    
    if not tracker:
        return {
            "citation_rate": None,
            "previous_rate": None,
            "delta": None
        }
    
    # Get last 2 completed scans
    scans_stmt = (
        select(CitationScan)
        .where(and_(
            CitationScan.tracker_id == tracker.id,
            CitationScan.status == "completed",
            CitationScan.citation_rate.isnot(None)
        ))
        .order_by(desc(CitationScan.completed_at))
        .limit(2)
    )
    
    scans_result = await db.execute(scans_stmt)
    scans = scans_result.scalars().all()
    
    if not scans:
        return {
            "citation_rate": None,
            "previous_rate": None,
            "delta": None
        }
    
    latest = scans[0]
    previous = scans[1] if len(scans) > 1 else None
    
    latest_rate = latest.citation_rate
    previous_rate = previous.citation_rate if previous else None
    delta = latest_rate - previous_rate if previous_rate else 0
    
    return {
        "citation_rate": round(latest_rate, 1) if latest_rate else None,
        "previous_rate": round(previous_rate, 1) if previous_rate else None,
        "delta": round(delta, 1) if delta else 0
    }


async def get_website_briefs_data(db: AsyncSession, website: str) -> Dict:
    """Get content briefs summary for a specific website."""
    from sqlalchemy import select
    
    # Get all audit IDs for this website
    audits_stmt = select(Audit.id).where(Audit.website == website)
    audits_result = await db.execute(audits_stmt)
    audit_ids = [row[0] for row in audits_result.all()]
    
    if not audit_ids:
        return {"total": 0, "pending": 0, "approved": 0, "completed": 0}
    
    # Count briefs by status
    stmt = (
        select(
            func.count(ContentBrief.id).label("total"),
            func.sum(case((ContentBrief.status == "generated", 1), else_=0)).label("pending"),
            func.sum(case((ContentBrief.status == "approved", 1), else_=0)).label("approved"),
            func.sum(case((ContentBrief.status == "completed", 1), else_=0)).label("completed")
        )
        .where(ContentBrief.audit_id.in_(audit_ids))
    )
    
    result = await db.execute(stmt)
    row = result.first()
    
    return {
        "total": row.total or 0,
        "pending": row.pending or 0,
        "approved": row.approved or 0,
        "completed": row.completed or 0
    }


async def get_website_schemas_data(db: AsyncSession, website: str) -> Dict:
    """Get schema markup summary for a specific website."""
    from sqlalchemy import select
    
    # Get all audit IDs for this website
    audits_stmt = select(Audit.id).where(Audit.website == website)
    audits_result = await db.execute(audits_stmt)
    audit_ids = [row[0] for row in audits_result.all()]
    
    if not audit_ids:
        return {"total": 0, "valid": 0, "warnings": 0}
    
    # Count schemas by validation status
    stmt = (
        select(
            func.count(SchemaMarkup.id).label("total"),
            func.sum(case((SchemaMarkup.validation_status == "validated", 1), else_=0)).label("valid"),
            func.sum(case((SchemaMarkup.validation_status == "has_warnings", 1), else_=0)).label("warnings")
        )
        .where(SchemaMarkup.audit_id.in_(audit_ids))
    )
    
    result = await db.execute(stmt)
    row = result.first()
    
    return {
        "total": row.total or 0,
        "valid": row.valid or 0,
        "warnings": row.warnings or 0
    }


async def detect_alerts_for_website(
    website: str,
    audits_data: Dict,
    db: AsyncSession
) -> List[str]:
    """Detect alerts for a specific website."""
    alerts = []
    
    # Check for score drops
    if audits_data["delta"] and audits_data["delta"] < -5:
        alerts.append(f"SEO score dropped {abs(audits_data['delta']):.0f} points")
    
    # Check for failed recent audits
    from sqlalchemy import select
    recent_failed_stmt = (
        select(func.count(Audit.id))
        .where(and_(
            Audit.website == website,
            Audit.status == "failed",
            Audit.created_at >= datetime.now(timezone.utc) - timedelta(days=7)
        ))
    )
    failed_result = await db.execute(recent_failed_stmt)
    failed_count = failed_result.scalar() or 0
    
    if failed_count > 0:
        alerts.append(f"{failed_count} audit(s) failed this week")
    
    # Check for stale data (no audit in 30 days)
    if audits_data["last_audit_date"]:
        last_audit = datetime.fromisoformat(audits_data["last_audit_date"])
        days_since = (datetime.now(timezone.utc) - last_audit).days
        if days_since > 30:
            alerts.append(f"No audit in {days_since} days")
    
    return alerts


async def _collect_website_data(website: str) -> Dict:
    """Collect all metrics for a single website using its own DB session."""
    async with AsyncSessionLocal() as session:
        audits = await get_website_audits_data(session, website)
        geo = await get_website_geo_data(session, website)
        citations = await get_website_citations_data(session, website)
        briefs = await get_website_briefs_data(session, website)
        schemas = await get_website_schemas_data(session, website)
        health = await determine_health_status(
            audits["latest_score"],
            audits["trend"],
            geo["latest_score"],
            citations["citation_rate"],
        )
        website_alerts = await detect_alerts_for_website(website, audits, session)

    return {
        "domain": website,
        "audits": audits,
        "geo_visibility": geo,
        "citations": citations,
        "briefs": briefs,
        "schemas": schemas,
        "overall_health": health,
        "alerts": website_alerts,
    }


@router.get("/overview")
async def get_portfolio_overview(db: AsyncSession = Depends(get_db)):
    """
    Get comprehensive portfolio overview with all websites and their metrics.

    Returns:
        - websites: List of all monitored websites with full metrics
        - global_stats: Aggregated statistics across all websites
        - alerts: Active alerts requiring attention
    """
    from sqlalchemy import select

    # Get all distinct websites from audits
    websites_stmt = (
        select(distinct(Audit.website))
        .where(Audit.status == "completed")
        .order_by(Audit.website)
    )
    websites_result = await db.execute(websites_stmt)
    websites = [row[0] for row in websites_result.all()]

    # Collect data for ALL websites in parallel — each uses its own session
    websites_data: List[Dict] = list(
        await asyncio.gather(*[_collect_website_data(w) for w in websites])
    )

    # Flatten alerts
    all_alerts = []
    for site_data in websites_data:
        for alert_msg in site_data["alerts"]:
            severity = "high" if "dropped" in alert_msg or "failed" in alert_msg else "medium"
            alert_type = (
                "score_drop" if "dropped" in alert_msg
                else ("schedule_failed" if "failed" in alert_msg else "stale_data")
            )
            all_alerts.append({
                "type": alert_type,
                "website": site_data["domain"],
                "message": alert_msg,
                "severity": severity,
            })
    
    # Calculate global statistics
    total_audits_stmt = select(func.count(Audit.id)).where(Audit.status == "completed")
    total_audits_result = await db.execute(total_audits_stmt)
    total_audits = total_audits_result.scalar() or 0
    
    total_pages_stmt = select(func.sum(Audit.pages_analyzed)).where(Audit.status == "completed")
    total_pages_result = await db.execute(total_pages_stmt)
    total_pages = total_pages_result.scalar() or 0
    
    avg_score_stmt = select(func.avg(Audit.average_score)).where(
        and_(Audit.status == "completed", Audit.average_score.isnot(None))
    )
    avg_score_result = await db.execute(avg_score_stmt)
    avg_score = avg_score_result.scalar() or 0
    
    # Calculate average GEO visibility across all latest scans
    avg_geo = 0
    geo_count = 0
    for site_data in websites_data:
        if site_data["geo_visibility"]["latest_score"]:
            avg_geo += site_data["geo_visibility"]["latest_score"]
            geo_count += 1
    avg_geo = (avg_geo / geo_count) if geo_count > 0 else 0
    
    # Calculate average citation rate
    avg_citation = 0
    citation_count = 0
    for site_data in websites_data:
        if site_data["citations"]["citation_rate"]:
            avg_citation += site_data["citations"]["citation_rate"]
            citation_count += 1
    avg_citation = (avg_citation / citation_count) if citation_count > 0 else 0
    
    global_stats = {
        "total_websites": len(websites),
        "total_audits": total_audits,
        "total_pages_analyzed": total_pages,
        "avg_score_all": round(avg_score, 1),
        "avg_geo_visibility": round(avg_geo, 1),
        "avg_citation_rate": round(avg_citation, 1)
    }
    
    return {
        "websites": websites_data,
        "global_stats": global_stats,
        "alerts": all_alerts
    }


@router.get("/website/{domain}")
async def get_website_details(domain: str, db: AsyncSession = Depends(get_db)):
    """
    Get detailed metrics and history for a specific website.
    
    Args:
        domain: Website domain (e.g., "ing.ro")
    
    Returns:
        Complete website metrics, trends, and recommended actions
    """
    from sqlalchemy import select
    
    # Verify website exists
    check_stmt = select(Audit.id).where(Audit.website == domain).limit(1)
    check_result = await db.execute(check_stmt)
    if not check_result.first():
        raise_not_found("Website", domain)
    
    # Get all metrics
    audits = await get_website_audits_data(db, domain)
    geo = await get_website_geo_data(db, domain)
    citations = await get_website_citations_data(db, domain)
    briefs = await get_website_briefs_data(db, domain)
    schemas = await get_website_schemas_data(db, domain)
    
    # Get audit history (last 10 completed audits)
    history_stmt = (
        select(Audit)
        .where(and_(
            Audit.website == domain,
            Audit.status == "completed",
            Audit.average_score.isnot(None)
        ))
        .order_by(desc(Audit.completed_at))
        .limit(10)
    )
    history_result = await db.execute(history_stmt)
    audit_history = history_result.scalars().all()
    
    history_data = [{
        "date": audit.completed_at.isoformat() if audit.completed_at else None,
        "score": round(audit.average_score, 1) if audit.average_score else None,
        "type": audit.audit_type,
        "pages": audit.pages_analyzed
    } for audit in audit_history]
    
    # Get GEO visibility trend
    from sqlalchemy import select
    geo_stmt = select(GeoMonitorProject).where(GeoMonitorProject.website == domain)
    geo_result = await db.execute(geo_stmt)
    geo_project = geo_result.scalar_one_or_none()
    
    geo_trend = []
    if geo_project:
        scans_stmt = (
            select(GeoMonitorScan)
            .where(and_(
                GeoMonitorScan.project_id == geo_project.id,
                GeoMonitorScan.status == "completed",
                GeoMonitorScan.visibility_score.isnot(None)
            ))
            .order_by(desc(GeoMonitorScan.completed_at))
            .limit(10)
        )
        scans_result = await db.execute(scans_stmt)
        scans = scans_result.scalars().all()
        
        geo_trend = [{
            "date": scan.completed_at.isoformat() if scan.completed_at else None,
            "score": round(scan.visibility_score, 1) if scan.visibility_score else None
        } for scan in scans]
    
    # Get citation trend
    citation_stmt = select(CitationTracker).where(CitationTracker.website == domain)
    citation_result = await db.execute(citation_stmt)
    citation_tracker = citation_result.scalar_one_or_none()
    
    citation_trend = []
    if citation_tracker:
        citation_scans_stmt = (
            select(CitationScan)
            .where(and_(
                CitationScan.tracker_id == citation_tracker.id,
                CitationScan.status == "completed",
                CitationScan.citation_rate.isnot(None)
            ))
            .order_by(desc(CitationScan.completed_at))
            .limit(10)
        )
        citation_scans_result = await db.execute(citation_scans_stmt)
        citation_scans = citation_scans_result.scalars().all()
        
        citation_trend = [{
            "date": scan.completed_at.isoformat() if scan.completed_at else None,
            "rate": round(scan.citation_rate, 1) if scan.citation_rate else None
        } for scan in citation_scans]
    
    # Determine health status
    health = await determine_health_status(
        audits["latest_score"],
        audits["trend"],
        geo["latest_score"],
        citations["citation_rate"]
    )
    
    # Generate recommended actions
    actions = []
    
    if audits["latest_score"] and audits["latest_score"] < 60:
        actions.append({
            "priority": "high",
            "action": "Generate Content Briefs",
            "reason": f"SEO score below 60 ({audits['latest_score']})"
        })
    
    if audits["trend"] == "declining":
        actions.append({
            "priority": "high",
            "action": "Investigate Score Drop",
            "reason": "Declining trend detected"
        })
    
    if geo["latest_score"] and geo["latest_score"] < 50:
        actions.append({
            "priority": "medium",
            "action": "Improve GEO Visibility",
            "reason": f"GEO visibility below 50% ({geo['latest_score']})"
        })
    
    if not geo["latest_score"]:
        actions.append({
            "priority": "medium",
            "action": "Set Up GEO Monitoring",
            "reason": "No GEO visibility data available"
        })
    
    if audits["last_audit_date"]:
        last_audit = datetime.fromisoformat(audits["last_audit_date"])
        days_since = (datetime.now(timezone.utc) - last_audit).days
        if days_since > 30:
            actions.append({
                "priority": "medium",
                "action": "Run Fresh Audit",
                "reason": f"Last audit was {days_since} days ago"
            })
    
    return {
        "domain": domain,
        "overall_health": health,
        "audits": audits,
        "geo_visibility": geo,
        "citations": citations,
        "briefs": briefs,
        "schemas": schemas,
        "audit_history": history_data,
        "geo_trend": geo_trend,
        "citation_trend": citation_trend,
        "recommended_actions": actions
    }


@router.get("/alerts")
async def get_portfolio_alerts(db: AsyncSession = Depends(get_db)):
    """
    Get all active alerts across the portfolio.
    
    Returns:
        List of alerts sorted by severity (high → medium → low)
    """
    from sqlalchemy import select
    
    # Get all websites
    websites_stmt = (
        select(distinct(Audit.website))
        .where(Audit.status == "completed")
    )
    websites_result = await db.execute(websites_stmt)
    websites = [row[0] for row in websites_result.all()]
    
    all_alerts = []
    
    for website in websites:
        audits = await get_website_audits_data(db, website)
        website_alerts = await detect_alerts_for_website(website, audits, db)
        
        for alert_msg in website_alerts:
            severity = "high" if "dropped" in alert_msg or "failed" in alert_msg else "medium"
            alert_type = "score_drop" if "dropped" in alert_msg else ("schedule_failed" if "failed" in alert_msg else "stale_data")
            
            all_alerts.append({
                "type": alert_type,
                "website": website,
                "message": alert_msg,
                "severity": severity,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
    
    # Sort by severity
    severity_order = {"high": 0, "medium": 1, "low": 2}
    all_alerts.sort(key=lambda x: severity_order.get(x["severity"], 3))
    
    return {"alerts": all_alerts}
