"""
ContentIQ API Router
====================
Prefix: /api/contentiq
All background tasks open their own AsyncSessionLocal session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.limiter import limiter
from api.models._base import AsyncSessionLocal
from api.models.contentiq import CiqAudit, CiqCompetitor, CiqGscToken, CiqPage
from api.models.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contentiq", tags=["contentiq"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_audit_or_404(audit_id: int, db: AsyncSession) -> CiqAudit:
    row = (await db.execute(select(CiqAudit).where(CiqAudit.id == audit_id))).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Audit not found")
    return row


# ---------------------------------------------------------------------------
# Background task: crawl + score
# ---------------------------------------------------------------------------

async def _bg_crawl_and_score(audit_id: int, sitemap_url: str, max_urls: int, concurrency: int) -> None:
    from api.workers.contentiq.crawler import crawl_audit
    from api.workers.contentiq.verdict import score_and_verdict

    async with AsyncSessionLocal() as db:
        try:
            # 1. Crawl
            await crawl_audit(audit_id, sitemap_url, db, max_urls, concurrency)

            # 2. Fetch crawled pages
            pages = (await db.execute(
                select(CiqPage).where(
                    CiqPage.audit_id == audit_id,
                    CiqPage.crawled_at.isnot(None),
                )
            )).scalars().all()

            # 3. Score each page
            now = datetime.now(timezone.utc)
            for page in pages:
                page_dict = {
                    "id":               page.id,
                    "url":              page.url,
                    "title":            page.title,
                    "h1":               page.h1,
                    "meta_description": page.meta_description,
                    "canonical":        page.canonical,
                    "word_count":       page.word_count,
                    "last_modified":    page.last_modified,
                    "status_code":      page.status_code,
                    "ahrefs_traffic":   page.ahrefs_traffic,
                    "ahrefs_keywords":  page.ahrefs_keywords,
                    "ahrefs_backlinks": page.ahrefs_backlinks,
                    "ahrefs_dr":        page.ahrefs_dr,
                    "gsc_clicks":       page.gsc_clicks,
                    "gsc_impressions":  page.gsc_impressions,
                    "gsc_ctr":          page.gsc_ctr,
                    "gsc_position":     page.gsc_position,
                    "score_freshness":  page.score_freshness,
                    "score_geo":        page.score_geo,
                    "score_eeat":       page.score_eeat,
                    "score_seo_health": page.score_seo_health,
                    "score_total":      page.score_total,
                    "freshness_reason":  page.freshness_reason,
                    "geo_reason":        page.geo_reason,
                    "eeat_reason":       page.eeat_reason,
                    "seo_health_reason": page.seo_health_reason,
                    "verdict":          page.verdict,
                    "verdict_reason":   page.verdict_reason,
                    "brief_generated":  page.brief_generated,
                    "competitor_gap":   page.competitor_gap,
                }

                try:
                    scored = score_and_verdict(page_dict)
                except Exception as exc:
                    logger.error("[CIQ] score_and_verdict failed for page %d: %s", page.id, exc)
                    continue

                page.score_freshness   = scored.get("score_freshness")
                page.score_geo         = scored.get("score_geo")
                page.score_eeat        = scored.get("score_eeat")
                page.score_seo_health  = scored.get("score_seo_health")
                page.score_total       = scored.get("score_total")
                page.freshness_reason  = scored.get("freshness_reason")
                page.geo_reason        = scored.get("geo_reason")
                page.eeat_reason       = scored.get("eeat_reason")
                page.seo_health_reason = scored.get("seo_health_reason")
                page.verdict           = scored.get("verdict")
                page.verdict_reason    = scored.get("verdict_reason")
                page.scored_at         = now

            await db.commit()

            # 4. Mark audit done
            audit = await _get_audit_or_404(audit_id, db)
            audit.status      = "done"
            audit.scored_urls = len(pages)
            audit.finished_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info("[CIQ] Audit %d complete — %d pages scored", audit_id, len(pages))

        except Exception as exc:
            logger.error("[CIQ] Audit %d pipeline failed: %s", audit_id, exc)
            try:
                audit = await _get_audit_or_404(audit_id, db)
                audit.status = "failed"
                await db.commit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Background task: Ahrefs sync
# ---------------------------------------------------------------------------

async def _bg_ahrefs_sync(audit_id: int) -> None:
    from api.workers.contentiq.ahrefs import get_client

    async with AsyncSessionLocal() as db:
        try:
            pages = (await db.execute(
                select(CiqPage).where(CiqPage.audit_id == audit_id)
            )).scalars().all()

            if not pages:
                return

            client = get_client()
            urls   = [p.url for p in pages]
            metrics = await client.batch_url_metrics(urls)

            for page in pages:
                m = metrics.get(page.url)
                if not m:
                    continue
                page.ahrefs_traffic   = m.get("traffic")
                page.ahrefs_keywords  = m.get("keywords")
                page.ahrefs_backlinks = m.get("backlinks")
                page.ahrefs_dr        = m.get("dr")

            await db.commit()
            logger.info("[CIQ] Ahrefs sync complete for audit %d (%d pages)", audit_id, len(pages))
        except Exception as exc:
            logger.error("[CIQ] Ahrefs sync failed for audit %d: %s", audit_id, exc)


# ---------------------------------------------------------------------------
# Background task: GSC sync
# ---------------------------------------------------------------------------

async def _bg_gsc_sync(audit_id: int) -> None:
    from api.workers.contentiq.gsc import GSCAuthError, get_page_metrics, load_tokens

    async with AsyncSessionLocal() as db:
        try:
            token_row = await load_tokens(audit_id, db)
            if not token_row or not token_row.access_token:
                logger.error("[CIQ] GSC sync: no tokens for audit %d", audit_id)
                return

            pages = (await db.execute(
                select(CiqPage).where(CiqPage.audit_id == audit_id)
            )).scalars().all()

            if not pages:
                return

            urls    = [p.url for p in pages]
            metrics = await get_page_metrics(
                token_row.access_token,
                token_row.property_url,
                urls,
                date_range_days=90,
            )

            for page in pages:
                m = metrics.get(page.url) or metrics.get(page.url.rstrip("/"))
                if not m:
                    continue
                page.gsc_clicks      = m.get("clicks")
                page.gsc_impressions = m.get("impressions")
                page.gsc_ctr         = m.get("ctr")
                page.gsc_position    = m.get("position")

            await db.commit()
            logger.info("[CIQ] GSC sync complete for audit %d", audit_id)
        except GSCAuthError as exc:
            logger.error("[CIQ] GSC auth error for audit %d: %s", audit_id, exc)
        except Exception as exc:
            logger.error("[CIQ] GSC sync failed for audit %d: %s", audit_id, exc)


# ---------------------------------------------------------------------------
# Background task: generate briefs
# ---------------------------------------------------------------------------

async def _bg_generate_briefs(audit_id: int, audit_domain: str, concurrency: int) -> None:
    from api.workers.contentiq.brief import batch_generate_briefs

    async with AsyncSessionLocal() as db:
        try:
            pages = (await db.execute(
                select(CiqPage).where(
                    CiqPage.audit_id == audit_id,
                    CiqPage.verdict.in_(["UPDATE", "CONSOLIDATE"]),
                    CiqPage.brief_generated.is_(False),
                )
            )).scalars().all()

            page_dicts = [p.to_dict() for p in pages]
            count = await batch_generate_briefs(page_dicts, audit_domain, audit_id, db, concurrency)
            logger.info("[CIQ] Generated %d briefs for audit %d", count, audit_id)
        except Exception as exc:
            logger.error("[CIQ] Brief generation failed for audit %d: %s", audit_id, exc)


# ---------------------------------------------------------------------------
# Background task: rescore (no re-crawl)
# ---------------------------------------------------------------------------

async def _bg_rescore(audit_id: int) -> None:
    from api.workers.contentiq.verdict import score_and_verdict

    async with AsyncSessionLocal() as db:
        try:
            pages = (await db.execute(
                select(CiqPage).where(
                    CiqPage.audit_id == audit_id,
                    CiqPage.crawled_at.isnot(None),
                )
            )).scalars().all()

            now = datetime.now(timezone.utc)
            for page in pages:
                page_dict = page.to_dict()
                try:
                    scored = score_and_verdict(page_dict)
                except Exception as exc:
                    logger.error("[CIQ] rescore failed for page %d: %s", page.id, exc)
                    continue

                page.score_freshness   = scored.get("score_freshness")
                page.score_geo         = scored.get("score_geo")
                page.score_eeat        = scored.get("score_eeat")
                page.score_seo_health  = scored.get("score_seo_health")
                page.score_total       = scored.get("score_total")
                page.freshness_reason  = scored.get("freshness_reason")
                page.geo_reason        = scored.get("geo_reason")
                page.eeat_reason       = scored.get("eeat_reason")
                page.seo_health_reason = scored.get("seo_health_reason")
                page.verdict           = scored.get("verdict")
                page.verdict_reason    = scored.get("verdict_reason")
                page.scored_at         = now

            await db.commit()
            logger.info("[CIQ] Rescore complete for audit %d (%d pages)", audit_id, len(pages))
        except Exception as exc:
            logger.error("[CIQ] Rescore failed for audit %d: %s", audit_id, exc)


# ===========================================================================
# ROUTES
# ===========================================================================

# ---------------------------------------------------------------------------
# Audits
# ---------------------------------------------------------------------------

@router.get("/audits")
async def list_audits(db: AsyncSession = Depends(get_db)):
    """List all audits, newest first."""
    audits = (await db.execute(
        select(CiqAudit).order_by(CiqAudit.created_at.desc())
    )).scalars().all()

    result = []
    for audit in audits:
        d = audit.to_dict()
        page_count = (await db.execute(
            select(func.count(CiqPage.id)).where(CiqPage.audit_id == audit.id)
        )).scalar() or 0
        d["page_count"] = page_count
        result.append(d)

    return result


@router.post("/audits")
async def create_audit(body: dict, db: AsyncSession = Depends(get_db)):
    """Create a new ContentIQ audit."""
    label        = body.get("label", "").strip()
    domain       = body.get("domain", "").strip()
    sitemap_url  = body.get("sitemap_url")
    triggered_by = body.get("triggered_by", "manual")

    if not label or not domain:
        raise HTTPException(status_code=422, detail="label and domain are required")

    audit = CiqAudit(
        label        = label,
        domain       = domain,
        sitemap_url  = sitemap_url,
        triggered_by = triggered_by,
        status       = "pending",
        created_at   = datetime.now(timezone.utc),
    )
    db.add(audit)
    await db.commit()
    await db.refresh(audit)
    return audit.to_dict()


@router.get("/audits/{audit_id}")
async def get_audit(audit_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single audit with its competitors."""
    audit = await _get_audit_or_404(audit_id, db)

    competitors = (await db.execute(
        select(CiqCompetitor).where(CiqCompetitor.audit_id == audit_id)
    )).scalars().all()

    d = audit.to_dict()
    d["competitors"] = [c.to_dict() for c in competitors]
    return d


