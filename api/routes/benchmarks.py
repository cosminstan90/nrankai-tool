"""
Competitor Benchmarking Module for Website LLM Analyzer.

Enables grouping and comparative analysis of 2–5 audits (target + competitors)
with AI-generated competitive insights.
"""

import json
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import AsyncSessionLocal, Audit, AuditResult, BenchmarkProject
from api.routes.summary import call_llm_for_summary, clean_json_response

router = APIRouter(prefix="/api/benchmarks", tags=["benchmarks"])

# Audit-type classification sets (mirror gap_analysis.py)
_GEO_TYPES     = {"GEO_AUDIT", "AI_OVERVIEW_OPTIMIZATION"}
_CONTENT_TYPES = {"CONTENT_QUALITY", "READABILITY_AUDIT", "CONTENT_FRESHNESS",
                  "SPELLING_GRAMMAR", "BRAND_VOICE", "TRANSLATION_QUALITY"}


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class CreateBenchmarkRequest(BaseModel):
    """Request model for creating a benchmark project."""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    audit_type: str = Field(..., min_length=1)
    target_audit_id: str = Field(..., min_length=1)
    competitor_audit_ids: List[str] = Field(..., min_length=1, max_length=4)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _effective_status(benchmark) -> str:
    """
    Return the analysis_status for a benchmark, with a safe fallback for
    pre-migration rows that may have NULL in the new column.
    """
    raw = getattr(benchmark, "analysis_status", None)
    if raw:
        return raw
    return "completed" if benchmark.benchmark_summary else "pending"


async def _batch_load_audit_summaries(
    audit_ids: List[str],
    db: AsyncSession,
) -> dict:
    """
    Load aggregated summaries for multiple audits using exactly 2 queries
    (1 for Audit rows, 1 for AuditResult rows) regardless of how many
    audit IDs are provided.

    Returns:
        {audit_id: {website, avg_score, pages_analyzed, distribution, top_issues}}
    """
    if not audit_ids:
        return {}

    # Query 1 – all Audit rows
    audits_result = await db.execute(select(Audit).where(Audit.id.in_(audit_ids)))
    audit_map = {a.id: a for a in audits_result.scalars().all()}

    # Query 2 – all AuditResult rows
    results_result = await db.execute(
        select(AuditResult).where(AuditResult.audit_id.in_(audit_ids))
    )
    results_by_audit: dict = defaultdict(list)
    for r in results_result.scalars().all():
        results_by_audit[r.audit_id].append(r)

    priority_order = {"high": 0, "critical": 0, "medium": 1, "low": 2}
    summaries: dict = {}

    for audit_id in audit_ids:
        audit = audit_map.get(audit_id)
        if not audit:
            continue

        results = results_by_audit[audit_id]

        if not results:
            summaries[audit_id] = {
                "website": audit.website,
                "avg_score": 0,
                "pages_analyzed": 0,
                "distribution": {"excellent": 0, "good": 0, "needs_work": 0, "poor": 0},
                "top_issues": [],
            }
            continue

        # Score distribution
        distribution = {"excellent": 0, "good": 0, "needs_work": 0, "poor": 0}
        for result in results:
            if result.classification:
                cls = result.classification.lower().replace(" ", "_")
                if cls in distribution:
                    distribution[cls] += 1

        # Top issues: at most 2 per page, sorted by severity, capped at 10
        top_issues = []
        for result in results:
            if result.result_json:
                try:
                    data = json.loads(result.result_json)
                    for opp in data.get("optimization_opportunities", [])[:2]:
                        if isinstance(opp, dict):
                            top_issues.append({
                                "page":     result.page_url,
                                "priority": opp.get("priority", "medium"),
                                "issue":    opp.get("issue", str(opp)),
                                "category": opp.get("category", "Other"),
                            })
                except (json.JSONDecodeError, KeyError):
                    continue

        top_issues.sort(key=lambda x: priority_order.get(x["priority"], 3))

        summaries[audit_id] = {
            "website":        audit.website,
            "avg_score":      round(audit.average_score, 1) if audit.average_score else 0,
            "pages_analyzed": audit.pages_analyzed,
            "distribution":   distribution,
            "top_issues":     top_issues[:10],
        }

    return summaries


