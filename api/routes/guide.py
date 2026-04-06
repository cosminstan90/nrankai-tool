"""
Per-URL GEO & SEO Guide Generation

Collects all audit results + GSC data for a given URL, then uses an LLM
to generate a structured GEO & SEO improvement guide.

Endpoints
---------
POST   /api/guide/generate          create guide + start background generation
GET    /api/guide/{id}              poll status / fetch completed guide
DELETE /api/guide/{id}              delete a guide
GET    /api/guide/by-url            list guides for a given URL
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Optional

from api.utils.task_runner import create_tracked_task

from fastapi import APIRouter, HTTPException
from api.utils.errors import raise_not_found
from pydantic import BaseModel
from sqlalchemy import select, desc

from api.models.database import (
    AsyncSessionLocal,
    UrlGuide,
    AuditResult,
    Audit,
    GscProperty,
    GscPageRow,
    GscQueryRow,
    GoogleOAuthToken,
)
from api.routes.schema_gen import call_llm_for_schema

router = APIRouter(prefix="/api/guide", tags=["guide"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an expert GEO (Generative Engine Optimisation) and SEO consultant.

Analyse the data provided for a specific URL and generate a structured improvement guide.

IMPORTANT: Respond with ONLY a valid JSON object in this exact format:
{
  "executive_summary": "2-3 sentences summarising the main opportunities for this URL",
  "geo_improvements": [
    {
      "title": "Short improvement title",
      "priority": "high",
      "description": "Why this matters for AI search engines / LLM citations",
      "action": "Specific, concrete action to take"
    }
  ],
  "seo_improvements": [
    {
      "title": "Short improvement title",
      "priority": "medium",
      "description": "Why this matters for traditional search",
      "action": "Specific, concrete action to take"
    }
  ],
  "query_opportunities": [
    {
      "query": "the search query",
      "current_position": 14.2,
      "insight": "Why this query is an opportunity",
      "recommendation": "What to do to capture this query better"
    }
  ],
  "quick_wins": [
    "Specific quick win action 1",
    "Specific quick win action 2"
  ]
}

Priority values must be: "high", "medium", or "low".
Provide at least 3 GEO improvements, 3 SEO improvements, and 3 quick wins.
If no query data is available, return an empty array for query_opportunities.
"""


def _audit_type_label(atype: str) -> str:
    labels = {
        "GEO_AUDIT": "GEO Audit",
        "SEO_AUDIT": "SEO Audit",
        "CONTENT_AUDIT": "Content Audit",
        "TECHNICAL_AUDIT": "Technical Audit",
        "UX_AUDIT": "UX Audit",
        "CONVERSION_AUDIT": "Conversion Audit",
    }
    return labels.get(atype, atype)


# ---------------------------------------------------------------------------
# Background generation task
# ---------------------------------------------------------------------------