@router.delete("/audits/{audit_id}")
async def delete_audit(audit_id: int, db: AsyncSession = Depends(get_db)):
    """Delete an audit and cascade-delete all related rows."""
    await _get_audit_or_404(audit_id, db)
    await db.execute(delete(CiqAudit).where(CiqAudit.id == audit_id))
    await db.commit()
    return {"ok": True}


@router.post("/audits/{audit_id}/start")
async def start_audit(
    audit_id: int,
    body: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Start crawl + scoring pipeline in background."""
    audit = await _get_audit_or_404(audit_id, db)

    max_urls    = int(body.get("max_urls",    500))
    concurrency = int(body.get("concurrency", 10))

    sitemap_url = audit.sitemap_url
    if not sitemap_url:
        raise HTTPException(status_code=422, detail="Audit has no sitemap_url configured")

    audit.status = "crawling"
    await db.commit()

    background_tasks.add_task(_bg_crawl_and_score, audit_id, sitemap_url, max_urls, concurrency)
    return {"ok": True, "status": "crawling"}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("/audits/{audit_id}/pages")
async def list_pages(
    audit_id: int,
    page:     int            = Query(1,    ge=1),
    per_page: int            = Query(50,   ge=1, le=200),
    verdict:  Optional[str]  = Query(None),
    search:   Optional[str]  = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Paginated list of pages for an audit."""
    await _get_audit_or_404(audit_id, db)

    stmt = select(CiqPage).where(CiqPage.audit_id == audit_id)

    if verdict:
        stmt = stmt.where(CiqPage.verdict == verdict.upper())
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            CiqPage.url.ilike(like) | CiqPage.title.ilike(like)
        )

    total = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar() or 0

    offset = (page - 1) * per_page
    pages  = (await db.execute(
        stmt.order_by(CiqPage.score_total.desc().nullslast()).offset(offset).limit(per_page)
    )).scalars().all()

    return {
        "pages":    [p.to_dict() for p in pages],
        "total":    total,
        "page":     page,
        "per_page": per_page,
    }