def _build_benchmark_system_prompt(audit_type: str = "SEO_AUDIT") -> str:
    """
    Build an audit-type-aware system prompt for the competitive analysis LLM call.
    The role and focus note are customised for SEO, GEO, or Content audit types.
    """
    audit_upper = audit_type.upper()

    if audit_upper in _GEO_TYPES:
        analyst_role = "GEO (Generative Engine Optimization) competitive analyst"
        focus_note = (
            "Focus on GEO-specific dimensions: citation probability, factual density, "
            "entity recognition scores, authority signals, and AI-readiness. "
            "Frame strengths and weaknesses in terms of how likely each site is to be "
            "cited or quoted by AI systems such as ChatGPT, Perplexity, or Google SGE."
        )
    elif audit_upper in _CONTENT_TYPES:
        analyst_role = "content quality competitive analyst"
        focus_note = (
            "Focus on content dimensions: readability scores, content depth, FAQ/Q&A coverage, "
            "structural clarity, brand voice consistency, and freshness. "
            "Frame insights in terms of user comprehension, trust, and content authority."
        )
    else:
        analyst_role = "SEO competitive intelligence analyst"
        focus_note = (
            "Focus on SEO dimensions: technical health, meta optimisation, heading structure, "
            "internal linking, content quality, and on-page performance. "
            "Frame insights in terms of search ranking potential and crawlability."
        )

    return f"""You are a {analyst_role} evaluating website performance across multiple competitors.
{focus_note}

Analyse the provided benchmark data and return a comprehensive competitive analysis as a JSON object with exactly these keys:

1. "competitive_summary": A narrative in 2–3 paragraphs covering the overall landscape, the target's position relative to competitors, key differentiators, and strategic implications.

2. "strengths": Array of 3–5 areas where the target outperforms competitors. Each object must have:
   - "area": Name of the strength area
   - "target_score_range": Target's score or range in this area
   - "competitor_avg": Average competitor score in this area
   - "insight": Why this is a competitive advantage

3. "weaknesses": Array of 3–5 areas where the target underperforms. Same structure as strengths.

4. "opportunities": Array of 3–5 strategic opportunities. Each object must have:
   - "opportunity": Clear description
   - "priority": "high" or "medium"
   - "rationale": Expected impact and reasoning

5. "threat_level": Overall competitive threat — "low" (target leads), "medium" (comparable), or "high" (target lags).

Return ONLY valid JSON. No text before or after the JSON."""


