"""
Multi-Source AI Insights — Haiku-powered page analysis.

Joins data from GSC, GA4, Google Ads, and audit results to produce
prioritised, actionable insight cards per page/query using Claude Haiku.

Endpoints
---------
POST   /api/insights/runs                  create a run + start background task
GET    /api/insights/runs                  list all runs
GET    /api/insights/runs/{id}             run detail + status
GET    /api/insights/runs/{id}/cards       paginated insight cards (filter by issue_type / priority)
DELETE /api/insights/runs/{id}             delete run + cascade cards
"""

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from api.utils.task_runner import create_tracked_task

from fastapi import APIRouter, HTTPException
from api.utils.errors import raise_not_found
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy import delete as sql_delete

from api.models.database import (
    AsyncSessionLocal,
    InsightRun,
    InsightCard,
    GscProperty,
    GscPageRow,
    Ga4Property,
    Ga4PageRow,
    AdsAccount,
    AdsSearchTermRow,
    Audit,
    AuditResult,
)
from api.routes.costs import track_cost

router = APIRouter(prefix="/api/insights", tags=["insights"])

# ── Haiku model to use ────────────────────────────────────────────────────────
_HAIKU_PROVIDER = "anthropic"
_HAIKU_MODEL    = "claude-haiku-4-5-20251001"

# ── Issue type → human label ──────────────────────────────────────────────────
_ISSUE_LABELS = {
    "low_ctr":               "Low CTR",
    "poor_engagement":       "Poor Engagement",
    "ranks_but_bounces":     "Ranks but Bounces",
    "paid_dependency":       "Paid Dependency",
    "organic_opportunity":   "Organic Opportunity",
    "no_audit":              "Not Audited",
    "content_gap":           "Content Gap",
    "near_miss":             "Near Miss",
}

# ── Haiku system prompt ───────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are an SEO and digital marketing analyst. You will receive a JSON array of pages with their performance metrics. Analyse each page and classify the main issue.

For EACH page in the input array, return ONE object in the output array with these exact fields:
- page_or_query: string (copy the URL or query exactly as given)
- issue_type: one of [low_ctr, poor_engagement, ranks_but_bounces, paid_dependency, organic_opportunity, no_audit, content_gap, near_miss]
- priority: one of [high, medium, low]
- reason: 1-2 sentences explaining the issue based on the metrics
- action: 1 concrete, specific recommended action

Issue type selection guide:
- low_ctr: GSC impressions are high but CTR is below 3%
- poor_engagement: GA4 shows high bounce rate (>60%) or very short engagement time
- ranks_but_bounces: Good GSC position (1-10) but high GA4 bounce rate
- paid_dependency: Term appears in Ads but not ranking organically in GSC
- organic_opportunity: Good GSC position but no Ads coverage (could amplify with paid)
- no_audit: Page has traffic but has never been audited (audit_score is null)
- content_gap: Page has impressions but very few clicks AND low audit score
- near_miss: GSC position between 4-20 with good impressions (close to page 1)

