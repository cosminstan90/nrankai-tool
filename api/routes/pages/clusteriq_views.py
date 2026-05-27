"""ClusterIQ page views."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models._base import AsyncSessionLocal
from api.models.clusteriq import (
    CluCluster,
    CluCompetitorJob,
    CluDecision,
    CluDuplicate,
    CluProject,
    CluUrl,
    CluUrlCluster,
)
from api.models.database import get_db
from app.modules.clusteriq.tasks.pipeline import PHASE_LABEL, PHASE_PROGRESS

from ._shared import templates

router = APIRouter()


async def _get_project_or_404(project_id: int, db: AsyncSession) -> CluProject:
    row = (await db.execute(
        select(CluProject).where(CluProject.id == project_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"ClusterIQ project {project_id} not found")
    return row  # type: ignore[return-value]


@router.get("/clusteriq/{project_id}", response_class=HTMLResponse)
async def clusteriq_overview_page(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    project = await _get_project_or_404(project_id, db)
    status  = project.status or "pending"

    # Overview stats (same logic as the API endpoint)
    total_urls = (await db.execute(
        select(func.count(CluUrl.id)).where(CluUrl.project_id == project_id)
    )).scalar() or 0

    demand_rows = (await db.execute(
        select(CluCluster.id)  # just need a count
        .join(CluDecision, CluDecision.cluster_id == CluCluster.id, isouter=True)
        .where(CluCluster.project_id == project_id)
    )).all()

    # Simple demand count from serp data
    from api.models.clusteriq import CluSerpData
    demand_confirmed = (await db.execute(
        select(func.count(func.distinct(CluSerpData.url)))
        .where(
            CluSerpData.project_id == project_id,
            CluSerpData.impressions > 0,
        )
    )).scalar() or 0

    demand_pct = round(demand_confirmed / total_urls * 100, 1) if total_urls else 0.0

    clusters_total = (await db.execute(
        select(func.count(CluCluster.id)).where(CluCluster.project_id == project_id)
    )).scalar() or 0

    multi_topic = (await db.execute(
        select(func.count(func.distinct(CluUrlCluster.url_id)))
        .join(CluUrl, CluUrl.id == CluUrlCluster.url_id)
        .where(
            CluUrl.project_id == project_id,
            CluUrlCluster.is_multi_topic == True,  # noqa: E712
        )
    )).scalar() or 0

    verdict_rows = (await db.execute(
        select(CluDecision.verdict, func.count(CluDecision.id).label("cnt"))
        .join(CluCluster, CluCluster.id == CluDecision.cluster_id)
        .where(CluCluster.project_id == project_id)
        .group_by(CluDecision.verdict)
    )).all()
    verdicts = {r.verdict.lower(): r.cnt for r in verdict_rows}

    # Count clusters that have a ContentIQ brief generated
    briefs_generated = (await db.execute(
        select(func.count(CluDecision.id))
        .join(CluCluster, CluCluster.id == CluDecision.cluster_id)
        .where(
            CluCluster.project_id == project_id,
            CluDecision.brief_id.isnot(None),
        )
    )).scalar() or 0

    overview = {
        "total_urls":            total_urls,
        "clusters_total":        clusters_total,
        "demand_confirmed_urls": demand_confirmed,
        "demand_confirmed_pct":  demand_pct,
        "no_demand_urls":        max(0, total_urls - demand_confirmed),
        "multi_topic_urls":      multi_topic,
        "briefs_generated":      briefs_generated,
        "verdicts": {
            "fix":         verdicts.get("fix",         0),
            "consolidate": verdicts.get("consolidate", 0),
            "optimize":    verdicts.get("optimize",    0),
            "keep":        verdicts.get("keep",        0),
            "prune":       verdicts.get("prune",       0),
        },
    }

    return templates.TemplateResponse("clusteriq/overview.html", {
        "request":      request,
        "project":      project,
        "overview":     overview,
        "phase_label":  PHASE_LABEL.get(status, status),
        "progress_pct": PHASE_PROGRESS.get(status, 0),
    })


@router.get("/clusteriq/{project_id}/clusters", response_class=HTMLResponse)
async def clusteriq_clusters_page(
    request: Request,
    project_id: int,
    verdict_filter: str = Query("", alias="verdict_filter"),
    db: AsyncSession = Depends(get_db),
):
    project = await _get_project_or_404(project_id, db)
    return templates.TemplateResponse("clusteriq/clusters.html", {
        "request":            request,
        "project":            project,
        "verdict_filter_init": verdict_filter,
    })


@router.get("/clusteriq/{project_id}/clusters/{cluster_id}", response_class=HTMLResponse)
async def clusteriq_cluster_detail_page(
    request: Request,
    project_id: int,
    cluster_id: int,
    db: AsyncSession = Depends(get_db),
):
    project = await _get_project_or_404(project_id, db)

    cluster = (await db.execute(
        select(CluCluster).where(
            CluCluster.id == cluster_id,
            CluCluster.project_id == project_id,
        )
    )).scalar_one_or_none()
    if cluster is None:
        raise HTTPException(status_code=404, detail=f"Cluster {cluster_id} not found")

    # Member URLs with aggregated impressions
    from api.models.clusteriq import CluSerpData
    member_rows = (await db.execute(
        select(
            CluUrlCluster.url_id,
            CluUrlCluster.role,
            CluUrlCluster.impression_share_pct,
            CluUrlCluster.rank_in_cluster,
            CluUrlCluster.is_multi_topic,
            CluUrl.url,
            CluUrl.status_code,
            CluUrl.is_indexable,
            CluUrl.title,
        )
        .join(CluUrl, CluUrl.id == CluUrlCluster.url_id)
        .where(CluUrlCluster.cluster_id == cluster_id)
        .order_by(CluUrlCluster.rank_in_cluster.asc().nullslast())
    )).all()

    url_strings = [r.url for r in member_rows]
    agg_by_url: dict = {}
    if url_strings:
        agg_rows = (await db.execute(
            select(
                CluSerpData.url,
                func.sum(CluSerpData.impressions).label("imp"),
                func.sum(CluSerpData.clicks).label("clk"),
                func.avg(CluSerpData.position).label("avg_pos"),
            )
            .where(
                CluSerpData.project_id == project_id,
                CluSerpData.url.in_(url_strings),
            )
            .group_by(CluSerpData.url)
        )).all()
        agg_by_url = {
            r.url: {
                "imp":     r.imp or 0,
                "clk":     r.clk or 0,
                "avg_pos": round(float(r.avg_pos), 1) if r.avg_pos else None,
            }
            for r in agg_rows
        }

    members = []
    for r in member_rows:
        agg = agg_by_url.get(r.url, {})
        members.append({
            "url_id":               r.url_id,
            "url":                  r.url,
            "title":                r.title,
            "status_code":          r.status_code,
            "is_indexable":         r.is_indexable,
            "role":                 r.role,
            "rank_in_cluster":      r.rank_in_cluster,
            "impression_share_pct": r.impression_share_pct,
            "is_multi_topic":       r.is_multi_topic,
            "impressions":          agg.get("imp", 0),
            "clicks":               agg.get("clk", 0),
            "avg_position":         agg.get("avg_pos"),
        })

    # Decision
    decision = (await db.execute(
        select(CluDecision).where(CluDecision.cluster_id == cluster_id)
    )).scalar_one_or_none()

    # Duplicate cohort — pairs where either URL is in this cluster
    url_id_set = {r.url_id for r in member_rows}
    duplicates_raw = []
    if url_id_set:
        dup_rows = (await db.execute(
            select(CluDuplicate)
            .where(
                CluDuplicate.project_id == project_id,
                CluDuplicate.winner_url_id.in_(url_id_set)
                | CluDuplicate.duplicate_url_id.in_(url_id_set),
            )
            .order_by(CluDuplicate.similarity_score.desc().nullslast())
        )).scalars().all()

        # Resolve URL strings
        all_url_ids = set()
        for d in dup_rows:
            if d.winner_url_id:
                all_url_ids.add(d.winner_url_id)
            if d.duplicate_url_id:
                all_url_ids.add(d.duplicate_url_id)
        url_map: dict[int, str] = {}
        if all_url_ids:
            url_map_rows = (await db.execute(
                select(CluUrl.id, CluUrl.url).where(CluUrl.id.in_(all_url_ids))
            )).all()
            url_map = {r.id: r.url for r in url_map_rows}

        for d in dup_rows:
            duplicates_raw.append({
                "winner_url":       url_map.get(d.winner_url_id or -1, ""),
                "duplicate_url":    url_map.get(d.duplicate_url_id or -1, ""),
                "similarity_score": d.similarity_score,
                "overlap_keywords": d.overlap_keywords or [],
                "winner_reason":    d.winner_reason,
                "action":           d.action,
                "dev_brief":        d.dev_brief,
            })

    # Convert decision ORM → plain dict for template
    decision_dict = None
    if decision:
        evidence = decision.evidence or []
        if isinstance(evidence, str):
            import json as _json
            try:
                evidence = _json.loads(evidence)
            except Exception:
                evidence = [evidence]
        decision_dict = {
            "verdict":    decision.verdict,
            "confidence": decision.confidence,
            "evidence":   evidence,
            "notes":      decision.notes,
            "brief_id":   decision.brief_id,
        }

    # Convert cluster ORM → plain dict
    cluster_dict = {
        "id":               cluster.id,
        "cluster_label":    cluster.cluster_label,
        "primary_url":      cluster.primary_url,
        "primary_keyword":  cluster.primary_keyword,
        "url_count":        cluster.url_count,
        "keyword_count":    cluster.keyword_count,
        "total_impressions": cluster.total_impressions,
        "avg_position":     cluster.avg_position,
    }

    return templates.TemplateResponse("clusteriq/cluster_detail.html", {
        "request":    request,
        "project":    project,
        "cluster":    cluster_dict,
        "members":    members,
        "decision":   decision_dict,
        "duplicates": duplicates_raw,
    })


@router.get("/clusteriq/{project_id}/duplicates", response_class=HTMLResponse)
async def clusteriq_duplicates_page(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Redirect to overview with duplicates tab pre-selected (full page in future sprint)."""
    project = await _get_project_or_404(project_id, db)

    from api.models.clusteriq import CluSerpData
    dup_rows = (await db.execute(
        select(CluDuplicate)
        .where(CluDuplicate.project_id == project_id)
        .order_by(CluDuplicate.similarity_score.desc().nullslast())
        .limit(200)
    )).scalars().all()

    url_ids = set()
    for d in dup_rows:
        if d.winner_url_id:   url_ids.add(d.winner_url_id)
        if d.duplicate_url_id: url_ids.add(d.duplicate_url_id)
    url_map: dict[int, str] = {}
    if url_ids:
        url_map_rows = (await db.execute(
            select(CluUrl.id, CluUrl.url).where(CluUrl.id.in_(url_ids))
        )).all()
        url_map = {r.id: r.url for r in url_map_rows}

    duplicates = []
    for d in dup_rows:
        duplicates.append({
            "id":               d.id,
            "winner_url":       url_map.get(d.winner_url_id or -1, ""),
            "duplicate_url":    url_map.get(d.duplicate_url_id or -1, ""),
            "similarity_score": d.similarity_score,
            "overlap_keywords": d.overlap_keywords or [],
            "winner_reason":    d.winner_reason,
            "action":           d.action,
            "dev_brief":        d.dev_brief,
        })

    return templates.TemplateResponse("clusteriq/duplicates.html", {
        "request":    request,
        "project":    project,
        "duplicates": duplicates,
    })


