"""
Compare and chart API routes for audit comparison and dashboard visualizations.
"""

import json
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, desc, asc
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import Audit, AuditResult, get_db

router = APIRouter(prefix="/api", tags=["compare"])


# ============================================================================
# DASHBOARD CHART DATA
# ============================================================================

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
    # Well-known top-level wrapper keys used by our audit prompts
    AUDIT_ROOT_KEYS = [
        "seo_audit", "geo_audit", "internal_linking_audit", "content_brief_audit",
        "accessibility_audit", "gdpr_audit", "social_media_audit", "page_speed_audit",
        "schema_markup_audit", "content_quality_audit", "technical_seo_audit",
        "competitor_analysis", "brand_sentiment_audit", "mobile_audit",
    ]
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
        raise HTTPException(status_code=400, detail="Need at least 2 audit IDs to compare")
    if len(ids) > 4:
        raise HTTPException(status_code=400, detail="Maximum 4 audits can be compared at once")

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
            raise HTTPException(status_code=404, detail=f"Audit {audit_id} not found")

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


# ============================================================================
# RE-RUN SINGLE PAGE
# ============================================================================

@router.post("/audits/{audit_id}/rerun/{result_id}")
async def rerun_single_page(
    audit_id: str,
    result_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Re-run analysis for a single page from an existing audit.
    """
    import os
    import sys
    from pathlib import Path
    
    # Get audit
    audit_result = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = audit_result.scalar_one_or_none()
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    
    if audit.status != "completed":
        raise HTTPException(status_code=400, detail="Can only re-run pages from completed audits")
    
    # Get the specific result
    result_query = await db.execute(
        select(AuditResult).where(
            AuditResult.audit_id == audit_id,
            AuditResult.id == result_id
        )
    )
    page_result = result_query.scalar_one_or_none()
    if not page_result:
        raise HTTPException(status_code=404, detail="Result not found")
    
    # Find the corresponding text file
    input_dir = os.path.join(audit.website, "input_llm")
    output_dir = os.path.join(audit.website, f"output_{audit.audit_type.lower()}")
    
    # The filename in the result might be the output JSON filename
    # We need to find the corresponding .txt input file
    base_name = page_result.filename
    if base_name.endswith('.json'):
        # Strip score prefix and .json extension to find the txt file
        import re
        txt_name = re.sub(r'^\d+_', '', base_name).replace('.json', '.txt')
    else:
        txt_name = base_name.replace('.json', '.txt') if not base_name.endswith('.txt') else base_name
    
    txt_path = os.path.join(input_dir, txt_name)
    
    if not os.path.exists(txt_path):
        # Try to find it with fuzzy matching
        if os.path.exists(input_dir):
            available = os.listdir(input_dir)
            # Try matching by URL part
            url_part = page_result.page_url.replace('https://', '').replace('http://', '')
            matches = [f for f in available if f.endswith('.txt') and url_part.replace('/', '_') in f]
            if matches:
                txt_path = os.path.join(input_dir, matches[0])
                txt_name = matches[0]
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"Source text file not found: {txt_name}. Available: {available[:5]}"
                )
        else:
            raise HTTPException(status_code=404, detail=f"Input directory not found: {input_dir}")
    
    # Read the text content
    with open(txt_path, 'r', encoding='utf-8') as f:
        page_text = f.read()
    
    # Check for research context
    research_dir = os.path.join(audit.website, "research")
    research_context = None
    if os.path.exists(research_dir):
        research_file = os.path.join(research_dir, txt_name.replace('.txt', '.research.json'))
        if os.path.exists(research_file):
            with open(research_file, 'r', encoding='utf-8') as f:
                research_data = json.load(f)
                research_context = "\n\n--- AI SEARCH RESEARCH CONTEXT ---\n"
                for r in research_data.get("results", []):
                    research_context += f"\nQuery: {r.get('query', '')}\n"
                    research_context += f"Response: {r.get('response', '')}\n"
                    research_context += f"Mentions brand: {r.get('mentions_brand', False)}\n"
    
    if research_context:
        page_text = page_text + research_context
    
    # Run analysis on single page
    try:
        from core.direct_analyzer import DirectAnalyzer
        
        analyzer = DirectAnalyzer(
            question_type=audit.audit_type,
            provider=audit.provider.upper(),
            model_name=audit.model,
            max_chars=30000
        )
        
        # Analyze single page
        result_text = await analyzer.analyze_single_page(page_text, txt_name)
        
        if result_text:
            # Parse JSON result
            from core.direct_analyzer import clean_json_response
            cleaned = clean_json_response(result_text)
            result_data = json.loads(cleaned)
            
            # Extract score
            score = None
            # Try all known YAML output_schema root keys
            for key in ['seo_audit', 'geo_audit', 'accessibility_audit',
                        'ux_content_audit', 'gdpr_audit', 'content_quality',
                        'brand_voice_audit', 'ecommerce_audit', 'translation_audit',
                        'internal_linking', 'competitive_positioning_audit',
                        'spelling_grammar_audit', 'readability_audit', 'technical_seo_audit',
                        'freshness_audit', 'local_seo_audit', 'security_content_audit',
                        'ai_overview_audit', 'score', 'overall_score']:
                if key in result_data:
                    val = result_data[key]
                    if isinstance(val, dict):
                        for score_key in ['overall_score', 'score']:
                            if score_key in val:
                                try:
                                    score = int(val[score_key])
                                except (ValueError, TypeError):
                                    continue
                                break
                    elif isinstance(val, (int, float)):
                        score = int(val)
                    elif isinstance(val, str):
                        try:
                            score = int(val)
                        except ValueError:
                            pass
                    if score is not None:
                        break
            
            # Determine classification
            classification = None
            if score is not None:
                if score >= 85:
                    classification = "excellent"
                elif score >= 70:
                    classification = "good"
                elif score >= 50:
                    classification = "needs_work"
                else:
                    classification = "poor"
            
            # Update result in database
            page_result.score = score
            page_result.classification = classification
            page_result.result_json = json.dumps(result_data)
            await db.commit()
            
            # Also save updated JSON file
            if score is not None:
                output_filename = f"{score:03d}_{txt_name.replace('.txt', '.json')}"
            else:
                output_filename = txt_name.replace('.txt', '.json')
            
            output_path = os.path.join(output_dir, output_filename)
            
            # Remove old file if it exists
            old_path = os.path.join(output_dir, page_result.filename)
            if os.path.exists(old_path) and old_path != output_path:
                os.remove(old_path)
            
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(result_data, f, ensure_ascii=False, indent=2)
            
            # Update filename in DB
            page_result.filename = output_filename
            await db.commit()
            
            # Recalculate audit average
            avg_query = select(func.avg(AuditResult.score)).where(
                AuditResult.audit_id == audit_id,
                AuditResult.score.isnot(None)
            )
            avg_result = await db.execute(avg_query)
            new_avg = avg_result.scalar()
            
            if new_avg is not None:
                audit.average_score = round(new_avg, 1)
                await db.commit()
            
            return {
                "status": "success",
                "page_url": page_result.page_url,
                "old_score": page_result.score,
                "new_score": score,
                "new_classification": classification,
                "result_json": result_data
            }
        else:
            raise HTTPException(status_code=500, detail="Analysis returned empty result")
    
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse LLM response: {str(e)}")
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Missing dependency: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Re-analysis failed: {str(e)}")