def _build_benchmark_data_payload(
    target_summary: dict,
    competitor_summaries: List[dict],
) -> str:
    """Build the data section of the LLM prompt from pre-loaded audit summaries."""
    parts = [
        "BENCHMARK OVERVIEW:",
        f"Target Website: {target_summary['website']}",
        f"Number of Competitors: {len(competitor_summaries)}",
        "",
        "TARGET PERFORMANCE:",
        f"  Average Score: {target_summary['avg_score']}",
        f"  Pages Analysed: {target_summary['pages_analyzed']}",
        f"  Distribution: Excellent={target_summary['distribution']['excellent']}, "
        f"Good={target_summary['distribution']['good']}, "
        f"Needs Work={target_summary['distribution']['needs_work']}, "
        f"Poor={target_summary['distribution']['poor']}",
        "",
        "TARGET TOP ISSUES:",
    ]

    for i, issue in enumerate(target_summary["top_issues"][:5], 1):
        parts.append(f"  {i}. [{issue['priority']}] {issue['category']}: {issue['issue']}")

    parts.extend(["", "COMPETITOR PERFORMANCE:"])

    for i, comp in enumerate(competitor_summaries, 1):
        parts.extend([
            "",
            f"Competitor {i}: {comp['website']}",
            f"  Average Score: {comp['avg_score']}",
            f"  Pages Analysed: {comp['pages_analyzed']}",
            f"  Distribution: Excellent={comp['distribution']['excellent']}, "
            f"Good={comp['distribution']['good']}, "
            f"Needs Work={comp['distribution']['needs_work']}, "
            f"Poor={comp['distribution']['poor']}",
        ])
        if comp["top_issues"]:
            parts.append("  Top Issues:")
            for j, issue in enumerate(comp["top_issues"][:3], 1):
                parts.append(f"    {j}. [{issue['priority']}] {issue['category']}: {issue['issue']}")

    # Page-count-weighted competitor average (more representative than a simple mean)
    total_pages    = sum(c["pages_analyzed"] for c in competitor_summaries) or 1
    weighted_avg   = sum(
        c["avg_score"] * (c["pages_analyzed"] / total_pages) for c in competitor_summaries
    ) if competitor_summaries else 0
    best_comp      = max((c["avg_score"] for c in competitor_summaries), default=0)
    all_scores     = [target_summary["avg_score"]] + [c["avg_score"] for c in competitor_summaries]
    target_rank    = sorted(all_scores, reverse=True).index(target_summary["avg_score"]) + 1

    parts.extend([
        "",
        "COMPARATIVE STATISTICS:",
        f"  Target Score: {target_summary['avg_score']}",
        f"  Competitor Weighted Average: {weighted_avg:.1f}",
        f"  Best Competitor Score: {best_comp}",
        f"  Target vs Weighted Average: {target_summary['avg_score'] - weighted_avg:+.1f} pts",
        f"  Target Rank: {target_rank} out of {len(all_scores)}",
    ])

    return "\n".join(parts)


async def generate_benchmark_analysis_task(
    benchmark_id: str,
    provider: Optional[str],
    model: Optional[str],
):
    """
    Background task: load audit data → call LLM → persist analysis.

    Status lifecycle:
        pending  → generating  (set at start of this function)
        generating → completed (on success)
        generating → failed    (on any exception; error message persisted)

    A fresh DB session is opened for error persistence to avoid issues
    with a rolled-back session from the main try block.
    """
    async with AsyncSessionLocal() as db:
        try:
            # ── Load benchmark ────────────────────────────────────────────
            result = await db.execute(
                select(BenchmarkProject).where(BenchmarkProject.id == benchmark_id)
            )
            benchmark = result.scalar_one_or_none()
            if not benchmark:
                print(f"[Benchmark] Project {benchmark_id} not found — aborting")
                return

            # ── Mark as generating ───────────────────────────────────────
            benchmark.analysis_status = "generating"
            benchmark.analysis_error  = None
            await db.commit()

            # ── Resolve provider / model from target audit if not supplied ─
            if not provider or not model:
                tgt_result = await db.execute(
                    select(Audit).where(Audit.id == benchmark.target_audit_id)
                )
                tgt_audit = tgt_result.scalar_one_or_none()
                if tgt_audit:
                    provider = provider or tgt_audit.provider
                    model    = model    or tgt_audit.model

            # ── Batch-load all summaries (2 queries) ─────────────────────
            comp_ids    = json.loads(benchmark.competitor_audit_ids) if benchmark.competitor_audit_ids else []
            all_ids     = [benchmark.target_audit_id] + comp_ids
            summaries   = await _batch_load_audit_summaries(all_ids, db)

            target_summary = summaries.get(benchmark.target_audit_id)
            if not target_summary:
                raise ValueError(f"Target audit {benchmark.target_audit_id} data not found")

            competitor_summaries = [summaries[cid] for cid in comp_ids if cid in summaries]
            if not competitor_summaries:
                raise ValueError("No competitor audit data found")

            # ── Build prompt and call LLM ─────────────────────────────────
            audit_type    = getattr(benchmark, "audit_type", "SEO_AUDIT") or "SEO_AUDIT"
            system_prompt = _build_benchmark_system_prompt(audit_type)
            user_content  = _build_benchmark_data_payload(target_summary, competitor_summaries)

            print(f"[Benchmark] Generating analysis for {benchmark_id} using {provider}/{model}")
            response_text = await call_llm_for_summary(
                provider=provider,
                model=model,
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=4096,
            )

            # ── Parse and validate ────────────────────────────────────────
            clean_text    = clean_json_response(response_text)
            analysis_data = json.loads(clean_text)

            for key in ("competitive_summary", "strengths", "weaknesses", "opportunities", "threat_level"):
                if key not in analysis_data:
                    raise ValueError(f"LLM response missing required key: '{key}'")

            # ── Persist completed state ──────────────────────────────────
            benchmark.benchmark_summary = json.dumps(analysis_data)
            benchmark.analysis_status   = "completed"
            benchmark.updated_at        = datetime.utcnow()
            await db.commit()
            print(f"[Benchmark] Analysis completed for {benchmark_id}")

        except Exception as exc:
            err_msg = str(exc)[:500]
            print(f"[Benchmark] Error for {benchmark_id}: {err_msg}")
            # Persist failed state in a fresh session (original session may be dirty)
            try:
                await db.rollback()
                async with AsyncSessionLocal() as err_db:
                    err_res = await err_db.execute(
                        select(BenchmarkProject).where(BenchmarkProject.id == benchmark_id)
                    )
                    err_bench = err_res.scalar_one_or_none()
                    if err_bench:
                        err_bench.analysis_status = "failed"
                        err_bench.analysis_error  = err_msg
                        err_bench.updated_at      = datetime.utcnow()
                        await err_db.commit()
            except Exception as db_exc:
                print(f"[Benchmark] Could not persist failed state for {benchmark_id}: {db_exc}")