@router.get("/clusteriq/{project_id}/decisions", response_class=HTMLResponse)
async def clusteriq_decisions_page(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    project = await _get_project_or_404(project_id, db)

    import json as _json
    decision_rows = (await db.execute(
        select(
            CluDecision,
            CluCluster.cluster_label,
            CluCluster.primary_keyword,
            CluCluster.primary_url,
            CluCluster.total_impressions,
        )
        .join(CluCluster, CluCluster.id == CluDecision.cluster_id)
        .where(CluCluster.project_id == project_id)
        .order_by(CluDecision.verdict.asc(), CluDecision.confidence.desc().nullslast())
        .limit(2000)
    )).all()

    decisions = []
    for decision, cluster_label, primary_keyword, primary_url, total_impressions in decision_rows:
        evidence = decision.evidence or []
        if isinstance(evidence, str):
            try:
                evidence = _json.loads(evidence)
            except Exception:
                evidence = [evidence]
        decisions.append({
            "id":               decision.id,
            "cluster_id":       decision.cluster_id,
            "cluster_label":    cluster_label,
            "primary_keyword":  primary_keyword,
            "primary_url":      primary_url,
            "total_impressions": total_impressions or 0,
            "verdict":          decision.verdict,
            "confidence":       decision.confidence,
            "evidence":         evidence,
            "notes":            decision.notes,
        })

    return templates.TemplateResponse("clusteriq/decisions.html", {
        "request":    request,
        "project":    project,
        "decisions":  decisions,
    })


@router.get("/clusteriq/{project_id}/urls/{url_path:path}", response_class=HTMLResponse)
async def clusteriq_url_detail_page(
    request: Request,
    project_id: int,
    url_path: str,
    db: AsyncSession = Depends(get_db),
):
    from urllib.parse import unquote
    import json as _json

    project = await _get_project_or_404(project_id, db)
    url = unquote(url_path)

    url_row = (await db.execute(
        select(CluUrl).where(
            CluUrl.project_id == project_id,
            CluUrl.url == url,
        )
    )).scalar_one_or_none()
    if url_row is None:
        raise HTTPException(status_code=404, detail=f"URL not found in project {project_id}")

    # All clusters this URL belongs to, with cluster meta + decision
    membership_rows = (await db.execute(
        select(
            CluUrlCluster.cluster_id,
            CluUrlCluster.role,
            CluUrlCluster.rank_in_cluster,
            CluUrlCluster.impression_share_pct,
            CluUrlCluster.is_multi_topic,
            CluCluster.cluster_label,
            CluCluster.primary_url,
            CluCluster.primary_keyword,
            CluCluster.total_impressions,
            CluCluster.avg_position,
        )
        .join(CluCluster, CluCluster.id == CluUrlCluster.cluster_id)
        .where(
            CluUrlCluster.url_id == url_row.id,
            CluCluster.project_id == project_id,
        )
        .order_by(CluUrlCluster.impression_share_pct.desc().nullslast())
    )).all()

    cluster_ids = [r.cluster_id for r in membership_rows]
    decisions_map: dict[int, dict] = {}
    if cluster_ids:
        decision_rows = (await db.execute(
            select(CluDecision).where(CluDecision.cluster_id.in_(cluster_ids))
        )).scalars().all()
        for d in decision_rows:
            evidence = d.evidence or []
            if isinstance(evidence, str):
                try:
                    evidence = _json.loads(evidence)
                except Exception:
                    evidence = [evidence]
            decisions_map[d.cluster_id] = {
                "verdict":    d.verdict,
                "confidence": d.confidence,
                "evidence":   evidence,
            }

    memberships = []
    for r in membership_rows:
        memberships.append({
            "cluster_id":           r.cluster_id,
            "cluster_label":        r.cluster_label,
            "primary_url":          r.primary_url,
            "primary_keyword":      r.primary_keyword,
            "cluster_impressions":  r.total_impressions,
            "avg_position":         r.avg_position,
            "role":                 r.role,
            "rank_in_cluster":      r.rank_in_cluster,
            "impression_share_pct": r.impression_share_pct,
            "is_multi_topic":       r.is_multi_topic,
            "decision":             decisions_map.get(r.cluster_id),
        })

    # Aggregate GSC stats for this URL
    from api.models.clusteriq import CluSerpData
    gsc_agg = (await db.execute(
        select(
            func.sum(CluSerpData.impressions).label("total_imp"),
            func.sum(CluSerpData.clicks).label("total_clk"),
            func.avg(CluSerpData.position).label("avg_pos"),
            func.count(CluSerpData.keyword).label("kw_count"),
        )
        .where(
            CluSerpData.project_id == project_id,
            CluSerpData.url == url,
        )
    )).one()

    # Duplicate involvement — is this URL a winner or a loser?
    dup_as_winner = (await db.execute(
        select(CluDuplicate).where(
            CluDuplicate.project_id == project_id,
            CluDuplicate.winner_url_id == url_row.id,
        )
    )).scalars().all()

    dup_as_loser = (await db.execute(
        select(CluDuplicate).where(
            CluDuplicate.project_id == project_id,
            CluDuplicate.duplicate_url_id == url_row.id,
        )
    )).scalars().all()

    # Resolve URL strings for duplicate partners
    partner_ids: set[int] = set()
    for d in dup_as_winner:
        if d.duplicate_url_id: partner_ids.add(d.duplicate_url_id)
    for d in dup_as_loser:
        if d.winner_url_id: partner_ids.add(d.winner_url_id)
    partner_map: dict[int, str] = {}
    if partner_ids:
        p_rows = (await db.execute(
            select(CluUrl.id, CluUrl.url).where(CluUrl.id.in_(partner_ids))
        )).all()
        partner_map = {r.id: r.url for r in p_rows}

    duplicates = {
        "as_winner": [
            {
                "duplicate_url":    partner_map.get(d.duplicate_url_id or -1, ""),
                "similarity_score": d.similarity_score,
                "dev_brief":        d.dev_brief,
                "winner_reason":    d.winner_reason,
            }
            for d in dup_as_winner
        ],
        "as_loser": [
            {
                "winner_url":       partner_map.get(d.winner_url_id or -1, ""),
                "similarity_score": d.similarity_score,
                "dev_brief":        d.dev_brief,
                "winner_reason":    d.winner_reason,
            }
            for d in dup_as_loser
        ],
    }

    is_multi_topic = any(r.is_multi_topic for r in membership_rows)

    url_data = {
        "id":          url_row.id,
        "url":         url_row.url,
        "title":       url_row.title,
        "status_code": url_row.status_code,
        "is_indexable": url_row.is_indexable,
        "total_impressions": int(gsc_agg.total_imp or 0),
        "total_clicks":      int(gsc_agg.total_clk or 0),
        "avg_position":      round(float(gsc_agg.avg_pos), 1) if gsc_agg.avg_pos else None,
        "keyword_count":     int(gsc_agg.kw_count or 0),
    }

    return templates.TemplateResponse("clusteriq/url_detail.html", {
        "request":      request,
        "project":      project,
        "url_data":     url_data,
        "memberships":  memberships,
        "is_multi_topic": is_multi_topic,
        "duplicates":   duplicates,
    })


@router.get("/clusteriq/{project_id}/competitors/{competitor_domain}", response_class=HTMLResponse)
async def clusteriq_competitor_gap_page(
    request:           Request,
    project_id:        int,
    competitor_domain: str,
    db: AsyncSession = Depends(get_db),
):
    """Competitor gap analysis page for one domain."""
    import json as _json
    project = await _get_project_or_404(project_id, db)
    domain  = competitor_domain.lower().strip()

    # Job status
    job = (await db.execute(
        select(CluCompetitorJob).where(
            CluCompetitorJob.project_id        == project_id,
            CluCompetitorJob.competitor_domain == domain,
        ).order_by(CluCompetitorJob.created_at.desc())
    )).scalar_one_or_none()

    job_dict = job.to_dict() if job else None
    job_status = job.status if job else "not_started"

    gap_report: dict = {}
    if job_status == "done":
        from app.modules.clusteriq.services.competitor_analysis import CompetitorClusterAnalyzer
        gap_report = await CompetitorClusterAnalyzer().generate_gap_report(project_id, domain)

    return templates.TemplateResponse("clusteriq/competitor_gap.html", {
        "request":           request,
        "project":           project,
        "competitor_domain": domain,
        "job":               job_dict,
        "job_status":        job_status,
        "gap_report":        gap_report,
        "gap_report_json":   _json.dumps(gap_report, ensure_ascii=False),
    })