async def _generate_guide(guide_id: int):
    """Background task: collect data and call LLM to generate the guide."""
    async with AsyncSessionLocal() as db:
        guide = await db.get(UrlGuide, guide_id)
        if not guide:
            return

        guide.status = "running"
        await db.commit()

        try:
            url = guide.url
            provider = (guide.provider or "anthropic").upper()
            model = guide.model or "claude-haiku-4-5"

            # ── 1. Load audit results for this URL ─────────────────────────
            stmt = (
                select(AuditResult, Audit)
                .join(Audit, AuditResult.audit_id == Audit.id)
                .where(AuditResult.page_url == url)
                .where(Audit.status == "completed")
                .order_by(Audit.audit_type, desc(Audit.created_at))
            )
            rows = (await db.execute(stmt)).fetchall()

            # Deduplicate: keep most-recent per audit_type
            seen = {}
            for result, audit in rows:
                if audit.audit_type not in seen:
                    seen[audit.audit_type] = (result, audit)

            audit_sections = []
            all_issues = []
            for atype, (result, audit) in seen.items():
                label = _audit_type_label(atype)
                score = result.score or "N/A"
                audit_sections.append(f"- {label}: {score}/100")

                # Extract top issues from result_json
                if result.result_json:
                    try:
                        rj = json.loads(result.result_json)
                        issues = rj.get("issues", rj.get("top_issues", []))
                        for iss in issues[:3]:
                            if isinstance(iss, dict):
                                title = iss.get("title", iss.get("name", ""))
                                desc = iss.get("description", iss.get("detail", ""))
                                if title:
                                    all_issues.append(f"[{label}] {title}: {desc}")
                            elif isinstance(iss, str):
                                all_issues.append(f"[{label}] {iss}")
                    except (json.JSONDecodeError, TypeError):
                        pass

            # ── 2. Load GSC page metrics ───────────────────────────────────
            gsc_page_data = None
            gsc_property_name = None
            if guide.gsc_property_id:
                prop = await db.get(GscProperty, guide.gsc_property_id)
                if prop:
                    gsc_property_name = prop.name
                    # Normalise URL for matching
                    norm_url = url.rstrip("/").lower()
                    page_stmt = select(GscPageRow).where(
                        GscPageRow.property_id == guide.gsc_property_id
                    )
                    page_rows = (await db.execute(page_stmt)).scalars().all()
                    for pr in page_rows:
                        if pr.page.rstrip("/").lower() == norm_url:
                            gsc_page_data = pr
                            break

            # ── 3. Load per-page queries (OAuth or from query rows with page filter) ──
            query_data = []
            if guide.gsc_property_id:
                # Try OAuth API first
                token_row = (await db.execute(select(GoogleOAuthToken))).scalars().first()
                if token_row:
                    try:
                        from api.routes.gsc import _get_gsc_credentials
                        creds = await _get_gsc_credentials()
                        if creds:
                            prop = await db.get(GscProperty, guide.gsc_property_id)
                            site_url = prop.site_url if prop else None
                            if site_url:
                                end_date = datetime.utcnow().date()
                                start_date = end_date - timedelta(days=90)

                                def _fetch_page_queries():
                                    from googleapiclient.discovery import build
                                    svc = build("searchconsole", "v1", credentials=creds)
                                    body = {
                                        "startDate": start_date.isoformat(),
                                        "endDate": end_date.isoformat(),
                                        "dimensions": ["query"],
                                        "dimensionFilterGroups": [{
                                            "filters": [{
                                                "dimension": "page",
                                                "expression": url,
                                                "operator": "equals",
                                            }]
                                        }],
                                        "rowLimit": 500,
                                    }
                                    resp = svc.searchanalytics().query(
                                        siteUrl=site_url, body=body
                                    ).execute()
                                    return resp.get("rows", [])

                                raw_rows = await asyncio.get_event_loop().run_in_executor(
                                    None, _fetch_page_queries
                                )
                                for r in raw_rows[:30]:
                                    keys = r.get("keys", [""])
                                    query_data.append({
                                        "query": keys[0] if keys else "",
                                        "clicks": int(r.get("clicks", 0)),
                                        "impressions": int(r.get("impressions", 0)),
                                        "ctr": round((r.get("ctr") or 0) * 100, 2),
                                        "position": round(r.get("position") or 0, 1),
                                    })
                    except Exception:
                        pass  # Fall through to DB rows below

                # Fallback: use existing query rows from DB (filtered by page URL)
                if not query_data:
                    # GscQueryRow doesn't have page association in CSV imports,
                    # so we just return the top queries for the property as context
                    q_stmt = (
                        select(GscQueryRow)
                        .where(GscQueryRow.property_id == guide.gsc_property_id)
                        .order_by(desc(GscQueryRow.clicks))
                        .limit(20)
                    )
                    db_queries = (await db.execute(q_stmt)).scalars().all()
                    for q in db_queries:
                        query_data.append({
                            "query": q.query,
                            "clicks": q.clicks,
                            "impressions": q.impressions,
                            "ctr": round((q.ctr or 0) * 100, 2),
                            "position": round(q.position or 0, 1),
                        })

            # ── 4. Build the LLM prompt ────────────────────────────────────
            prompt_parts = [f"URL: {url}\n"]

            if audit_sections:
                prompt_parts.append("AUDIT SCORES:\n" + "\n".join(audit_sections))
            else:
                prompt_parts.append("AUDIT SCORES: No audit data available")

            if all_issues:
                prompt_parts.append(
                    "\nTOP AUDIT FINDINGS (deduplicated across all audit types):\n"
                    + "\n".join(f"• {i}" for i in all_issues[:10])
                )

            if gsc_page_data:
                ctr_pct = round((gsc_page_data.ctr or 0) * 100, 2)
                prompt_parts.append(
                    f"\nGSC PAGE METRICS (last 90 days, property: {gsc_property_name}):\n"
                    f"Clicks: {gsc_page_data.clicks:,} | "
                    f"Impressions: {gsc_page_data.impressions:,} | "
                    f"CTR: {ctr_pct}% | "
                    f"Avg Position: {round(gsc_page_data.position or 0, 1)}"
                )
            elif guide.gsc_property_id:
                prompt_parts.append("\nGSC PAGE METRICS: This specific page was not found in GSC data.")

            if query_data:
                lines = []
                for i, q in enumerate(query_data[:20], 1):
                    lines.append(
                        f'{i}. "{q["query"]}" — {q["clicks"]} clicks, '
                        f'pos {q["position"]}, CTR {q["ctr"]}%'
                    )
                header = "TOP QUERIES FOR THIS PAGE:" if token_row else "TOP QUERIES (property-level, CSV data):"
                prompt_parts.append("\n" + header + "\n" + "\n".join(lines))

            user_prompt = "\n\n".join(prompt_parts)
            user_prompt += (
                "\n\nBased on all the above data, generate a comprehensive GEO & SEO "
                "improvement guide for this URL. Focus on actionable recommendations."
            )

            # ── 5. Call LLM ───────────────────────────────────────────────
            response_text, _, _ = await call_llm_for_schema(
                provider=provider,
                model=model,
                system_prompt=_SYSTEM_PROMPT,
                user_content=user_prompt,
                max_tokens=4096,
            )

            # ── 6. Parse JSON ─────────────────────────────────────────────
            # Strip markdown code fences if present
            clean = re.sub(r"^```(?:json)?\s*", "", response_text.strip(), flags=re.IGNORECASE)
            clean = re.sub(r"\s*```$", "", clean.strip())
            guide_data = json.loads(clean)

            guide.guide_json = json.dumps(guide_data)
            guide.status = "completed"

        except Exception as exc:
            guide.status = "failed"
            guide.error_message = str(exc)

        guide.updated_at = datetime.utcnow()
        await db.commit()


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class GenerateGuideRequest(BaseModel):
    url: str
    gsc_property_id: Optional[str] = None
    provider: Optional[str] = "anthropic"
    model: Optional[str] = "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/generate")
