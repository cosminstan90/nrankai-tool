"""
Ad-hoc side-by-side comparison of 2-4 audits.

Etapa 5.2 of the consolidation (docs/CONSOLIDATION_PLAN.md): this file used
to also host 4 unrelated dashboard-chart endpoints (moved to
api/routes/dashboard_charts.py) and a single-page re-run endpoint (moved to
api/routes/audit_rerun.py) -- three unrelated feature groups bundled under
one router. URL paths are unchanged (still /api/compare) -- this is a file
reorganization, not a behavior change.
"""

import json
from typing import List
from fastapi import APIRouter, Depends, Query
from api.utils.errors import raise_not_found, raise_bad_request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import Audit, AuditResult, get_db
from api.utils.audit_json import AUDIT_ROOT_KEYS

router = APIRouter(prefix="/api", tags=["compare"])


# ============================================================================
# COMPARE AUDITS — helpers
# ============================================================================

def _extract_criteria_averages(results) -> dict:
    """
    Parse each page's result_json and extract numeric sub-scores.
    Returns {criterion_key: average_float} averaged across all pages.

    Handles the standard nested structure:
        { "seo_audit": { "overall_score": 75, "title_tag": {"score": 80, ...}, ... } }
    """
    # Keys that represent the overall score or non-criterion metadata
    SKIP_KEYS = {
        "overall_score", "score", "total_score", "grade", "page_url", "url",
        "word_count", "page_count", "summary", "recommendations",
        "priority_issues", "issues", "strengths", "weaknesses", "notes",
    }
    # Sub-keys that carry the numeric score within a criterion dict
    SCORE_SUB_KEYS = [
        "score", "overall_score", "ai_citation_likelihood", "clarity_score",
        "gdpr_compliance_score", "overall_quality_score", "grade", "rating",
    ]

    accumulator: dict = {}

    for r in results:
        if not r.result_json:
            continue
        try:
            data = json.loads(r.result_json)
        except (json.JSONDecodeError, TypeError):
            continue

        # Find the audit root dict (first matching well-known key)
        audit_root = None
        for root_key in AUDIT_ROOT_KEYS:
            if root_key in data and isinstance(data[root_key], dict):
                audit_root = data[root_key]
                break
        if audit_root is None:
            audit_root = data  # fallback: treat top-level as root

        for crit_key, crit_val in audit_root.items():
            if crit_key in SKIP_KEYS:
                continue
            score = None
            if isinstance(crit_val, dict):
                for sk in SCORE_SUB_KEYS:
                    if sk in crit_val and isinstance(crit_val[sk], (int, float)):
                        score = float(crit_val[sk])
                        break
            elif isinstance(crit_val, (int, float)):
                score = float(crit_val)

            # Only keep plausible 0-100 scores
            if score is not None and 0 <= score <= 100:
                accumulator.setdefault(crit_key, []).append(score)

    return {
        name: round(sum(vals) / len(vals), 1)
        for name, vals in accumulator.items()
        if vals
    }


# ============================================================================
# COMPARE AUDITS
# ============================================================================

