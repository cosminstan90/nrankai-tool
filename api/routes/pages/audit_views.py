"""Audit detail and site health routes."""

import json

from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from api.models.database import (
    get_db, Audit, AuditResult, AuditSummary,
)
from ._shared import templates, _AUDIT_TYPE_LABELS, _load_weights, _compute_composite

router = APIRouter()


@router.get("/audits/{audit_id}", response_class=HTMLResponse)
async def audit_detail(
    request: Request,
    audit_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Audit detail page with real-time updates."""
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()

    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    # Get results summary if completed
    results_summary = None
    if audit.status == "completed":
        # Get score distribution
        dist_query = select(
            AuditResult.classification,
            func.count(AuditResult.id)
        ).where(
            AuditResult.audit_id == audit_id
        ).group_by(AuditResult.classification)

        dist_result = await db.execute(dist_query)
        score_distribution = dict(dist_result.fetchall())

        # Get top and bottom results
        top_query = select(AuditResult).where(
            AuditResult.audit_id == audit_id
        ).order_by(desc(AuditResult.score)).limit(5)
        top_result = await db.execute(top_query)
        top_results = top_result.scalars().all()

        bottom_query = select(AuditResult).where(
            AuditResult.audit_id == audit_id,
            AuditResult.score.isnot(None)
        ).order_by(AuditResult.score).limit(5)
        bottom_result = await db.execute(bottom_query)
        bottom_results = bottom_result.scalars().all()

        results_summary = {
            "distribution": score_distribution,
            "top_results": top_results,
            "bottom_results": bottom_results
        }

    if audit.audit_type.startswith('SINGLE_'):
        # Get all results for this single page audit
        results_query = select(AuditResult).where(AuditResult.audit_id == audit_id)
        res = await db.execute(results_query)
        all_results = res.scalars().all()

        single_results_dict = {}
        for r in all_results:
            a_type = r.filename.replace('.json', '')
            try:
                single_results_dict[a_type] = json.loads(r.result_json)
            except Exception as e:
                import traceback
                print(f"Failed to parse JSON for {a_type} in {audit_id}:\n{traceback.format_exc()}")
                single_results_dict[a_type] = {"error": "Invalid JSON"}

        return templates.TemplateResponse("single_audit_detail.html", {
            "request": request,
            "audit": audit,
            "single_results_dict": single_results_dict
        })

    return templates.TemplateResponse("audit_detail.html", {
        "request": request,
        "audit": audit,
        "results_summary": results_summary
    })


@router.get("/audits/{audit_id}/results", response_class=HTMLResponse)
async def audit_results_page(
    request: Request,
    audit_id: str,
    page: int = 1,
    min_score: Optional[int] = Query(None, ge=0, le=100),
    max_score: Optional[int] = Query(None, ge=0, le=100),
    classification: Optional[str] = Query(None),
    url_search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db)
):
    """Full results table page with optional score/classification/URL filters."""
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()

    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    # ── Score distribution (always unfiltered, for the summary chart) ─────────
    dist_q = (
        select(AuditResult.score, func.count(AuditResult.id))
        .where(AuditResult.audit_id == audit_id, AuditResult.score.isnot(None))
        .group_by(AuditResult.score)
    )
    dist_rows = dict((await db.execute(dist_q)).fetchall())
    score_distribution = {
        "0-49":   sum(v for k, v in dist_rows.items() if 0  <= k <= 49),
        "50-69":  sum(v for k, v in dist_rows.items() if 50 <= k <= 69),
        "70-84":  sum(v for k, v in dist_rows.items() if 70 <= k <= 84),
        "85-100": sum(v for k, v in dist_rows.items() if 85 <= k <= 100),
    }

    # ── Build filter conditions ───────────────────────────────────────────────
    results_query = select(AuditResult).where(AuditResult.audit_id == audit_id)
    count_query   = select(func.count(AuditResult.id)).where(AuditResult.audit_id == audit_id)

    if min_score is not None:
        results_query = results_query.where(AuditResult.score >= min_score)
        count_query   = count_query.where(AuditResult.score >= min_score)
    if max_score is not None:
        results_query = results_query.where(AuditResult.score <= max_score)
        count_query   = count_query.where(AuditResult.score <= max_score)
    if classification:
        results_query = results_query.where(AuditResult.classification == classification)
        count_query   = count_query.where(AuditResult.classification == classification)
    if url_search:
        results_query = results_query.where(AuditResult.page_url.ilike(f"%{url_search}%"))
        count_query   = count_query.where(AuditResult.page_url.ilike(f"%{url_search}%"))

    # ── Pagination ────────────────────────────────────────────────────────────
    page_size = 50
    offset = (page - 1) * page_size

    results_query = results_query.order_by(desc(AuditResult.score)).offset(offset).limit(page_size)
    results_result = await db.execute(results_query)
    results = results_result.scalars().all()

    count_result = await db.execute(count_query)
    total = count_result.scalar()
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1

    # ── Build filter query-string prefix for pagination links ─────────────────
    from urllib.parse import quote as _quote
    _qs_parts = []
    if min_score is not None:
        _qs_parts.append(f"min_score={min_score}")
    if max_score is not None:
        _qs_parts.append(f"max_score={max_score}")
    if classification:
        _qs_parts.append(f"classification={_quote(classification)}")
    if url_search:
        _qs_parts.append(f"url_search={_quote(url_search)}")
    filter_qs = ("&".join(_qs_parts) + "&") if _qs_parts else ""

    return templates.TemplateResponse("results.html", {
        "request": request,
        "audit": audit,
        "results": results,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "score_distribution": score_distribution,
        "min_score": min_score if min_score is not None else "",
        "max_score": max_score if max_score is not None else "",
        "classification": classification or "",
        "url_search": url_search or "",
        "filter_qs": filter_qs,
    })


@router.get("/audits/{audit_id}/report", response_class=HTMLResponse)
async def audit_report_page(
    request: Request,
    audit_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Full audit report page (printable/PDF-friendly)."""
    result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = result.scalar_one_or_none()

    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    if audit.status != "completed":
        raise HTTPException(status_code=400, detail="Audit is not completed yet")

    # Get all results
    results_result = await db.execute(
        select(AuditResult).where(
            AuditResult.audit_id == audit_id
        ).order_by(desc(AuditResult.score))
    )
    results = results_result.scalars().all()

    # Score distribution
    dist_query = select(
        AuditResult.classification,
        func.count(AuditResult.id)
    ).where(
        AuditResult.audit_id == audit_id
    ).group_by(AuditResult.classification)

    dist_result = await db.execute(dist_query)
    score_distribution = dict(dist_result.fetchall())

    # Parse result_json for top issues
    top_issues = []
    for r in results[:20]:
        if r.result_json:
            try:
                data = json.loads(r.result_json)
                # Extract optimization opportunities
                opps = data.get("optimization_opportunities", [])
                for opp in opps[:3]:
                    if isinstance(opp, dict) and opp.get("priority") == "high":
                        top_issues.append({
                            "page_url": r.page_url,
                            "category": opp.get("category", ""),
                            "recommendation": opp.get("recommendation", ""),
                            "current_state": opp.get("current_state", "")
                        })
            except (json.JSONDecodeError, TypeError):
                pass

    # Get AI Summary if exists
    summary_result = await db.execute(
        select(AuditSummary).where(AuditSummary.audit_id == audit_id)
    )
    summary = summary_result.scalar_one_or_none()

    return templates.TemplateResponse("report.html", {
        "request": request,
        "audit": audit,
        "results": results,
        "score_distribution": score_distribution,
        "top_issues": top_issues[:20],
        "summary": summary.to_dict() if summary else None
    })


@router.get("/pages", response_class=HTMLResponse)
async def page_view(
    request: Request,
    url: str = Query(..., description="Page URL to view across all audit types"),
    db: AsyncSession = Depends(get_db)
):
    """Unified per-URL view — shows every audit type result for a single page."""
    import json as _json
    from urllib.parse import unquote

    decoded_url = unquote(url)

    # Fetch all AuditResults for this URL, joined with Audit, most-recent-per-type
    stmt = (
        select(AuditResult, Audit)
        .join(Audit, AuditResult.audit_id == Audit.id)
        .where(AuditResult.page_url == decoded_url)
        .where(Audit.status == "completed")
        .order_by(Audit.audit_type, desc(Audit.created_at))
    )
    rows = (await db.execute(stmt)).fetchall()

    # Deduplicate: keep most-recent per audit_type
    seen: set = set()
    audit_results = []
    for ar, audit in rows:
        if audit.audit_type not in seen:
            seen.add(audit.audit_type)
            try:
                rdata = _json.loads(ar.result_json) if ar.result_json else {}
            except Exception:
                rdata = {}
            raw_issues = rdata.get("issues", [])
            audit_results.append({
                "audit_type": audit.audit_type,
                "audit_type_label": _AUDIT_TYPE_LABELS.get(audit.audit_type, audit.audit_type),
                "audit_id": audit.id,
                "result_id": ar.id,
                "score": ar.score,
                "classification": ar.classification,
                "provider": audit.provider,
                "model": audit.model,
                "completed_at": audit.completed_at.strftime("%Y-%m-%d") if audit.completed_at else None,
                "top_issues": raw_issues[:3],
                "website": audit.website,
            })

    if not audit_results:
        raise HTTPException(status_code=404, detail=f"No completed audit results found for: {decoded_url}")

    # Load effective weights (DB override or hardcoded defaults)
    weights = await _load_weights(db)

    # Sort by weight descending so most important audits appear first
    audit_results.sort(key=lambda x: weights.get(x["audit_type"], 0), reverse=True)

    # Composite score
    composite = _compute_composite({r["audit_type"]: r["score"] for r in audit_results}, weights)

    # Chart data (radar)
    chart_labels = [r["audit_type_label"] for r in audit_results if r["score"] is not None]
    chart_scores = [r["score"] for r in audit_results if r["score"] is not None]

    # ── Score history per audit_type for this specific URL (oldest → newest) ─
    history_data: dict = {}
    for atype in seen:
        hist_stmt = (
            select(Audit.completed_at, AuditResult.score)
            .join(Audit, AuditResult.audit_id == Audit.id)
            .where(
                AuditResult.page_url == decoded_url,
                Audit.audit_type == atype,
                Audit.status == "completed",
                AuditResult.score.isnot(None),
            )
            .order_by(desc(Audit.completed_at))
            .limit(10)
        )
        hist_rows = (await db.execute(hist_stmt)).fetchall()
        if len(hist_rows) >= 2:
            history_data[atype] = [
                {
                    "date": row[0].strftime("%Y-%m-%d") if row[0] else None,
                    "score": row[1],
                    "label": _AUDIT_TYPE_LABELS.get(atype, atype),
                }
                for row in reversed(hist_rows)   # oldest first for chart
            ]

    return templates.TemplateResponse("page_view.html", {
        "request": request,
        "page_url": decoded_url,
        "website": audit_results[0]["website"] if audit_results else "",
        "audit_results": audit_results,
        "composite_score": composite,
        "chart_labels": _json.dumps(chart_labels),
        "chart_scores": _json.dumps(chart_scores),
        "history_data": _json.dumps(history_data),
        "total_audits": len(audit_results),
    })


@router.get("/sites/{website:path}/export/csv")
async def site_health_csv(website: str, db: AsyncSession = Depends(get_db)):
    """Export site health summary as a CSV file."""
    import csv as _csv
    import io as _io
    from fastapi.responses import StreamingResponse as _SR

    stmt = (
        select(Audit)
        .where(Audit.website == website, Audit.status == "completed")
        .order_by(Audit.audit_type, desc(Audit.created_at))
    )
    all_audits = (await db.execute(stmt)).scalars().all()

    seen: set = set()
    latest_by_type: dict = {}
    for audit in all_audits:
        if audit.audit_type not in seen:
            seen.add(audit.audit_type)
            latest_by_type[audit.audit_type] = audit

    if not latest_by_type:
        raise HTTPException(status_code=404, detail=f"No completed audits for: {website}")

    weights = await _load_weights(db)

    output = _io.StringIO()
    writer = _csv.writer(output)
    writer.writerow(["Audit Type", "Label", "Avg Score", "Pages Analyzed", "Weight %", "Completed", "Provider", "Model"])

    for atype, audit in sorted(latest_by_type.items(), key=lambda x: weights.get(x[0], 0), reverse=True):
        avg_q = select(func.avg(AuditResult.score)).where(
            AuditResult.audit_id == audit.id, AuditResult.score.isnot(None)
        )
        avg_score = (await db.execute(avg_q)).scalar()
        avg_score = round(avg_score, 1) if avg_score else ""

        count_q = select(func.count(AuditResult.id)).where(AuditResult.audit_id == audit.id)
        page_count = (await db.execute(count_q)).scalar() or 0

        writer.writerow([
            atype,
            _AUDIT_TYPE_LABELS.get(atype, atype),
            avg_score,
            page_count,
            round(weights.get(atype, 0.02) * 100),
            audit.completed_at.strftime("%Y-%m-%d") if audit.completed_at else "",
            audit.provider or "",
            audit.model or "",
        ])

    content = output.getvalue()
    safe_site = website.replace("https://", "").replace("http://", "").replace("/", "_")
    filename = f"site_health_{safe_site}.csv"

    return _SR(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/sites/{website:path}", response_class=HTMLResponse)
async def site_health(
    request: Request,
    website: str,
    db: AsyncSession = Depends(get_db)
):
    """Site health overview — composite score + latest audit per type for a whole domain."""
    import json as _json

    # For each audit_type, get the most recent completed audit for this website
    stmt = (
        select(Audit)
        .where(Audit.website == website, Audit.status == "completed")
        .order_by(Audit.audit_type, desc(Audit.created_at))
    )
    all_audits = (await db.execute(stmt)).scalars().all()

    # Deduplicate by audit_type — most recent per type
    seen: set = set()
    latest_by_type: dict = {}
    for audit in all_audits:
        if audit.audit_type not in seen:
            seen.add(audit.audit_type)
            latest_by_type[audit.audit_type] = audit

    if not latest_by_type:
        raise HTTPException(status_code=404, detail=f"No completed audits found for: {website}")

    # Load effective weights (DB override or hardcoded defaults)
    weights = await _load_weights(db)

    # For each latest audit, get the site-average score from AuditResult
    audit_summaries = []
    for atype, audit in sorted(latest_by_type.items(),
                                key=lambda x: weights.get(x[0], 0), reverse=True):
        avg_q = select(func.avg(AuditResult.score)).where(
            AuditResult.audit_id == audit.id,
            AuditResult.score.isnot(None)
        )
        avg_score = (await db.execute(avg_q)).scalar()
        avg_score = round(avg_score) if avg_score else None

        count_q = select(func.count(AuditResult.id)).where(AuditResult.audit_id == audit.id)
        page_count = (await db.execute(count_q)).scalar() or 0

        audit_summaries.append({
            "audit_type": atype,
            "audit_type_label": _AUDIT_TYPE_LABELS.get(atype, atype),
            "audit_id": audit.id,
            "avg_score": avg_score,
            "page_count": page_count,
            "provider": audit.provider,
            "model": audit.model,
            "completed_at": audit.completed_at.strftime("%Y-%m-%d") if audit.completed_at else None,
            "weight_pct": round(weights.get(atype, 0.02) * 100),
        })

    # Composite health score
    composite = _compute_composite({s["audit_type"]: s["avg_score"] for s in audit_summaries}, weights)

    # Chart data (radar)
    chart_labels = [s["audit_type_label"] for s in audit_summaries if s["avg_score"] is not None]
    chart_scores = [s["avg_score"] for s in audit_summaries if s["avg_score"] is not None]

    # ── Score history (last 10 runs per audit type, oldest→newest) ────────────
    history_data: dict = {}
    for atype in latest_by_type:
        hist_stmt = (
            select(Audit.completed_at, func.avg(AuditResult.score))
            .join(AuditResult, AuditResult.audit_id == Audit.id)
            .where(
                Audit.website == website,
                Audit.audit_type == atype,
                Audit.status == "completed",
                AuditResult.score.isnot(None),
            )
            .group_by(Audit.id, Audit.completed_at)
            .order_by(desc(Audit.completed_at))
            .limit(10)
        )
        hist_rows = (await db.execute(hist_stmt)).fetchall()
        if len(hist_rows) >= 2:
            history_data[atype] = [
                {
                    "date": row[0].strftime("%Y-%m-%d") if row[0] else None,
                    "score": round(row[1]) if row[1] else None,
                    "label": _AUDIT_TYPE_LABELS.get(atype, atype),
                }
                for row in reversed(hist_rows)  # oldest first for chart
            ]

    # ── Worst 5 pages per audit type (for drill-down section) ────────────────
    worst_pages_by_type: dict = {}
    for atype, audit in latest_by_type.items():
        worst_q = (
            select(AuditResult.page_url, AuditResult.score, AuditResult.classification)
            .where(AuditResult.audit_id == audit.id, AuditResult.score.isnot(None))
            .order_by(AuditResult.score)
            .limit(5)
        )
        worst_rows = (await db.execute(worst_q)).fetchall()
        if worst_rows:
            worst_pages_by_type[atype] = [
                {
                    "url": row[0],
                    "score": int(row[1]),
                    "classification": row[2] or "",
                    "audit_id": audit.id,
                }
                for row in worst_rows
            ]

    return templates.TemplateResponse("site_health.html", {
        "request": request,
        "website": website,
        "audit_summaries": audit_summaries,
        "composite_score": composite,
        "chart_labels": _json.dumps(chart_labels),
        "chart_scores": _json.dumps(chart_scores),
        "history_data": _json.dumps(history_data),
        "total_audit_types": len(audit_summaries),
        "total_audits_run": len(all_audits),
        "worst_pages_by_type": worst_pages_by_type,
    })


@router.get("/compare", response_class=HTMLResponse)
async def compare_page(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Compare audits page."""
    # Get all completed audits for the selector
    result = await db.execute(
        select(Audit).where(Audit.status == "completed").order_by(desc(Audit.created_at))
    )
    audits = result.scalars().all()

    return templates.TemplateResponse("compare.html", {
        "request": request,
        "audits": [a.to_dict() for a in audits]
    })
