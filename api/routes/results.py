"""
Results API routes for viewing audit results and logs.
"""

import csv
import io
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from api.utils.errors import raise_not_found
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse
import asyncio

from api.models.database import Audit, AuditResult, AuditLog, get_db
from api.models.schemas import (
    AuditResultResponse, AuditResultsResponse, AuditLogResponse
)

router = APIRouter(prefix="/api/audits", tags=["results"])


@router.get("/{audit_id}/results", response_model=AuditResultsResponse)
async def get_audit_results(
    audit_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    min_score: Optional[int] = Query(None, ge=0, le=100),
    max_score: Optional[int] = Query(None, ge=0, le=100),
    classification: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """
    Get paginated results for a specific audit.
    """
    # Verify audit exists
    audit_result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = audit_result.scalar_one_or_none()
    
    if not audit:
        raise_not_found("Audit")
    
    # Build query
    query = select(AuditResult).where(AuditResult.audit_id == audit_id)
    count_query = select(func.count(AuditResult.id)).where(AuditResult.audit_id == audit_id)
    
    # Apply filters
    if min_score is not None:
        query = query.where(AuditResult.score >= min_score)
        count_query = count_query.where(AuditResult.score >= min_score)
    if max_score is not None:
        query = query.where(AuditResult.score <= max_score)
        count_query = count_query.where(AuditResult.score <= max_score)
    if classification:
        query = query.where(AuditResult.classification == classification)
        count_query = count_query.where(AuditResult.classification == classification)
    
    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar()
    
    # Apply pagination and ordering
    query = query.order_by(desc(AuditResult.score))
    query = query.offset((page - 1) * page_size).limit(page_size)
    
    result = await db.execute(query)
    results = result.scalars().all()
    
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    
    # Calculate score distribution
    dist_query = select(
        AuditResult.score,
        func.count(AuditResult.id)
    ).where(
        AuditResult.audit_id == audit_id
    ).where(
        AuditResult.score.isnot(None)
    ).group_by(AuditResult.score)
    
    dist_result = await db.execute(dist_query)
    score_counts = dict(dist_result.fetchall())
    
    # Bucket scores into ranges
    score_distribution = {
        "0-49": sum(v for k, v in score_counts.items() if k is not None and 0 <= k <= 49),
        "50-69": sum(v for k, v in score_counts.items() if k is not None and 50 <= k <= 69),
        "70-84": sum(v for k, v in score_counts.items() if k is not None and 70 <= k <= 84),
        "85-100": sum(v for k, v in score_counts.items() if k is not None and 85 <= k <= 100)
    }
    
    # Calculate average score
    avg_query = select(func.avg(AuditResult.score)).where(
        AuditResult.audit_id == audit_id
    ).where(AuditResult.score.isnot(None))
    avg_result = await db.execute(avg_query)
    average_score = avg_result.scalar()
    
    return AuditResultsResponse(
        results=[AuditResultResponse(
            id=r.id,
            audit_id=r.audit_id,
            page_url=r.page_url,
            filename=r.filename,
            score=r.score,
            classification=r.classification,
            result_json=json.loads(r.result_json) if r.result_json else None,
            created_at=r.created_at
        ) for r in results],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        average_score=round(average_score, 1) if average_score else None,
        score_distribution=score_distribution
    )


@router.get("/{audit_id}/results/csv")
async def export_audit_results_csv(
    audit_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Export all audit results for an audit as a CSV file.
    Includes URL, filename, score, and classification columns.
    """
    audit_result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = audit_result.scalar_one_or_none()

    if not audit:
        raise_not_found("Audit")

    query = (
        select(AuditResult)
        .where(AuditResult.audit_id == audit_id)
        .order_by(desc(AuditResult.score))
    )
    result = await db.execute(query)
    rows = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["URL", "Filename", "Score", "Classification"])
    for r in rows:
        writer.writerow([
            r.page_url or "",
            r.filename or "",
            r.score if r.score is not None else "",
            r.classification or "",
        ])

    content = output.getvalue()
    safe_name = (
        f"{audit.website}_{audit.audit_type}.csv"
        .replace("https://", "").replace("http://", "")
        .replace("/", "_").replace(":", "").replace(" ", "_")
    )

    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.get("/{audit_id}/results/{result_id}", response_model=AuditResultResponse)
async def get_single_result(
    audit_id: str,
    result_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Get a single result by ID.
    """
    result = await db.execute(
        select(AuditResult).where(
            AuditResult.audit_id == audit_id,
            AuditResult.id == result_id
        )
    )
    audit_result = result.scalar_one_or_none()

    if not audit_result:
        raise_not_found("Result")

    return AuditResultResponse(
        id=audit_result.id,
        audit_id=audit_result.audit_id,
        page_url=audit_result.page_url,
        filename=audit_result.filename,
        score=audit_result.score,
        classification=audit_result.classification,
        result_json=json.loads(audit_result.result_json) if audit_result.result_json else None,
        created_at=audit_result.created_at
    )


@router.get("/{audit_id}/logs", response_model=list[AuditLogResponse])
async def get_audit_logs(
    audit_id: str,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db)
):
    """
    Get recent logs for an audit.
    """
    # Verify audit exists
    audit_result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = audit_result.scalar_one_or_none()
    
    if not audit:
        raise_not_found("Audit")
    
    # Get logs
    query = select(AuditLog).where(
        AuditLog.audit_id == audit_id
    ).order_by(desc(AuditLog.created_at)).limit(limit)
    
    result = await db.execute(query)
    logs = result.scalars().all()
    
    return [AuditLogResponse(**log.to_dict()) for log in reversed(logs)]


@router.get("/{audit_id}/stream")
async def stream_audit_status(
    audit_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Server-Sent Events endpoint for real-time audit status updates.
    """
    # Verify audit exists
    audit_result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = audit_result.scalar_one_or_none()
    
    if not audit:
        raise_not_found("Audit")
    
    async def event_generator():
        """Generate SSE events for audit status updates."""
        last_log_id = 0
        last_status = None
        last_progress = None
        last_pages_analyzed = None

        while True:
            # Get fresh session
            from api.models.database import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                # Get current audit status
                result = await session.execute(
                    select(Audit).where(Audit.id == audit_id)
                )
                current_audit = result.scalar_one_or_none()

                if not current_audit:
                    yield {
                        "event": "error",
                        "data": json.dumps({"message": "Audit not found"})
                    }
                    break

                # Send status update if anything visible changed
                if (current_audit.status != last_status
                        or current_audit.progress_percent != last_progress
                        or current_audit.pages_analyzed != last_pages_analyzed):
                    last_status = current_audit.status
                    last_progress = current_audit.progress_percent
                    last_pages_analyzed = current_audit.pages_analyzed
                    
                    yield {
                        "event": "status",
                        "data": json.dumps({
                            "status": current_audit.status,
                            "current_step": current_audit.current_step,
                            "progress_percent": current_audit.progress_percent,
                            "total_pages": current_audit.total_pages,
                            "pages_scraped": current_audit.pages_scraped,
                            "pages_analyzed": current_audit.pages_analyzed,
                            "average_score": current_audit.average_score
                        })
                    }
                
                # Get new logs
                logs_result = await session.execute(
                    select(AuditLog).where(
                        AuditLog.audit_id == audit_id,
                        AuditLog.id > last_log_id
                    ).order_by(AuditLog.id)
                )
                new_logs = logs_result.scalars().all()
                
                for log in new_logs:
                    last_log_id = log.id
                    yield {
                        "event": "log",
                        "data": json.dumps({
                            "level": log.level,
                            "message": log.message,
                            "timestamp": log.created_at.isoformat()
                        })
                    }
                
                # Check if audit is complete
                if current_audit.status in ["completed", "failed"]:
                    yield {
                        "event": "complete",
                        "data": json.dumps({
                            "status": current_audit.status,
                            "average_score": current_audit.average_score,
                            "error_message": current_audit.error_message
                        })
                    }
                    break
            
            # Poll interval
            await asyncio.sleep(1)
    
    return EventSourceResponse(event_generator())