@router.get("/compare")
async def compare_audits(
    audit_ids: str = Query(..., description="Comma-separated audit IDs to compare"),
    db: AsyncSession = Depends(get_db)
):
    """
    Compare two or more audits side by side.

    Returns score distributions, overlapping pages, per-page score deltas,
    criterion-level analysis, winner/anomaly info, and any data-quality warnings.

    Optimised to issue exactly 3 DB queries regardless of audit count:
      1. Load all Audit rows (IN clause)
      2. Load all AuditResult rows (IN clause)
      3. Aggregate distribution counts (GROUP BY)
    """
    from collections import defaultdict

    ids = [id.strip() for id in audit_ids.split(",") if id.strip()]

    if len(ids) < 2:
        raise_bad_request("Need at least 2 audit IDs to compare")
    if len(ids) > 4:
        raise_bad_request("Maximum 4 audits can be compared at once")

    warnings: List[str] = []

    # Detect duplicate IDs before hitting the DB
    if len(set(ids)) != len(ids):
        warnings.append(
            "The same audit was selected more than once — comparing an audit "
            "against itself will always show zero delta."
        )

    # ── 1. Load all Audit rows in one query ──────────────────────────────────
    audits_result = await db.execute(select(Audit).where(Audit.id.in_(ids)))
    audit_map = {a.id: a for a in audits_result.scalars().all()}

    for audit_id in ids:
        if audit_id not in audit_map:
            raise_not_found("Audit", audit_id)

    # Preserve the caller's order
    ordered_audits = [audit_map[aid] for aid in ids]

    # Cross-type warning (don't block — user may intentionally compare types)
    audit_types_seen = list(dict.fromkeys(a.audit_type for a in ordered_audits))
    if len(audit_types_seen) > 1:
        warnings.append(
            f"Audits span different types ({', '.join(audit_types_seen)}). "
            "Criterion-level comparison is only meaningful between audits of the same type."
        )

    # ── 2. Load all AuditResult rows in one query ────────────────────────────
    results_query = await db.execute(
        select(AuditResult).where(AuditResult.audit_id.in_(ids))
    )
    results_by_audit: dict = defaultdict(list)
    for r in results_query.scalars().all():
        results_by_audit[r.audit_id].append(r)

    # ── 3. Classification distribution via SQL GROUP BY (one round-trip) ─────
    dist_query = await db.execute(
        select(
            AuditResult.audit_id,
            AuditResult.classification,
            func.count(AuditResult.id).label("cnt"),
        )
        .where(
            AuditResult.audit_id.in_(ids),
            AuditResult.classification.isnot(None),
        )
        .group_by(AuditResult.audit_id, AuditResult.classification)
    )
    dist_by_audit: dict = {
        aid: {"excellent": 0, "good": 0, "needs_work": 0, "poor": 0} for aid in ids
    }
    for row in dist_query.fetchall():
        if row.classification in dist_by_audit[row.audit_id]:
            dist_by_audit[row.audit_id][row.classification] = row.cnt

    # ── 4. Build per-audit data ───────────────────────────────────────────────
    audits_data = []
    for audit in ordered_audits:
        results = results_by_audit[audit.id]
        page_scores = {
            r.page_url: {"score": r.score, "classification": r.classification}
            for r in results
        }
        criteria_averages = _extract_criteria_averages(results)
        audits_data.append({
            "id": audit.id,
            "website": audit.website,
            "audit_type": audit.audit_type,
            "provider": audit.provider,
            "model": audit.model,
            "average_score": audit.average_score,
            "total_pages": audit.pages_analyzed,
            "completed_at": audit.completed_at.isoformat() if audit.completed_at else None,
            "distribution": dist_by_audit[audit.id],
            "page_scores": page_scores,
            "criteria_averages": criteria_averages,
        })

    # ── 5. Find overlapping pages ─────────────────────────────────────────────
    all_page_sets = [set(a["page_scores"].keys()) for a in audits_data]
    common_pages = all_page_sets[0]
    for ps in all_page_sets[1:]:
        common_pages = common_pages.intersection(ps)

    if not common_pages:
        warnings.append(
            "No common pages found — the selected audits analysed different page sets. "
            "For page-by-page comparison, run all audits on the same website."
        )

    # ── 6. Build page comparisons with delta + anomaly flag ───────────────────
    page_comparisons = []
    for page_url in sorted(common_pages):
        scores = [
            audits_data[i]["page_scores"].get(page_url, {}).get("score")
            for i in range(len(audits_data))
        ]
        valid = [s for s in scores if s is not None]
        delta = round(max(valid) - min(valid), 1) if len(valid) >= 2 else 0
        page_comparisons.append({
            "page_url": page_url,
            "scores": scores,
            "delta": delta,
            "anomaly": delta >= 30,   # Flag pages with surprising divergence
        })

    page_comparisons.sort(key=lambda c: c["delta"], reverse=True)
    anomaly_count = sum(1 for c in page_comparisons if c["anomaly"])

    # ── 7. Per-criterion cross-audit comparison with winner/delta ─────────────
    per_audit_criteria = [a["criteria_averages"] for a in audits_data]
    common_criteria: dict = {}

    if len(per_audit_criteria) >= 2:
        crit_sets = [set(c.keys()) for c in per_audit_criteria]
        common_crit_keys = crit_sets[0]
        for s in crit_sets[1:]:
            common_crit_keys = common_crit_keys.intersection(s)

        for key in sorted(common_crit_keys):
            scores = [c.get(key) for c in per_audit_criteria]
            valid = [s for s in scores if s is not None]
            winner_idx = scores.index(max(valid)) if valid else None
            delta = round(max(valid) - min(valid), 1) if len(valid) >= 2 else 0
            common_criteria[key] = {
                "scores": scores,
                "winner": winner_idx,   # index of best-performing audit
                "delta": delta,
            }

    avg_page_delta = (
        round(sum(c["delta"] for c in page_comparisons) / len(page_comparisons), 1)
        if page_comparisons else 0
    )

    return {
        "audits": [
            {k: v for k, v in a.items() if k not in ("page_scores", "criteria_averages")}
            for a in audits_data
        ],
        "warnings": warnings,
        "common_pages_count": len(common_pages),
        "page_comparisons": page_comparisons[:100],   # top 100 by delta
        "anomaly_count": anomaly_count,
        "criteria": {
            "per_audit": per_audit_criteria,
            "common_criteria": common_criteria,
        },
        "summary": {"avg_delta": avg_page_delta},
    }
