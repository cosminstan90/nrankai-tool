"""
Competitor Gap Analysis — Detailed per-criterion competitive comparison.

Analyzes target audit vs competitors at the individual criterion level
(meta descriptions, schema markup, content quality, etc.) to identify
specific gaps and generate actionable recommendations.
"""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import (
    AsyncSessionLocal,
    Audit,
    AuditResult,
    BenchmarkProject,
    CompetitorGapAnalysis
)
from api.provider_registry import get_default_model, calculate_cost
from api.routes.summary import call_llm_for_summary, clean_json_response

router = APIRouter(prefix="/api/gap-analysis", tags=["gap_analysis"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class GenerateGapAnalysisRequest(BaseModel):
    """Request to generate a new gap analysis."""
    name: str = Field(..., description="Gap analysis name")
    target_audit_id: str = Field(..., description="Target audit ID (client)")
    competitor_audit_ids: List[str] = Field(..., min_length=1, max_length=3, description="Competitor audit IDs (1-3)")
    benchmark_id: Optional[str] = Field(None, description="Optional benchmark project ID")
    provider: str = Field("anthropic", description="LLM provider")
    model: Optional[str] = Field(None, description="LLM model (uses provider default if not set)")


# ============================================================================
# CRITERIA EXTRACTION LOGIC (Rules-based pre-processing)
# ============================================================================

# Audit-type groups used to dispatch to the right extractor
_SEO_TYPES   = {"SEO_AUDIT", "TECHNICAL_SEO", "LOCAL_SEO"}
_GEO_TYPES   = {"GEO_AUDIT", "AI_OVERVIEW_OPTIMIZATION"}
_CONTENT_TYPES = {
    "CONTENT_QUALITY", "READABILITY_AUDIT", "CONTENT_FRESHNESS",
    "SPELLING_GRAMMAR", "BRAND_VOICE", "TRANSLATION_QUALITY",
}

# Per-audit-type human-readable criterion labels used in build_comparison_matrix
_SEO_CRITERION_NAMES = {
    "meta_titles":        "Meta Titles",
    "meta_descriptions":  "Meta Descriptions",
    "headings_structure": "Heading Structure",
    "schema_markup":      "Schema Markup",
    "internal_links":     "Internal Linking",
    "content_length":     "Content Length",
    "faq_content":        "FAQ Content",
    "image_optimization": "Image Optimization",
    "readability":        "Readability",
    "keyword_usage":      "Keyword Usage",
}

_GEO_CRITERION_NAMES = {
    "meta_titles":        "Citation Probability",
    "meta_descriptions":  "Factual Density",
    "headings_structure": "Structure Quality",
    "schema_markup":      "Schema Signals",
    "internal_links":     "Entity Richness",
    "content_length":     "Content Depth",
    "faq_content":        "FAQ / Q&A Coverage",
    "image_optimization": "Authority Score",
    "readability":        "Overall GEO Score",
    "keyword_usage":      "Quotable Statements",
}


def _empty_criteria() -> Dict[str, Any]:
    """Return a blank criteria aggregation dict."""
    return {
        "meta_titles":        {"present": 0, "optimized": 0, "total": 0, "avg_length": []},
        "meta_descriptions":  {"present": 0, "optimized": 0, "total": 0, "avg_length": []},
        "headings_structure": {"correct": 0, "issues": 0, "total": 0},
        "schema_markup":      {"present": 0, "total": 0, "types": []},
        "internal_links":     {"total_links": 0, "page_count": 0, "avg": 0},
        "content_length":     {"total_words": 0, "thin_pages": 0, "page_count": 0, "avg": 0},
        "faq_content":        {"present": 0, "total": 0},
        "image_optimization": {"optimized": 0, "total": 0},
        "readability":        {"scores": [], "avg": 0},
        "keyword_usage":      {"optimized": 0, "total": 0},
    }


def _finalize_averages(criteria: Dict[str, Any]) -> None:
    """Compute avg fields in-place from accumulated totals."""
    if criteria["internal_links"]["page_count"] > 0:
        criteria["internal_links"]["avg"] = round(
            criteria["internal_links"]["total_links"] / criteria["internal_links"]["page_count"], 1
        )
    if criteria["content_length"]["page_count"] > 0:
        criteria["content_length"]["avg"] = round(
            criteria["content_length"]["total_words"] / criteria["content_length"]["page_count"], 0
        )
    if criteria["readability"]["scores"]:
        criteria["readability"]["avg"] = round(
            sum(criteria["readability"]["scores"]) / len(criteria["readability"]["scores"]), 1
        )


def _extract_seo_criteria_scores(results: List[AuditResult]) -> Dict[str, Any]:
    """Extract per-criterion performance from SEO / Technical-SEO audit results."""
    criteria = _empty_criteria()

    for result in results:
        if not result.result_json:
            continue
        try:
            rj = json.loads(result.result_json) if isinstance(result.result_json, str) else result.result_json
            if not rj:
                continue

            # Meta titles
            criteria["meta_titles"]["total"] += 1
            if rj.get("meta_title"):
                criteria["meta_titles"]["present"] += 1
                title_len = len(rj.get("meta_title", ""))
                criteria["meta_titles"]["avg_length"].append(title_len)
                if 30 <= title_len <= 60:
                    criteria["meta_titles"]["optimized"] += 1

            # Meta descriptions
            criteria["meta_descriptions"]["total"] += 1
            if rj.get("meta_description"):
                criteria["meta_descriptions"]["present"] += 1
                desc_len = len(rj.get("meta_description", ""))
                criteria["meta_descriptions"]["avg_length"].append(desc_len)
                if 120 <= desc_len <= 160:
                    criteria["meta_descriptions"]["optimized"] += 1

            # Headings structure
            criteria["headings_structure"]["total"] += 1
            headings_issues = [
                issue for issue in rj.get("issues", [])
                if isinstance(issue, str) and "heading" in issue.lower()
            ]
            if headings_issues:
                criteria["headings_structure"]["issues"] += 1
            else:
                criteria["headings_structure"]["correct"] += 1

            # Schema markup
            criteria["schema_markup"]["total"] += 1
            schema_data = rj.get("schema_markup", [])
            if schema_data:
                criteria["schema_markup"]["present"] += 1
                for schema in schema_data:
                    if isinstance(schema, dict) and schema.get("@type"):
                        criteria["schema_markup"]["types"].append(schema.get("@type"))

            # Internal links
            internal_links = rj.get("internal_links", 0)
            if isinstance(internal_links, int):
                criteria["internal_links"]["total_links"] += internal_links
                criteria["internal_links"]["page_count"] += 1

            # Content length
            content = rj.get("content", "")
            if content:
                word_count = len(content.split())
                criteria["content_length"]["total_words"] += word_count
                criteria["content_length"]["page_count"] += 1
                if word_count < 300:
                    criteria["content_length"]["thin_pages"] += 1

            # FAQ content
            criteria["faq_content"]["total"] += 1
            if rj.get("has_faq") or any("faq" in str(s).lower() for s in rj.get("schema_markup", [])):
                criteria["faq_content"]["present"] += 1

            # Image optimization
            images = rj.get("images", [])
            criteria["image_optimization"]["total"] += len(images)
            criteria["image_optimization"]["optimized"] += sum(
                1 for img in images if isinstance(img, dict) and img.get("has_alt")
            )

            # Readability
            readability_score = rj.get("readability_score")
            if readability_score and isinstance(readability_score, (int, float)):
                criteria["readability"]["scores"].append(readability_score)

            # Keyword usage
            criteria["keyword_usage"]["total"] += 1
            if rj.get("primary_keywords") and len(rj.get("primary_keywords", [])) > 0:
                criteria["keyword_usage"]["optimized"] += 1

        except (json.JSONDecodeError, KeyError, TypeError):
            continue

    _finalize_averages(criteria)
    return criteria


def _extract_geo_criteria_scores(results: List[AuditResult]) -> Dict[str, Any]:
    """
    Extract criteria from GEO audit results.

    GEO audits store scores under a ``geo_audit`` wrapper key.  Each criterion
    slot is repurposed to hold the nearest GEO metric so the generic
    ``calculate_criterion_score`` / ``build_comparison_matrix`` pipeline works
    without modification:

      meta_titles        → Citation Probability   (geo_audit.citation_probability)
      meta_descriptions  → Factual Density        (geo_audit.factual_density_score)
      headings_structure → Structure Quality      (geo_audit.structure_score)
      schema_markup      → Schema Signals present (geo_audit.schema_signals_detected)
      internal_links     → Entity Richness        (count of entities_detected)
      content_length     → Content Depth          (word count when available)
      faq_content        → FAQ / Q&A Coverage     (no FAQ-gap issue found → covered)
      image_optimization → Authority Score        (geo_audit.authority_score)
      readability        → Overall GEO Score      (geo_audit.overall_score)
      keyword_usage      → Quotable Statements    (≥3 quotable_statements → optimised)
    """
    criteria = _empty_criteria()

    for result in results:
        if not result.result_json:
            continue
        try:
            rj = json.loads(result.result_json) if isinstance(result.result_json, str) else result.result_json
            if not rj:
                continue

            # GEO audits wrap sub-scores under "geo_audit"; fall back to flat dict
            geo = rj.get("geo_audit", rj)

            # ── Citation Probability → meta_titles slot ───────────────────────
            # Store as total+=100, optimized+=score so that
            # (optimized/total)*100 == avg(citation_probability) across pages.
            citation_prob = geo.get("citation_probability")
            if citation_prob is not None:
                criteria["meta_titles"]["total"] += 100
                criteria["meta_titles"]["optimized"] += int(citation_prob)

            # ── Factual Density → meta_descriptions slot ─────────────────────
            factual_density = geo.get("factual_density_score")
            if factual_density is not None:
                criteria["meta_descriptions"]["total"] += 100
                criteria["meta_descriptions"]["optimized"] += int(factual_density)

            # ── Structure Score → headings_structure slot ─────────────────────
            structure_score = geo.get("structure_score")
            if structure_score is not None:
                criteria["headings_structure"]["total"] += 100
                criteria["headings_structure"]["correct"] += int(structure_score)

            # ── Schema Signals → schema_markup slot (binary per page) ─────────
            criteria["schema_markup"]["total"] += 1
            schema_signals = geo.get("schema_signals_detected", [])
            if schema_signals:
                criteria["schema_markup"]["present"] += 1
                for sig in schema_signals:
                    if isinstance(sig, str):
                        criteria["schema_markup"]["types"].append(sig)

            # ── Entity count → internal_links slot (entity richness) ──────────
            entities = geo.get("entities_detected", [])
            if entities:
                criteria["internal_links"]["total_links"] += len(entities)
                criteria["internal_links"]["page_count"] += 1

            # ── Content depth (word count when available) ─────────────────────
            content = rj.get("content", geo.get("content", ""))
            if content:
                word_count = len(content.split())
                criteria["content_length"]["total_words"] += word_count
                criteria["content_length"]["page_count"] += 1
                if word_count < 300:
                    criteria["content_length"]["thin_pages"] += 1

            # ── FAQ coverage: absence of a FAQ-gap issue → covered ────────────
            criteria["faq_content"]["total"] += 1
            issues = rj.get("issues", [])
            faq_issue = any(
                "faq" in str(iss.get("finding", "")).lower() or
                "faq" in str(iss.get("category", "")).lower()
                for iss in issues if isinstance(iss, dict)
            )
            if not faq_issue:
                criteria["faq_content"]["present"] += 1

            # ── Authority Score → image_optimization slot ─────────────────────
            authority_score = geo.get("authority_score")
            if authority_score is not None:
                criteria["image_optimization"]["total"] += 100
                criteria["image_optimization"]["optimized"] += int(authority_score)

            # ── Overall GEO Score → readability slot ──────────────────────────
            overall = geo.get("overall_score")
            if overall is not None:
                criteria["readability"]["scores"].append(float(overall))

            # ── Quotable Statements → keyword_usage slot ──────────────────────
            criteria["keyword_usage"]["total"] += 1
            if len(geo.get("quotable_statements", [])) >= 3:
                criteria["keyword_usage"]["optimized"] += 1

        except (json.JSONDecodeError, KeyError, TypeError):
            continue

    _finalize_averages(criteria)
    return criteria


def _extract_content_criteria_scores(results: List[AuditResult]) -> Dict[str, Any]:
    """
    Extract criteria from CONTENT_QUALITY, READABILITY_AUDIT, and similar
    content-focused audit results.  Accepts common field aliases (e.g.
    ``title`` instead of ``meta_title``, ``word_count`` integer instead of
    reading the raw ``content`` string) to maximise hit-rate.
    """
    criteria = _empty_criteria()

    for result in results:
        if not result.result_json:
            continue
        try:
            rj = json.loads(result.result_json) if isinstance(result.result_json, str) else result.result_json
            if not rj:
                continue

            # Meta title (accept 'title' alias)
            criteria["meta_titles"]["total"] += 1
            title = rj.get("meta_title") or rj.get("title", "")
            if title:
                criteria["meta_titles"]["present"] += 1
                title_len = len(title)
                criteria["meta_titles"]["avg_length"].append(title_len)
                if 30 <= title_len <= 60:
                    criteria["meta_titles"]["optimized"] += 1

            # Meta description
            criteria["meta_descriptions"]["total"] += 1
            desc = rj.get("meta_description", "")
            if desc:
                criteria["meta_descriptions"]["present"] += 1
                desc_len = len(desc)
                criteria["meta_descriptions"]["avg_length"].append(desc_len)
                if 120 <= desc_len <= 160:
                    criteria["meta_descriptions"]["optimized"] += 1

            # Headings structure
            criteria["headings_structure"]["total"] += 1
            heading_issues = [
                iss for iss in rj.get("issues", [])
                if isinstance(iss, str) and "heading" in iss.lower()
            ]
            if heading_issues:
                criteria["headings_structure"]["issues"] += 1
            else:
                criteria["headings_structure"]["correct"] += 1

            # Schema markup (optional in content audits)
            criteria["schema_markup"]["total"] += 1
            if rj.get("schema_markup"):
                criteria["schema_markup"]["present"] += 1

            # Internal links (optional)
            internal_links = rj.get("internal_links", 0)
            if isinstance(internal_links, int):
                criteria["internal_links"]["total_links"] += internal_links
                criteria["internal_links"]["page_count"] += 1

            # Content length — accept integer 'word_count' or count from 'content'
            word_count = rj.get("word_count")
            if word_count is None:
                content = rj.get("content", "")
                word_count = len(content.split()) if content else 0
            if word_count:
                criteria["content_length"]["total_words"] += int(word_count)
                criteria["content_length"]["page_count"] += 1
                if int(word_count) < 300:
                    criteria["content_length"]["thin_pages"] += 1

            # FAQ content — accept 'has_faq', 'has_q_a', or 'faq_count'
            criteria["faq_content"]["total"] += 1
            if rj.get("has_faq") or rj.get("has_q_a") or (rj.get("faq_count") or 0) > 0:
                criteria["faq_content"]["present"] += 1

            # Images
            images = rj.get("images", [])
            criteria["image_optimization"]["total"] += len(images)
            criteria["image_optimization"]["optimized"] += sum(
                1 for img in images if isinstance(img, dict) and img.get("has_alt")
            )

            # Readability — accept 'readability_score' or nested dict
            rs = rj.get("readability_score") or (
                rj.get("readability", {}).get("score")
                if isinstance(rj.get("readability"), dict) else None
            )
            if rs and isinstance(rs, (int, float)):
                criteria["readability"]["scores"].append(float(rs))

            # Keyword usage
            criteria["keyword_usage"]["total"] += 1
            if rj.get("primary_keywords") and len(rj.get("primary_keywords", [])) > 0:
                criteria["keyword_usage"]["optimized"] += 1

        except (json.JSONDecodeError, KeyError, TypeError):
            continue

    _finalize_averages(criteria)
    return criteria


def extract_criteria_scores(
    results: List[AuditResult],
    audit_type: str = "SEO_AUDIT",
) -> Dict[str, Any]:
    """
    Extract per-criterion performance from audit results.

    Dispatches to an audit-type-aware extractor so that GEO, Content Quality,
    and other non-SEO audit types produce meaningful scores rather than
    silently returning zeros for all SEO-specific fields.

    Args:
        results:    AuditResult rows from a single audit run.
        audit_type: The audit type key (e.g. ``'SEO_AUDIT'``, ``'GEO_AUDIT'``).

    Returns:
        Dict compatible with ``calculate_criterion_score()`` and
        ``build_comparison_matrix()``.
    """
    normalized = audit_type.upper() if audit_type else ""

    if any(t in normalized for t in _GEO_TYPES):
        return _extract_geo_criteria_scores(results)

    if any(t in normalized for t in _CONTENT_TYPES):
        return _extract_content_criteria_scores(results)

    if any(t in normalized for t in _SEO_TYPES) or not normalized:
        return _extract_seo_criteria_scores(results)

    # Unknown type — best-effort SEO extractor with a warning
    print(f"[gap_analysis] Unknown audit_type '{audit_type}' — falling back to SEO criteria extractor.")
    return _extract_seo_criteria_scores(results)


def calculate_criterion_score(criterion_data: Dict[str, Any], criterion_type: str) -> float:
    """
    Calculate 0-100 score for a specific criterion based on its data.
    
    Returns:
        Score between 0-100
    """
    if criterion_type == "meta_titles":
        if criterion_data["total"] == 0:
            return 0.0
        return round((criterion_data["optimized"] / criterion_data["total"]) * 100, 1)
    
    elif criterion_type == "meta_descriptions":
        if criterion_data["total"] == 0:
            return 0.0
        return round((criterion_data["optimized"] / criterion_data["total"]) * 100, 1)
    
    elif criterion_type == "headings_structure":
        if criterion_data["total"] == 0:
            return 0.0
        return round((criterion_data["correct"] / criterion_data["total"]) * 100, 1)
    
    elif criterion_type == "schema_markup":
        if criterion_data["total"] == 0:
            return 0.0
        return round((criterion_data["present"] / criterion_data["total"]) * 100, 1)
    
    elif criterion_type == "internal_links":
        avg_links = criterion_data.get("avg", 0)
        # Score based on average: 0 links = 0, 10+ links = 100
        return min(100.0, round((avg_links / 10) * 100, 1))
    
    elif criterion_type == "content_length":
        if criterion_data["page_count"] == 0:
            return 0.0
        thin_ratio = criterion_data["thin_pages"] / criterion_data["page_count"]
        # Less thin pages = higher score
        return round((1 - thin_ratio) * 100, 1)
    
    elif criterion_type == "faq_content":
        if criterion_data["total"] == 0:
            return 0.0
        return round((criterion_data["present"] / criterion_data["total"]) * 100, 1)
    
    elif criterion_type == "image_optimization":
        if criterion_data["total"] == 0:
            return 100.0  # No images to optimize
        return round((criterion_data["optimized"] / criterion_data["total"]) * 100, 1)
    
    elif criterion_type == "readability":
        return criterion_data.get("avg", 0.0)
    
    elif criterion_type == "keyword_usage":
        if criterion_data["total"] == 0:
            return 0.0
        return round((criterion_data["optimized"] / criterion_data["total"]) * 100, 1)
    
    return 0.0


def build_comparison_matrix(
    target_criteria: Dict[str, Any],
    competitors_criteria: List[Dict[str, Any]],
    competitor_websites: List[str],
    audit_type: str = "SEO_AUDIT",
) -> Dict[str, Any]:
    """
    Build comparison matrix showing target vs competitors per criterion.

    Args:
        target_criteria:      Output of ``extract_criteria_scores`` for the target.
        competitors_criteria: One dict per competitor in the same format.
        competitor_websites:  Parallel list of website domain strings.
        audit_type:           Audit type key — drives criterion label selection.

    Returns:
        Dict with per-criterion comparisons ready for LLM analysis.
    """
    matrix: Dict[str, Any] = {
        "audit_type": audit_type,
        "criteria_comparisons": [],
        "category_summary": {},
    }

    # Map criteria to categories
    criterion_categories = {
        "meta_titles":        "on_page_seo",
        "meta_descriptions":  "on_page_seo",
        "headings_structure": "on_page_seo",
        "schema_markup":      "schema_markup",
        "internal_links":     "technical_seo",
        "content_length":     "content_quality",
        "faq_content":        "content_quality",
        "image_optimization": "technical_seo",
        "readability":        "content_quality",
        "keyword_usage":      "on_page_seo",
    }

    # Pick label set based on audit type
    _is_geo = any(t in (audit_type or "").upper() for t in _GEO_TYPES)
    criterion_names = _GEO_CRITERION_NAMES if _is_geo else _SEO_CRITERION_NAMES
    
    for criterion_key, criterion_name in criterion_names.items():
        target_score = calculate_criterion_score(target_criteria[criterion_key], criterion_key)
        
        # Calculate competitor scores
        competitor_scores = []
        for i, comp_criteria in enumerate(competitors_criteria):
            comp_score = calculate_criterion_score(comp_criteria[criterion_key], criterion_key)
            competitor_scores.append({
                "website": competitor_websites[i],
                "score": comp_score,
                "data": comp_criteria[criterion_key]
            })
        
        # Find best competitor
        best_comp = max(competitor_scores, key=lambda x: x["score"])
        gap_size = best_comp["score"] - target_score
        
        # Determine severity
        severity = "low"
        if gap_size > 30:
            severity = "critical"
        elif gap_size > 15:
            severity = "high"
        elif gap_size > 5:
            severity = "medium"
        
        matrix["criteria_comparisons"].append({
            "criterion": criterion_name,
            "criterion_key": criterion_key,
            "category": criterion_categories[criterion_key],
            "target_score": target_score,
            "target_data": target_criteria[criterion_key],
            "competitors": competitor_scores,
            "best_competitor_score": best_comp["score"],
            "best_competitor": best_comp["website"],
            "gap_size": round(gap_size, 1),
            "severity": severity
        })
    
    # Category summary
    categories = list(set(criterion_categories.values()))
    for category in categories:
        cat_criteria = [
            comp for comp in matrix["criteria_comparisons"]
            if comp["category"] == category
        ]
        
        if not cat_criteria:
            continue
        
        target_avg = sum(c["target_score"] for c in cat_criteria) / len(cat_criteria)
        comp_avg = sum(c["best_competitor_score"] for c in cat_criteria) / len(cat_criteria)
        
        matrix["category_summary"][category] = {
            "client_avg": round(target_avg, 1),
            "competitor_avg": round(comp_avg, 1),
            "gap": round(comp_avg - target_avg, 1)
        }
    
    return matrix


# ============================================================================
# LLM SYSTEM PROMPT
# ============================================================================

def build_gap_analysis_prompt(audit_type: str = "SEO_AUDIT") -> str:
    """Build system prompt for LLM gap analysis, adapted to the audit type."""
    normalized = (audit_type or "").upper()
    if any(t in normalized for t in _GEO_TYPES):
        analyst_role = "GEO (Generative Engine Optimization) specialist"
        focus_note = (
            "Focus on citation probability, factual density, entity richness, "
            "schema signals, authority score, and AI-visibility factors. "
            "Criterion names in the data reflect GEO metrics, not traditional SEO fields."
        )
    elif any(t in normalized for t in _CONTENT_TYPES):
        analyst_role = "content quality specialist"
        focus_note = (
            "Focus on readability, content depth, structure quality, "
            "FAQ coverage, and writing clarity."
        )
    else:
        analyst_role = "SEO specialist"
        focus_note = (
            "Focus on on-page SEO signals, schema markup, technical health, "
            "and keyword optimisation."
        )

    return f"""You are a competitive {analyst_role}. Analyze the comparison data between a target website and its competitors.

{focus_note}

For each criterion where the target scores LOWER than the best competitor:

1. Identify the gap size and severity (critical >30pts, high >15pts, medium >5pts, low ≤5pts)
2. Explain WHAT the competitor does better with a specific example from the data
3. Provide an exact, actionable fix for the client
4. Estimate effort (low/medium/high) and impact (low/medium/high)

Also identify strengths (where target leads competitors).

Group recommendations into:
- quick_wins (high impact, low effort)
- medium_term (medium impact/effort or high impact/medium effort)
- strategic (long-term improvements, high effort)

Return JSON with exactly these keys:

{{
  "criteria_gaps": [
    {{
      "criterion": "string (criterion name)",
      "category": "string",
      "client_score": 45,
      "best_competitor_score": 88,
      "best_competitor": "competitor-x.ro",
      "gap_size": 43,
      "severity": "critical",
      "details": "Specific description of what's different",
      "competitor_example": "What the competitor does better",
      "fix_action": "Exact recommended action",
      "effort": "medium",
      "impact": "high"
    }}
  ],
  "strengths": [
    {{
      "criterion": "string",
      "client_score": 85,
      "competitor_avg": 62,
      "advantage": 23,
      "details": "Why the target leads here"
    }}
  ],
  "recommendations": {{
    "quick_wins": [
      {{
        "action": "string",
        "criterion": "string",
        "impact": "high",
        "effort": "low",
        "estimated_time": "1 week"
      }}
    ],
    "medium_term": [],
    "strategic": []
  }}
}}

Return ONLY valid JSON. Do not include any text before or after the JSON."""


# ============================================================================
# BACKGROUND TASK
# ============================================================================

async def generate_gap_analysis_task(
    gap_id: str,
    target_audit_id: str,
    competitor_audit_ids: List[str],
    provider: str,
    model: str
):
    """
    Background task to generate gap analysis.
    
    Runs asynchronously after endpoint returns.
    """
    async with AsyncSessionLocal() as db:
        try:
            # Update status to running
            gap_result = await db.execute(
                select(CompetitorGapAnalysis).where(CompetitorGapAnalysis.id == gap_id)
            )
            gap = gap_result.scalar_one_or_none()
            if not gap:
                return
            
            gap.status = "running"
            await db.commit()
            
            # Load target audit + results
            target_audit_result = await db.execute(
                select(Audit).where(Audit.id == target_audit_id)
            )
            target_audit = target_audit_result.scalar_one_or_none()
            if not target_audit:
                raise ValueError(f"Target audit {target_audit_id} not found")
            
            target_results_query = await db.execute(
                select(AuditResult).where(AuditResult.audit_id == target_audit_id)
            )
            target_results = target_results_query.scalars().all()
            
            # Load competitor audits + results
            competitors_data = []
            for comp_id in competitor_audit_ids:
                comp_audit_result = await db.execute(
                    select(Audit).where(Audit.id == comp_id)
                )
                comp_audit = comp_audit_result.scalar_one_or_none()
                if not comp_audit:
                    continue
                
                comp_results_query = await db.execute(
                    select(AuditResult).where(AuditResult.audit_id == comp_id)
                )
                comp_results = comp_results_query.scalars().all()
                
                competitors_data.append({
                    "website": comp_audit.website,
                    "results": comp_results
                })
            
            # Determine audit type — all audits in a benchmark share the same type
            _audit_type = target_audit.audit_type or "SEO_AUDIT"

            # Extract criteria scores (audit-type-aware)
            print(f"[Gap Analysis {gap_id}] Extracting criteria scores (audit_type={_audit_type})...")
            target_criteria = extract_criteria_scores(target_results, audit_type=_audit_type)

            competitors_criteria = []
            competitor_websites = []
            for comp in competitors_data:
                comp_criteria = extract_criteria_scores(comp["results"], audit_type=_audit_type)
                competitors_criteria.append(comp_criteria)
                competitor_websites.append(comp["website"])

            # Build comparison matrix
            print(f"[Gap Analysis {gap_id}] Building comparison matrix...")
            matrix = build_comparison_matrix(
                target_criteria,
                competitors_criteria,
                competitor_websites,
                audit_type=_audit_type,
            )

            # Prepare LLM prompt
            system_prompt = build_gap_analysis_prompt(audit_type=_audit_type)
            
            user_content = f"""Target Website: {target_audit.website}
Competitors: {', '.join(competitor_websites)}

COMPARISON DATA:

{json.dumps(matrix, indent=2)}

Analyze the above data and generate a comprehensive gap analysis following the JSON format specified in the system prompt."""
            
            # Call LLM
            print(f"[Gap Analysis {gap_id}] Calling LLM {provider}/{model}...")
            response_text = await call_llm_for_summary(
                provider=provider,
                model=model,
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=4096
            )
            
            # Parse response
            clean_text = clean_json_response(response_text)
            gap_data = json.loads(clean_text)
            
            # Validate structure
            required_keys = ['criteria_gaps', 'strengths', 'recommendations']
            for key in required_keys:
                if key not in gap_data:
                    raise ValueError(f"Missing required key in LLM response: {key}")
            
            # Calculate overall gap score
            # Gap score = weighted average of gap sizes (0-100, higher = further behind)
            gaps = gap_data.get("criteria_gaps", [])
            if gaps:
                total_gap = sum(g.get("gap_size", 0) for g in gaps)
                overall_gap_score = min(100.0, round(total_gap / len(gaps), 1))
            else:
                overall_gap_score = 0.0
            
            # Update gap analysis record
            gap.status = "completed"
            gap.overall_gap_score = overall_gap_score
            gap.gaps_json = json.dumps(gap_data.get("criteria_gaps", []))
            gap.strengths_json = json.dumps(gap_data.get("strengths", []))
            gap.recommendations_json = json.dumps(gap_data.get("recommendations", {}))
            gap.completed_at = datetime.utcnow()
            
            await db.commit()
            print(f"[Gap Analysis {gap_id}] Completed successfully. Overall gap score: {overall_gap_score}")
            
        except Exception as e:
            print(f"[Gap Analysis {gap_id}] Error: {str(e)}")
            # Update status to failed
            try:
                gap_result = await db.execute(
                    select(CompetitorGapAnalysis).where(CompetitorGapAnalysis.id == gap_id)
                )
                gap = gap_result.scalar_one_or_none()
                if gap:
                    gap.status = "failed"
                    gap.error_message = str(e)
                    await db.commit()
            except Exception as _db_ex:
                print(f"[Gap Analysis {gap_id}] Warning: failed to persist failed status to DB: {_db_ex}")


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/generate")
async def generate_gap_analysis(
    request: GenerateGapAnalysisRequest,
    background_tasks: BackgroundTasks
):
    """
    Generate a new competitor gap analysis.
    
    Compares target audit vs competitors at criterion level (meta descriptions,
    schema markup, etc.) and generates actionable recommendations.
    
    Returns immediately and runs analysis in background.
    """
    async with AsyncSessionLocal() as db:
        # Validate target audit exists and is completed
        target_result = await db.execute(
            select(Audit).where(Audit.id == request.target_audit_id)
        )
        target_audit = target_result.scalar_one_or_none()
        
        if not target_audit:
            raise HTTPException(status_code=404, detail="Target audit not found")
        
        if target_audit.status != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Target audit must be completed (current status: {target_audit.status})"
            )
        
        # Validate competitor audits exist and are completed
        for comp_id in request.competitor_audit_ids:
            comp_result = await db.execute(
                select(Audit).where(Audit.id == comp_id)
            )
            comp_audit = comp_result.scalar_one_or_none()
            
            if not comp_audit:
                raise HTTPException(status_code=404, detail=f"Competitor audit {comp_id} not found")
            
            if comp_audit.status != "completed":
                raise HTTPException(
                    status_code=400,
                    detail=f"All competitor audits must be completed (audit {comp_id} status: {comp_audit.status})"
                )
        
        # Get model
        model = request.model or get_default_model(request.provider)
        
        # Create gap analysis record
        gap_id = str(uuid.uuid4())
        new_gap = CompetitorGapAnalysis(
            id=gap_id,
            benchmark_id=request.benchmark_id,
            name=request.name,
            target_website=target_audit.website,
            target_audit_id=request.target_audit_id,
            competitor_audit_ids=json.dumps(request.competitor_audit_ids),
            status="pending",
            provider=request.provider,
            model=model,
            created_at=datetime.utcnow()
        )
        
        db.add(new_gap)
        await db.commit()
        
        # Schedule background task
        background_tasks.add_task(
            generate_gap_analysis_task,
            gap_id=gap_id,
            target_audit_id=request.target_audit_id,
            competitor_audit_ids=request.competitor_audit_ids,
            provider=request.provider,
            model=model
        )
        
        return {
            "id": gap_id,
            "status": "generating",
            "message": "Gap analysis generation started. Check GET /api/gap-analysis/{id} for results."
        }


@router.get("")
async def list_gap_analyses(
    benchmark_id: Optional[str] = Query(None, description="Filter by benchmark project ID")
):
    """
    List all gap analyses.
    
    Optional filter by benchmark_id to show only analyses linked to a specific benchmark.
    """
    async with AsyncSessionLocal() as db:
        query = select(CompetitorGapAnalysis).order_by(CompetitorGapAnalysis.created_at.desc())
        
        if benchmark_id:
            query = query.where(CompetitorGapAnalysis.benchmark_id == benchmark_id)
        
        result = await db.execute(query)
        gaps = result.scalars().all()
        
        return [gap.to_dict() for gap in gaps]


@router.get("/{gap_id}")
async def get_gap_analysis(gap_id: str):
    """
    Get detailed gap analysis results.
    
    Returns:
        Full gap analysis data including criteria gaps, strengths, and recommendations
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CompetitorGapAnalysis).where(CompetitorGapAnalysis.id == gap_id)
        )
        gap = result.scalar_one_or_none()
        
        if not gap:
            raise HTTPException(status_code=404, detail="Gap analysis not found")
        
        return gap.to_dict()


@router.get("/{gap_id}/export")
async def export_gap_analysis(gap_id: str):
    """
    Export gap analysis as structured JSON for PDF generation or other use.
    
    Returns:
        Comprehensive export including all data, formatted for reports
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CompetitorGapAnalysis).where(CompetitorGapAnalysis.id == gap_id)
        )
        gap = result.scalar_one_or_none()
        
        if not gap:
            raise HTTPException(status_code=404, detail="Gap analysis not found")
        
        if gap.status != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Gap analysis must be completed before export (current status: {gap.status})"
            )
        
        # Build comprehensive export
        export_data = gap.to_dict()
        
        # Add metadata
        export_data["export_metadata"] = {
            "exported_at": datetime.utcnow().isoformat(),
            "format_version": "1.0"
        }
        
        return JSONResponse(content=export_data)


@router.delete("/{gap_id}")
async def delete_gap_analysis(gap_id: str):
    """
    Delete a gap analysis.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(CompetitorGapAnalysis).where(CompetitorGapAnalysis.id == gap_id)
        )
        gap = result.scalar_one_or_none()
        
        if not gap:
            raise HTTPException(status_code=404, detail="Gap analysis not found")
        
        await db.delete(gap)
        await db.commit()
        
        return {"status": "deleted", "id": gap_id}