async def generate_guide(req: GenerateGuideRequest):
    """Create a new guide generation job and start it in the background."""
    async with AsyncSessionLocal() as db:
        guide = UrlGuide(
            url=req.url,
            status="pending",
            provider=req.provider,
            model=req.model,
            gsc_property_id=req.gsc_property_id,
        )
        db.add(guide)
        await db.commit()
        await db.refresh(guide)
        guide_id = guide.id

    create_tracked_task(_generate_guide(guide_id), name=f"guide-generate-{guide_id}", timeout=600)
    return {"guide_id": guide_id, "status": "pending"}


@router.get("/by-url")
async def list_guides_by_url(url: str):
    """List all guides for a given URL, newest first."""
    async with AsyncSessionLocal() as db:
        stmt = (
            select(UrlGuide)
            .where(UrlGuide.url == url)
            .order_by(desc(UrlGuide.created_at))
        )
        guides = (await db.execute(stmt)).scalars().all()
        return [
            {
                "id": g.id,
                "status": g.status,
                "provider": g.provider,
                "model": g.model,
                "gsc_property_id": g.gsc_property_id,
                "created_at": g.created_at.isoformat() if g.created_at else None,
                "updated_at": g.updated_at.isoformat() if g.updated_at else None,
            }
            for g in guides
        ]


@router.get("/{guide_id}")
async def get_guide(guide_id: int):
    """Fetch guide status and result."""
    async with AsyncSessionLocal() as db:
        guide = await db.get(UrlGuide, guide_id)
        if not guide:
            raise_not_found("Guide")

        result = {
            "id": guide.id,
            "url": guide.url,
            "status": guide.status,
            "provider": guide.provider,
            "model": guide.model,
            "gsc_property_id": guide.gsc_property_id,
            "error_message": guide.error_message,
            "reviewed": bool(guide.reviewed),
            "created_at": guide.created_at.isoformat() if guide.created_at else None,
            "updated_at": guide.updated_at.isoformat() if guide.updated_at else None,
            "guide_json": None,
        }
        if guide.guide_json:
            try:
                gj = json.loads(guide.guide_json)
                # Repair any {"raw": "..."} entries stored when LLM parsing failed
                if isinstance(gj, dict) and "results" in gj:
                    try:
                        from json_repair import repair_json
                        for key, val in gj["results"].items():
                            if isinstance(val, dict) and "raw" in val and isinstance(val["raw"], str):
                                repaired = repair_json(val["raw"], return_objects=True)
                                if isinstance(repaired, dict) and repaired:
                                    gj["results"][key] = repaired
                    except Exception:
                        pass
                result["guide_json"] = gj
            except json.JSONDecodeError:
                result["guide_json"] = None

        return result


@router.patch("/{guide_id}/reviewed")
async def mark_guide_reviewed(guide_id: int, reviewed: bool = True):
    """Toggle the reviewed flag on a guide."""
    async with AsyncSessionLocal() as db:
        guide = await db.get(UrlGuide, guide_id)
        if not guide:
            raise_not_found("Guide")
        guide.reviewed = reviewed
        guide.updated_at = datetime.utcnow()
        await db.commit()
    return {"ok": True, "reviewed": reviewed}


@router.delete("/{guide_id}")
async def delete_guide(guide_id: int):
    """Delete a guide."""
    async with AsyncSessionLocal() as db:
        guide = await db.get(UrlGuide, guide_id)
        if not guide:
            raise_not_found("Guide")
        await db.delete(guide)
        await db.commit()
    return {"ok": True}
