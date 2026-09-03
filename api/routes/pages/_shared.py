"""Shared state for HTML page routes — templates instance, constants, helpers."""

from pathlib import Path
from typing import Optional

from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import AuditWeightConfig

# Templates directory: pages/_shared.py → pages/ → routes/ → api/ → project root/templates
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))

# ============================================================================
# COMPOSITE SCORE WEIGHTS — used by per-URL and per-site views
# ============================================================================
_COMPOSITE_WEIGHTS = {
    'SEO_AUDIT': 0.20,
    'GEO_AUDIT': 0.15,
    'CONTENT_QUALITY': 0.12,
    'TECHNICAL_SEO': 0.12,
    'UX_CONTENT': 0.10,
    'ACCESSIBILITY_AUDIT': 0.08,
    'BRAND_VOICE': 0.07,
    'LEGAL_GDPR': 0.06,
    'INTERNAL_LINKING': 0.05,
    'READABILITY_AUDIT': 0.05,
    'COMPETITOR_ANALYSIS': 0.04,
    'CONTENT_FRESHNESS': 0.04,
    'AI_OVERVIEW_OPTIMIZATION': 0.04,
    'SPELLING_GRAMMAR': 0.03,
    'TRANSLATION_QUALITY': 0.03,
    'LOCAL_SEO': 0.03,
    'SECURITY_CONTENT_AUDIT': 0.03,
    'E_COMMERCE': 0.03,
}

_AUDIT_TYPE_LABELS = {
    'SEO_AUDIT': 'SEO', 'GEO_AUDIT': 'GEO', 'CONTENT_QUALITY': 'Content Quality',
    'TECHNICAL_SEO': 'Technical SEO', 'UX_CONTENT': 'UX Content',
    'ACCESSIBILITY_AUDIT': 'Accessibility', 'BRAND_VOICE': 'Brand Voice',
    'LEGAL_GDPR': 'Legal / GDPR', 'INTERNAL_LINKING': 'Internal Linking',
    'READABILITY_AUDIT': 'Readability', 'COMPETITOR_ANALYSIS': 'Competitors',
    'CONTENT_FRESHNESS': 'Content Freshness', 'AI_OVERVIEW_OPTIMIZATION': 'AI Overview',
    'SPELLING_GRAMMAR': 'Spelling & Grammar', 'TRANSLATION_QUALITY': 'Translation',
    'LOCAL_SEO': 'Local SEO', 'SECURITY_CONTENT_AUDIT': 'Security Content',
    'E_COMMERCE': 'E-Commerce',
}


async def _load_weights(db: AsyncSession) -> dict:
    """Return effective weight dict — DB rows override hardcoded defaults.

    Falls back to _COMPOSITE_WEIGHTS when the audit_weight_configs table
    is empty (fresh install or after a reset).
    """
    result = await db.execute(select(AuditWeightConfig))
    rows = result.scalars().all()
    if not rows:
        return _COMPOSITE_WEIGHTS
    # Merge: start from defaults, overlay DB values
    merged = dict(_COMPOSITE_WEIGHTS)
    for row in rows:
        merged[row.audit_type] = row.weight
    return merged


def _compute_composite(scored_map: dict, weights: Optional[dict] = None) -> Optional[int]:
    """Compute weighted composite score from {audit_type: score} dict.

    Args:
        scored_map: Mapping of audit_type → score (None scores are skipped).
        weights: Override weight dict; defaults to _COMPOSITE_WEIGHTS.
    """
    w_map = weights if weights is not None else _COMPOSITE_WEIGHTS
    weighted_sum = 0.0
    weight_sum = 0.0
    for atype, score in scored_map.items():
        if score is not None:
            w = w_map.get(atype.upper(), 0.02)
            weighted_sum += score * w
            weight_sum += w
    if weight_sum == 0:
        return None
    return round(weighted_sum / weight_sum)