# ============================================================================
# API ENDPOINTS
# ============================================================================

@router.post("")
async def create_benchmark(
    request: CreateBenchmarkRequest,
    background_tasks: BackgroundTasks,
):
    """
    Create a new benchmark project and queue AI analysis.

    Validates that:
    - All audit IDs exist and are completed
    - All audits share the same audit_type as requested
    - Target is not also listed as a competitor
    - At most 4 competitors are supplied
    """
    async with AsyncSessionLocal() as db:
        # Runtime guards (Pydantic's max_length=4 can be bypassed via direct API calls)
        if len(request.competitor_audit_ids) > 4:
            raise HTTPException(status_code=400, detail="Maximum 4 competitors allowed")

        if request.target_audit_id in request.competitor_audit_ids:
            raise HTTPException(
                status_code=400,
                detail="Target audit cannot also be listed as a competitor",
            )

        # Validate target
        tgt_result = await db.execute(select(Audit).where(Audit.id == request.target_audit_id))
        target_audit = tgt_result.scalar_one_or_none()
        if not target_audit:
            raise HTTPException(status_code=404, detail=f"Target audit not found: {request.target_audit_id}")
        if target_audit.status != "completed":
            raise HTTPException(status_code=400, detail=f"Target audit must be completed (status: {target_audit.status})")
        if target_audit.audit_type != request.audit_type:
            raise HTTPException(
                status_code=400,
                detail=f"Target audit type ({target_audit.audit_type}) does not match specified type ({request.audit_type})",
            )

        # Deduplicate competitor IDs while preserving order
        comp_ids = list(dict.fromkeys(request.competitor_audit_ids))

        # Validate all competitors in a single batch query
        comp_results = await db.execute(select(Audit).where(Audit.id.in_(comp_ids)))
        comp_map = {a.id: a for a in comp_results.scalars().all()}

        for comp_id in comp_ids:
            comp = comp_map.get(comp_id)
            if not comp:
                raise HTTPException(status_code=404, detail=f"Competitor audit not found: {comp_id}")
            if comp.status != "completed":
                raise HTTPException(
                    status_code=400,
                    detail=f"All audits must be completed. Audit {comp_id} status: {comp.status}",
                )
            if comp.audit_type != request.audit_type:
                raise HTTPException(
                    status_code=400,
                    detail=f"All audits must share the same type. Audit {comp_id} type: {comp.audit_type}",
                )

        # Create the benchmark record
        benchmark_id  = str(uuid.uuid4())
        new_benchmark = BenchmarkProject(
            id                   = benchmark_id,
            name                 = request.name,
            description          = request.description,
            audit_type           = request.audit_type,
            target_audit_id      = request.target_audit_id,
            competitor_audit_ids = json.dumps(comp_ids),
            benchmark_summary    = None,
            analysis_status      = "pending",
            analysis_error       = None,
        )
        db.add(new_benchmark)
        await db.commit()

    # Queue background analysis
    background_tasks.add_task(
        generate_benchmark_analysis_task,
        benchmark_id=benchmark_id,
        provider=None,
        model=None,
    )

    return {
        "id":              benchmark_id,
        "analysis_status": "pending",
        "message":         "Benchmark created. AI analysis queued.",
    }