Respond with ONLY a valid JSON array. No explanation, no markdown, no code blocks. Just the raw JSON array."""


# ── Pydantic models ───────────────────────────────────────────────────────────

class CreateRunRequest(BaseModel):
    name:             str
    gsc_property_id:  Optional[str] = None
    ga4_property_id:  Optional[str] = None
    ads_account_id:   Optional[str] = None
    audit_id:         Optional[str] = None


# ── Background task ───────────────────────────────────────────────────────────

def _normalise_url(url: str) -> str:
    """Lowercase, strip trailing slash."""
    return (url or "").rstrip("/").lower()


def _extract_json_safe(text: str) -> list:
    """Extract a JSON array from LLM response, handling markdown fences."""
    text = text.strip()
    # Strip markdown fences if present
    text = re.sub(r"^```[a-z]*\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n?```$", "", text, flags=re.MULTILINE)
    text = text.strip()
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, ValueError):
        # Try to find a JSON array inside the text
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except (json.JSONDecodeError, ValueError):
                pass
    return []


async def _run_insights(run_id: str) -> None:
    """
    Background task: collect metrics from all selected data sources,
    batch pages to Haiku, insert InsightCard rows.
    """
    # Import here to avoid circular imports
    from api.routes.schema_gen import call_llm_for_schema

    async with AsyncSessionLocal() as db:
        run = await db.get(InsightRun, run_id)
        if not run:
            return

        async def _upd(status: str = None, progress: int = None, msg: str = None):
            if status   is not None: run.status           = status
            if progress is not None: run.progress         = progress
            if msg      is not None: run.progress_message = msg
            await db.commit()

        try:
            await _upd(status="running", progress=5, msg="Collecting data…")

            # ── Step 1: Collect pages and metrics ──────────────────────────
            # page_norm → metrics dict
            pages: dict[str, dict] = {}

            # ── GSC pages ──────────────────────────────────────────────────
            if run.gsc_property_id:
                gsc_rows = (await db.execute(
                    select(GscPageRow).where(GscPageRow.property_id == run.gsc_property_id)
                )).scalars().all()

                for r in gsc_rows:
                    norm = _normalise_url(r.page)
                    if norm not in pages:
                        pages[norm] = {"url": r.page}
                    pages[norm]["gsc_clicks"]      = r.clicks
                    pages[norm]["gsc_impressions"] = r.impressions
                    pages[norm]["gsc_ctr"]         = round(r.ctr * 100, 2)      if r.ctr is not None      else None
                    pages[norm]["gsc_position"]    = round(r.position, 1)       if r.position is not None else None

            # ── GA4 pages ──────────────────────────────────────────────────
            if run.ga4_property_id:
                ga4_rows = (await db.execute(
                    select(Ga4PageRow).where(Ga4PageRow.property_id == run.ga4_property_id)
                )).scalars().all()

                for r in ga4_rows:
                    norm = _normalise_url(r.page)
                    if norm not in pages:
                        pages[norm] = {"url": r.page}
                    pages[norm]["ga4_sessions"]    = r.sessions
                    pages[norm]["ga4_bounce_rate"] = round(r.bounce_rate * 100, 1) if r.bounce_rate is not None else None
                    pages[norm]["ga4_engagement"]  = round(r.avg_engagement_time, 1) if r.avg_engagement_time is not None else None

            # ── Ads search terms (as "pages") ──────────────────────────────
            if run.ads_account_id:
                ads_rows = (await db.execute(
                    select(AdsSearchTermRow).where(AdsSearchTermRow.account_id == run.ads_account_id)
                )).scalars().all()

                for r in ads_rows:
                    # Search terms are queries, not pages — use term as key
                    key = (r.search_term or "").lower().strip()
                    if key not in pages:
                        pages[key] = {"url": r.search_term}
                    pages[key]["ads_cost"]   = round(r.cost, 2) if r.cost is not None else None
                    pages[key]["ads_clicks"] = r.clicks

            # ── Audit results ──────────────────────────────────────────────
            if run.audit_id:
                audit_rows = (await db.execute(
                    select(AuditResult.page_url, func.max(AuditResult.score))
                    .where(AuditResult.audit_id == run.audit_id)
                    .group_by(AuditResult.page_url)
                )).all()

                for page_url, score in audit_rows:
                    norm = _normalise_url(page_url)
                    if norm not in pages:
                        pages[norm] = {"url": page_url}
                    pages[norm]["audit_score"] = score

            if not pages:
                await _upd(status="completed", progress=100, msg="No pages found in selected sources.")
                return

            await _upd(progress=20, msg=f"Collected {len(pages)} pages. Starting analysis…")

            # ── Step 2: Batch pages to Haiku (30 per batch) ────────────────
            page_list     = list(pages.values())
            batch_size    = 30
            total_batches = (len(page_list) + batch_size - 1) // batch_size
            cards_inserted = 0

            for batch_idx in range(total_batches):
                batch = page_list[batch_idx * batch_size : (batch_idx + 1) * batch_size]

                # Build Haiku input
                haiku_input = []
                for item in batch:
                    entry = {"page_or_query": item.get("url", "")}
                    if item.get("gsc_clicks")      is not None: entry["gsc_clicks"]      = item["gsc_clicks"]
                    if item.get("gsc_impressions") is not None: entry["gsc_impressions"] = item["gsc_impressions"]
                    if item.get("gsc_ctr")         is not None: entry["gsc_ctr_pct"]     = item["gsc_ctr"]
                    if item.get("gsc_position")    is not None: entry["gsc_position"]    = item["gsc_position"]
                    if item.get("ga4_sessions")    is not None: entry["ga4_sessions"]    = item["ga4_sessions"]
                    if item.get("ga4_bounce_rate") is not None: entry["ga4_bounce_rate_pct"] = item["ga4_bounce_rate"]
                    if item.get("ga4_engagement")  is not None: entry["ga4_engagement_secs"] = item["ga4_engagement"]
                    if item.get("ads_cost")        is not None: entry["ads_cost"]        = item["ads_cost"]
                    if item.get("ads_clicks")      is not None: entry["ads_clicks"]      = item["ads_clicks"]
                    if item.get("audit_score")     is not None: entry["audit_score"]     = item["audit_score"]
                    haiku_input.append(entry)

                user_content = json.dumps(haiku_input, ensure_ascii=False)

                try:
                    text, in_tok, out_tok = await call_llm_for_schema(
                        provider     = _HAIKU_PROVIDER,
                        model        = _HAIKU_MODEL,
                        system_prompt= _SYSTEM_PROMPT,
                        user_content = user_content,
                        max_tokens   = 4096,
                    )
                    create_tracked_task(track_cost(
                        source="insights",
                        provider=_HAIKU_PROVIDER,
                        model=_HAIKU_MODEL,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        source_id=run_id,
                    ), name=f"insights-track-cost-{run_id}", timeout=300)
                    parsed_cards = _extract_json_safe(text)
                except Exception as llm_exc:
                    # Skip batch on error, continue with next
                    parsed_cards = []

                # Insert insight cards
                for card_data in parsed_cards:
                    if not isinstance(card_data, dict):
                        continue

                    page_key = (card_data.get("page_or_query") or "").rstrip("/").lower()
                    metrics  = pages.get(page_key, {})

                    issue_type = card_data.get("issue_type", "content_gap")
                    priority   = card_data.get("priority",   "medium")

                    # Validate enum values
                    if issue_type not in _ISSUE_LABELS:
                        issue_type = "content_gap"
                    if priority not in {"high", "medium", "low"}:
                        priority = "medium"

                    db.add(InsightCard(
                        run_id        = run_id,
                        page_or_query = card_data.get("page_or_query") or metrics.get("url", ""),
                        issue_type    = issue_type,
                        priority      = priority,
                        reason        = str(card_data.get("reason", "")).strip(),
                        action        = str(card_data.get("action", "")).strip(),
                        gsc_clicks      = metrics.get("gsc_clicks"),
                        gsc_impressions = metrics.get("gsc_impressions"),
                        gsc_ctr         = metrics.get("gsc_ctr"),
                        gsc_position    = metrics.get("gsc_position"),
                        ga4_sessions    = metrics.get("ga4_sessions"),
                        ga4_bounce_rate = metrics.get("ga4_bounce_rate"),
                        ga4_engagement  = metrics.get("ga4_engagement"),
                        ads_cost        = metrics.get("ads_cost"),
                        ads_clicks      = metrics.get("ads_clicks"),
                        audit_score     = metrics.get("audit_score"),
                    ))
                    cards_inserted += 1

                await db.commit()

                # Update progress
                progress = 20 + int(80 * (batch_idx + 1) / total_batches)
                await _upd(
                    progress = progress,
                    msg      = f"Processed batch {batch_idx + 1}/{total_batches} — {cards_inserted} insights so far…",
                )

            # ── Step 3: Finalise ───────────────────────────────────────────
            run.total_cards = cards_inserted
            await _upd(status="completed", progress=100, msg=f"Done — {cards_inserted} insights generated.")

        except Exception as exc:
            import traceback
            run.status           = "failed"
            run.progress_message = f"Error: {exc}"
            await db.commit()
            traceback.print_exc()


# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.post("/runs", status_code=201)
async def create_run(req: CreateRunRequest):
    """Create an insight run and start background Haiku analysis."""
    if not any([req.gsc_property_id, req.ga4_property_id,
                req.ads_account_id, req.audit_id]):
        raise HTTPException(
            status_code=400,
            detail="At least one data source must be selected."
        )

    run_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        run = InsightRun(
            id              = run_id,
            name            = req.name.strip() or f"Insight Run {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
            gsc_property_id = req.gsc_property_id or None,
            ga4_property_id = req.ga4_property_id or None,
            ads_account_id  = req.ads_account_id  or None,
            audit_id        = req.audit_id        or None,
            status          = "pending",
            progress        = 0,
            progress_message= "Queued…",
        )
        db.add(run)
        await db.commit()

    create_tracked_task(_run_insights(run_id), name=f"insights-run-{run_id}", timeout=300)
    return {"run_id": run_id, "status": "pending"}


@router.get("/runs")
async def list_runs():
    """Return all insight runs, newest first."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(InsightRun).order_by(InsightRun.created_at.desc())
        )).scalars().all()

    return [
        {
            "id":               r.id,
            "name":             r.name,
            "gsc_property_id":  r.gsc_property_id,
            "ga4_property_id":  r.ga4_property_id,
            "ads_account_id":   r.ads_account_id,
            "audit_id":         r.audit_id,
            "status":           r.status,
            "progress":         r.progress,
            "progress_message": r.progress_message,
            "total_cards":      r.total_cards,
            "created_at":       r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Return run detail + status."""
    async with AsyncSessionLocal() as db:
        run = await db.get(InsightRun, run_id)
    if not run:
        raise_not_found("Run")
    return {
        "id":               run.id,
        "name":             run.name,
        "gsc_property_id":  run.gsc_property_id,
        "ga4_property_id":  run.ga4_property_id,
        "ads_account_id":   run.ads_account_id,
        "audit_id":         run.audit_id,
        "status":           run.status,
        "progress":         run.progress,
        "progress_message": run.progress_message,
        "total_cards":      run.total_cards,
        "created_at":       run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/runs/{run_id}/cards")
async def get_cards(
    run_id:     str,
    issue_type: str = "",
    priority:   str = "",
    q:          str = "",
    page:       int = 0,
    page_size:  int = 50,
):
    """Return paginated insight cards, optionally filtered by issue_type, priority, or text search."""
    async with AsyncSessionLocal() as db:
        stmt = select(InsightCard).where(InsightCard.run_id == run_id)
        if issue_type:
            stmt = stmt.where(InsightCard.issue_type == issue_type)
        if priority:
            stmt = stmt.where(InsightCard.priority == priority)
        if q:
            stmt = stmt.where(InsightCard.page_or_query.ilike(f"%{q}%"))

        # Order: high priority first, then medium, then low
        from sqlalchemy import case as sa_case
        priority_order = sa_case(
            (InsightCard.priority == "high",   1),
            (InsightCard.priority == "medium", 2),
            (InsightCard.priority == "low",    3),
            else_=4,
        )
        stmt = stmt.order_by(priority_order, InsightCard.id)

        total = (await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar_one()

        items = (await db.execute(
            stmt.offset(page * page_size).limit(page_size)
        )).scalars().all()

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "items": [
            {
                "id":            c.id,
                "page_or_query": c.page_or_query,
                "issue_type":    c.issue_type,
                "issue_label":   _ISSUE_LABELS.get(c.issue_type, c.issue_type),
                "priority":      c.priority,
                "reason":        c.reason,
                "action":        c.action,
                # Source metrics
                "gsc_clicks":      c.gsc_clicks,
                "gsc_impressions": c.gsc_impressions,
                "gsc_ctr":         c.gsc_ctr,
                "gsc_position":    c.gsc_position,
                "ga4_sessions":    c.ga4_sessions,
                "ga4_bounce_rate": c.ga4_bounce_rate,
                "ga4_engagement":  c.ga4_engagement,
                "ads_cost":        c.ads_cost,
                "ads_clicks":      c.ads_clicks,
                "audit_score":     c.audit_score,
            }
            for c in items
        ],
    }


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str):
    """Delete a run and all its insight cards (CASCADE)."""
    async with AsyncSessionLocal() as db:
        await db.execute(sql_delete(InsightCard).where(InsightCard.run_id == run_id))
        await db.execute(sql_delete(InsightRun).where(InsightRun.id == run_id))
        await db.commit()
    return {"success": True}