# ============================================================================
# SEO / GEO / AEO SCORECARD — Etapa 7 of docs/CONSOLIDATION_PLAN.md
# ============================================================================
# Classifies the 18 LLM audit types into two axes. AEO is deliberately NOT
# built from any audit type -- "apparition mesurata in raspunsuri reale prin
# Fan-Out" means it comes entirely from real Fan-Out/citation-tracking data,
# not an LLM's opinion about a page. This classification is a product
# judgment call, easy to adjust here if it doesn't match how the two axes
# should actually be split.
_SEO_AUDIT_TYPES = {
    'SEO_AUDIT', 'TECHNICAL_SEO', 'LOCAL_SEO', 'INTERNAL_LINKING',
    'E_COMMERCE', 'ACCESSIBILITY_AUDIT', 'SECURITY_CONTENT_AUDIT',
}
_GEO_AUDIT_TYPES = {
    'GEO_AUDIT', 'AI_OVERVIEW_OPTIMIZATION', 'CONTENT_QUALITY',
    'CONTENT_FRESHNESS', 'COMPETITOR_ANALYSIS', 'UX_CONTENT', 'BRAND_VOICE',
    'READABILITY_AUDIT', 'SPELLING_GRAMMAR', 'TRANSLATION_QUALITY', 'LEGAL_GDPR',
}


def _normalize_audit_type(atype: str) -> str:
    """Strip the "SINGLE_" prefix the /api/audits/single instant-audit
    endpoint adds (e.g. SINGLE_GEO_AUDIT, SINGLE_SEO_AUDIT -- real, common
    audit_type values, 48+ rows for SINGLE_GEO_AUDIT alone in production),
    so those results still land in the right SEO/GEO bucket instead of
    silently falling into neither. SINGLE_PAGE_GOD_MODE has no single-type
    equivalent and is left unclassified.
    """
    atype = atype.upper()
    if atype.startswith("SINGLE_") and atype != "SINGLE_PAGE_GOD_MODE":
        return atype[len("SINGLE_"):]
    return atype


def _bare_domain(raw: str) -> str:
    """Normalise a domain/URL/GSC site_url string to a bare lowercase domain.

    Real stored values are inconsistent across tables: Audit.website is
    sometimes a bare domain, sometimes a full page URL with path;
    GscProperty.site_url uses GSC's own "sc-domain:example.com" syntax;
    CitationTracker.website is a bare domain. All three need to compare
    equal for the same real site.
    """
    if not raw:
        return ""
    raw = raw.strip()
    if raw.lower().startswith("sc-domain:"):
        raw = raw[len("sc-domain:"):]
    if raw.startswith("http://") or raw.startswith("https://"):
        from urllib.parse import urlparse
        try:
            raw = urlparse(raw).netloc
        except Exception:
            pass
    from api.utils.domain import strip_www
    return strip_www(raw).lower()


def _gsc_position_to_score(avg_position: float) -> int:
    """Map an average GSC ranking position to a rough 0-100 scale.

    Heuristic, not a standard: position 1 -> 100, position ~10 -> ~60,
    position 20+ -> ~10. Clamped to [0, 100].
    """
    return max(0, min(100, round(110 - avg_position * 5)))


async def _gsc_seo_signal(db: AsyncSession, website: str, page_url: Optional[str] = None) -> Optional[dict]:
    """Return real GSC signal for a site (or one page within it), if a
    matching GscProperty exists. None if not tracked -- never guessed.
    """
    from api.models.analytics import GscProperty, GscPageRow
    from sqlalchemy import func

    target = _bare_domain(website)
    if not target:
        return None

    props = (await db.execute(select(GscProperty))).scalars().all()
    prop = next((p for p in props if _bare_domain(p.site_url) == target), None)
    if prop is None:
        return None

    q = select(func.avg(GscPageRow.position), func.avg(GscPageRow.ctr), func.sum(GscPageRow.clicks)) \
        .where(GscPageRow.property_id == prop.id, GscPageRow.position.isnot(None))
    if page_url:
        q = q.where(GscPageRow.page == page_url)
    row = (await db.execute(q)).first()
    if row is None or row[0] is None:
        return None

    avg_position, avg_ctr, total_clicks = row
    return {
        "avg_position": round(avg_position, 1),
        "avg_ctr": round(avg_ctr, 3) if avg_ctr is not None else None,
        "total_clicks": total_clicks or 0,
        "position_score": _gsc_position_to_score(avg_position),
    }