@router.get("/audits/{audit_id}/pages/{page_id}")
async def get_page(audit_id: int, page_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single page detail."""
    row = (await db.execute(
        select(CiqPage).where(CiqPage.id == page_id, CiqPage.audit_id == audit_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Page not found")
    return row.to_dict()


@router.put("/audits/{audit_id}/pages/{page_id}")
async def update_page(audit_id: int, page_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    """Override page fields (verdict, notes, brief, competitor_gap)."""
    row = (await db.execute(
        select(CiqPage).where(CiqPage.id == page_id, CiqPage.audit_id == audit_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Page not found")

    allowed_fields = {"verdict", "verdict_reason", "brief_content", "brief_generated", "competitor_gap"}
    for field, value in body.items():
        if field in allowed_fields and hasattr(row, field):
            setattr(row, field, value)

    await db.commit()
    await db.refresh(row)
    return row.to_dict()


# ---------------------------------------------------------------------------
# Ahrefs
# ---------------------------------------------------------------------------

@router.post("/audits/{audit_id}/ahrefs")
async def sync_ahrefs(
    audit_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Queue Ahrefs metrics pull for all pages."""
    await _get_audit_or_404(audit_id, db)

    page_count = (await db.execute(
        select(func.count(CiqPage.id)).where(CiqPage.audit_id == audit_id)
    )).scalar() or 0

    background_tasks.add_task(_bg_ahrefs_sync, audit_id)
    return {"ok": True, "queued": page_count}


# ---------------------------------------------------------------------------
# GSC
# ---------------------------------------------------------------------------

@router.get("/gsc/oauth-url")
async def gsc_oauth_url(
    audit_id:     int = Query(...),
    redirect_uri: str = Query(...),
):
    """Return the Google OAuth URL for GSC authorisation."""
    from api.workers.contentiq.gsc import get_oauth_url
    state = str(audit_id)
    url   = get_oauth_url(state=state)
    return {"url": url}


@router.post("/audits/{audit_id}/gsc")
async def save_gsc_tokens(audit_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    """Exchange GSC OAuth code and persist tokens."""
    from api.workers.contentiq.gsc import exchange_code, save_tokens

    await _get_audit_or_404(audit_id, db)

    code         = body.get("code", "")
    redirect_uri = body.get("redirect_uri", "")
    property_url = body.get("property_url", "")

    if not code:
        raise HTTPException(status_code=422, detail="code is required")

    try:
        tokens = await exchange_code(code)
    except Exception as exc:
        logger.error("[CIQ] GSC exchange_code failed: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to exchange GSC code")

    await save_tokens(audit_id, tokens, property_url, db)
    return {"ok": True}


@router.post("/audits/{audit_id}/gsc/sync")
async def sync_gsc(
    audit_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Queue GSC metrics sync for all pages."""
    await _get_audit_or_404(audit_id, db)

    page_count = (await db.execute(
        select(func.count(CiqPage.id)).where(CiqPage.audit_id == audit_id)
    )).scalar() or 0

    background_tasks.add_task(_bg_gsc_sync, audit_id)
    return {"ok": True, "queued": page_count}


# ---------------------------------------------------------------------------
# Briefs
# ---------------------------------------------------------------------------

@router.post("/audits/{audit_id}/briefs")
async def generate_briefs(
    audit_id: int,
    body: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Queue brief generation for UPDATE/CONSOLIDATE pages without briefs."""
    audit       = await _get_audit_or_404(audit_id, db)
    concurrency = int(body.get("concurrency", 3))

    background_tasks.add_task(_bg_generate_briefs, audit_id, audit.domain, concurrency)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@router.get("/audits/{audit_id}/export")
async def export_audit(audit_id: int, db: AsyncSession = Depends(get_db)):
    """Download the audit as an Excel file."""
    from api.workers.contentiq.export import export_audit_excel

    audit = await _get_audit_or_404(audit_id, db)
    pages = (await db.execute(
        select(CiqPage).where(CiqPage.audit_id == audit_id).order_by(CiqPage.score_total.desc().nullslast())
    )).scalars().all()

    try:
        xlsx_bytes = export_audit_excel(audit, pages)
    except Exception as exc:
        logger.error("[CIQ] Excel export failed for audit %d: %s", audit_id, exc)
        raise HTTPException(status_code=500, detail="Export failed")

    return Response(
        content     = xlsx_bytes,
        media_type  = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers     = {"Content-Disposition": f"attachment; filename=contentiq_{audit_id}.xlsx"},
    )


# ---------------------------------------------------------------------------
# Competitors
# ---------------------------------------------------------------------------

@router.post("/audits/{audit_id}/competitors")
async def add_competitor(audit_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    """Add a competitor domain to an audit."""
    await _get_audit_or_404(audit_id, db)

    domain = body.get("domain", "").strip()
    label  = body.get("label")

    if not domain:
        raise HTTPException(status_code=422, detail="domain is required")

    competitor = CiqCompetitor(
        audit_id = audit_id,
        domain   = domain,
        label    = label,
        added_at = datetime.now(timezone.utc),
    )
    db.add(competitor)
    await db.commit()
    await db.refresh(competitor)
    return competitor.to_dict()


@router.delete("/competitors/{competitor_id}")
async def delete_competitor(competitor_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a competitor by ID."""
    row = (await db.execute(
        select(CiqCompetitor).where(CiqCompetitor.id == competitor_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Competitor not found")

    await db.execute(delete(CiqCompetitor).where(CiqCompetitor.id == competitor_id))
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Rescore
# ---------------------------------------------------------------------------

@router.post("/audits/{audit_id}/rescore")
async def rescore_audit(
    audit_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Re-run scoring on all crawled pages without re-crawling."""
    await _get_audit_or_404(audit_id, db)
    background_tasks.add_task(_bg_rescore, audit_id)
    return {"ok": True}