@router.get("")
async def list_benchmarks():
    """
    List all benchmark projects with summary info and analysis status.

    Uses a single batch query for all referenced audits (no N+1).
    Returns analysis_status and analysis_error so the frontend can
    distinguish pending / generating / completed / failed states.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BenchmarkProject).order_by(BenchmarkProject.created_at.desc())
        )
        benchmarks = result.scalars().all()

        # Batch-load all referenced audits in one query
        all_audit_ids:     set  = set()
        benchmark_comp_ids: dict = {}
        for bm in benchmarks:
            if bm.target_audit_id:
                all_audit_ids.add(bm.target_audit_id)
            comp_ids = json.loads(bm.competitor_audit_ids) if bm.competitor_audit_ids else []
            benchmark_comp_ids[bm.id] = comp_ids
            all_audit_ids.update(comp_ids)

        audit_map: dict = {}
        if all_audit_ids:
            audits_result = await db.execute(select(Audit).where(Audit.id.in_(all_audit_ids)))
            audit_map = {a.id: a for a in audits_result.scalars().all()}

        response = []
        for bm in benchmarks:
            tgt  = audit_map.get(bm.target_audit_id)
            comp = [
                {
                    "website":   audit_map[cid].website,
                    "avg_score": round(audit_map[cid].average_score, 1) if audit_map[cid].average_score else 0,
                }
                for cid in benchmark_comp_ids.get(bm.id, [])
                if cid in audit_map
            ]

            response.append({
                "id":              bm.id,
                "name":            bm.name,
                "audit_type":      bm.audit_type,
                "target":          {"website": tgt.website, "avg_score": round(tgt.average_score, 1) if tgt and tgt.average_score else 0} if tgt else None,
                "competitors":     comp,
                "has_analysis":    bm.benchmark_summary is not None,
                "analysis_status": _effective_status(bm),
                "analysis_error":  getattr(bm, "analysis_error", None),
                "created_at":      bm.created_at.isoformat() if bm.created_at else None,
            })

        return response


@router.get("/{benchmark_id}")
async def get_benchmark_detail(benchmark_id: str):
    """
    Return full benchmark detail with AI analysis.

    Uses batch loading: exactly 3 DB queries regardless of competitor count
    (1 for BenchmarkProject, 1 for all Audit rows, 1 for all AuditResult rows).
    """
    async with AsyncSessionLocal() as db:
        bm_result = await db.execute(
            select(BenchmarkProject).where(BenchmarkProject.id == benchmark_id)
        )
        benchmark = bm_result.scalar_one_or_none()
        if not benchmark:
            raise HTTPException(status_code=404, detail="Benchmark project not found")

        comp_ids = json.loads(benchmark.competitor_audit_ids) if benchmark.competitor_audit_ids else []
        all_ids  = [benchmark.target_audit_id] + comp_ids

        # Batch-load all summaries (2 queries)
        summaries = await _batch_load_audit_summaries(all_ids, db)

        _empty = {
            "website": "Unknown", "avg_score": 0, "pages_analyzed": 0,
            "distribution": {"excellent": 0, "good": 0, "needs_work": 0, "poor": 0},
            "top_issues": [],
        }
        target_summary       = summaries.get(benchmark.target_audit_id, _empty)
        competitor_summaries = [
            {"audit_id": cid, **summaries[cid]}
            for cid in comp_ids if cid in summaries
        ]

        # Page-count-weighted competitor average
        total_pages   = sum(c["pages_analyzed"] for c in competitor_summaries) or 1
        comp_avg      = sum(
            c["avg_score"] * (c["pages_analyzed"] / total_pages)
            for c in competitor_summaries
        ) if competitor_summaries else 0
        comp_best     = max((c["avg_score"] for c in competitor_summaries), default=0)
        all_scores    = [target_summary["avg_score"]] + [c["avg_score"] for c in competitor_summaries]
        target_rank   = sorted(all_scores, reverse=True).index(target_summary["avg_score"]) + 1

        comparison_metrics = {
            "target_score":   target_summary["avg_score"],
            "competitor_avg": round(comp_avg, 1),
            "competitor_best": comp_best,
            "delta_vs_avg":   round(target_summary["avg_score"] - comp_avg, 1),
            "rank":           target_rank,
            "position":       f"{target_rank} out of {len(all_scores)}",
        }

        ai_analysis = None
        if benchmark.benchmark_summary:
            try:
                ai_analysis = json.loads(benchmark.benchmark_summary)
            except (json.JSONDecodeError, TypeError):
                ai_analysis = None

        return {
            "id":                   benchmark.id,
            "name":                 benchmark.name,
            "description":          benchmark.description,
            "audit_type":           benchmark.audit_type,
            "analysis_status":      _effective_status(benchmark),
            "analysis_error":       getattr(benchmark, "analysis_error", None),
            "target_summary":       {"audit_id": benchmark.target_audit_id, **target_summary},
            "competitor_summaries": competitor_summaries,
            "comparison_metrics":   comparison_metrics,
            "ai_analysis":          ai_analysis,
            "created_at":           benchmark.created_at.isoformat() if benchmark.created_at else None,
            "updated_at":           benchmark.updated_at.isoformat() if benchmark.updated_at else None,
        }


@router.delete("/{benchmark_id}")
async def delete_benchmark(benchmark_id: str):
    """Delete a benchmark project."""
    async with AsyncSessionLocal() as db:
        bm_result = await db.execute(
            select(BenchmarkProject).where(BenchmarkProject.id == benchmark_id)
        )
        benchmark = bm_result.scalar_one_or_none()
        if not benchmark:
            raise HTTPException(status_code=404, detail="Benchmark project not found")
        await db.delete(benchmark)
        await db.commit()
        return {"message": "Benchmark deleted"}


@router.post("/{benchmark_id}/regenerate")
async def regenerate_benchmark_analysis(
    benchmark_id: str,
    background_tasks: BackgroundTasks,
    provider: Optional[str] = Query(default=None, description="Override LLM provider"),
    model:    Optional[str] = Query(default=None, description="Override LLM model"),
):
    """
    Re-queue AI analysis for an existing benchmark.

    Synchronously sets analysis_status = 'generating' before returning so
    the frontend immediately reflects the in-progress state.
    """
    async with AsyncSessionLocal() as db:
        bm_result = await db.execute(
            select(BenchmarkProject).where(BenchmarkProject.id == benchmark_id)
        )
        benchmark = bm_result.scalar_one_or_none()
        if not benchmark:
            raise HTTPException(status_code=404, detail="Benchmark project not found")

        if provider:
            provider = provider.lower()
            if provider not in ("anthropic", "openai", "mistral"):
                raise HTTPException(status_code=400, detail="Invalid provider. Use: anthropic, openai, or mistral")

        # Update status synchronously so the frontend sees it immediately
        benchmark.analysis_status = "generating"
        benchmark.analysis_error  = None
        await db.commit()

    background_tasks.add_task(
        generate_benchmark_analysis_task,
        benchmark_id=benchmark_id,
        provider=provider,
        model=model,
    )

    return {
        "analysis_status": "generating",
        "message":         "Analysis regeneration started.",
        "benchmark_id":    benchmark_id,
    }