async def _aeo_signal(db: AsyncSession, website: str) -> Optional[dict]:
    """Return real Fan-Out/citation-tracking AEO signal for a site, if a
    CitationTracker is configured for it. None if not tracked.

    Reuses the exact mention_rate*0.4 + citation_rate*0.6 blend already
    used by api/routes/ai_visibility.py's global summary, scoped here to
    just this site's tracker(s) instead of every tracker in the account.
    """
    from api.models.content import CitationTracker, CitationScan

    target = _bare_domain(website)
    if not target:
        return None

    trackers = (await db.execute(
        select(CitationTracker).where(CitationTracker.is_active == 1)
    )).scalars().all()
    matching = [t for t in trackers if _bare_domain(t.website) == target]
    if not matching:
        return None

    mention_scores, citation_scores = [], []
    last_scan_at = None
    for tracker in matching:
        scan = (await db.execute(
            select(CitationScan)
            .where(CitationScan.tracker_id == tracker.id, CitationScan.status == "completed")
            .order_by(CitationScan.completed_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if scan is None:
            continue
        if scan.visibility_score is not None:
            mention_scores.append(scan.visibility_score)
        if scan.citation_rate is not None:
            citation_scores.append(scan.citation_rate)
        if scan.completed_at and (last_scan_at is None or scan.completed_at > last_scan_at):
            last_scan_at = scan.completed_at

    if not mention_scores and not citation_scores:
        return None

    mention_rate = (sum(mention_scores) / len(mention_scores)) if mention_scores else 0.0
    citation_rate = (sum(citation_scores) / len(citation_scores)) if citation_scores else 0.0
    return {
        "mention_rate": round(mention_rate, 1),
        "citation_rate": round(citation_rate, 1),
        "score": round(mention_rate * 0.4 + citation_rate * 0.6, 1),
        "last_scan_at": last_scan_at.strftime("%Y-%m-%d") if last_scan_at else None,
    }


async def compute_scorecard(
    db: AsyncSession,
    website: str,
    scored_map: dict,
    weights: dict,
    page_url: Optional[str] = None,
) -> dict:
    """
    SEO / GEO / AEO scorecard (Etapa 7): three separately-measured scores
    instead of one blended composite.
      - SEO:  weighted LLM scores for SEO-bucket audit types, blended with
              real GSC position data when a matching property exists.
      - GEO:  weighted LLM scores for GEO-bucket audit types (citability --
              no independent real-data source identified for this axis yet).
      - AEO:  real Fan-Out/citation-tracking mention+citation rate for this
              site. Not derived from any LLM audit type at all.
    Any axis with no data returns None rather than a guessed number.
    """
    # Normalise keys (strips the /api/audits/single "SINGLE_" prefix) so both
    # the bucket check AND _compute_composite's own weight lookup use the
    # same real audit type -- otherwise SINGLE_-prefixed rows would fall
    # back to the default 0.02 weight instead of their real one.
    normalized_map = {_normalize_audit_type(k): v for k, v in scored_map.items()}
    seo_llm = _compute_composite(
        {k: v for k, v in normalized_map.items() if k in _SEO_AUDIT_TYPES}, weights,
    )
    geo_llm = _compute_composite(
        {k: v for k, v in normalized_map.items() if k in _GEO_AUDIT_TYPES}, weights,
    )

    gsc = await _gsc_seo_signal(db, website, page_url)
    if seo_llm is not None and gsc is not None:
        seo_score = round(seo_llm * 0.6 + gsc["position_score"] * 0.4)
    else:
        seo_score = seo_llm if seo_llm is not None else (gsc["position_score"] if gsc else None)

    aeo = await _aeo_signal(db, website)

    return {
        "seo": {"score": seo_score, "llm_score": seo_llm, "gsc": gsc},
        "geo": {"score": geo_llm, "llm_score": geo_llm},
        "aeo": {"score": aeo["score"] if aeo else None, "detail": aeo},
    }


def _repair_guide_json(raw_json_str: str):
    """Parse guide_json and repair any {"raw": "..."} audit entries using json_repair."""
    import json
    try:
        gj = json.loads(raw_json_str)
        if isinstance(gj, dict) and "results" in gj:
            from json_repair import repair_json
            for key, val in gj["results"].items():
                if isinstance(val, dict) and "raw" in val and isinstance(val["raw"], str):
                    try:
                        repaired = repair_json(val["raw"], return_objects=True)
                        if isinstance(repaired, dict) and repaired:
                            gj["results"][key] = repaired
                    except Exception:
                        pass
        return gj
    except Exception:
        return None
