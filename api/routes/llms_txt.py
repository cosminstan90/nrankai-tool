"""
llms.txt Generator — creates a valid /llms.txt file for a website.

The llms.txt specification (llmstxt.org) uses Markdown with:
  - H1  : site/project name
  - Blockquote : short summary paragraph
  - Body text  : optional intro
  - H2 sections: curated resource lists  [Title](url): description
  - ## Optional section for secondary / skippable content

Endpoints
---------
POST   /api/llms-txt/jobs               create job + start background generation
GET    /api/llms-txt/jobs               list all jobs (newest first)
GET    /api/llms-txt/jobs/{id}          job detail + generated content
GET    /api/llms-txt/jobs/{id}/download raw llms.txt file download
DELETE /api/llms-txt/jobs/{id}          delete job
"""

import asyncio
import re
import uuid
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from api.utils.task_runner import create_tracked_task

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy import delete as sql_delete

from api.models.database import (
    AsyncSessionLocal,
    LlmsTxtJob,
    Audit,
    AuditResult,
    GscProperty,
    GscPageRow,
)
from api.routes.costs import track_cost

router = APIRouter(prefix="/api/llms-txt", tags=["llms-txt"])

# ── Default models per provider ───────────────────────────────────────────────
_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai":    "gpt-4o-mini",
    "mistral":   "mistral-small-latest",
    "google":    "gemini-2.0-flash",
}

# ── LLM system prompt ─────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are an expert technical writer creating an llms.txt file for a website.

The llms.txt specification uses Markdown to help AI models navigate and understand a website.

Given a list of pages with their URLs, titles (if known), and optional performance data,
you must produce ONLY a valid llms.txt file in this EXACT structure:

# [Site Name]

> [1-2 sentence summary of what this website is about, including its main purpose and audience]

[1 short paragraph of additional context about the site — what it offers, who it serves]

## [Section Name]

- [Page Title](https://url/): Brief description of what this page contains
- [Page Title](https://url/): Brief description

[Repeat H2 sections for logical groups: e.g. Services, Blog, About, Contact, etc.]

## Optional

- [Less important page](https://url/): Description

RULES:
- Every URL must appear EXACTLY as provided — do not modify URLs
- Section names must be concise (1-3 words): e.g. "Main Pages", "Services", "Blog", "About", "Products"
- Descriptions must be 5-15 words, factual, starting with a verb or noun
- Put the most important pages first in dedicated sections
- Put secondary/boilerplate pages (privacy, contact, login) in ## Optional
- Do NOT add any text before the H1 or after the last list item
- Output ONLY the markdown, no code fences, no explanation"""

# ── Pydantic models ───────────────────────────────────────────────────────────

class CreateJobRequest(BaseModel):
    name:            Optional[str] = None
    site_url:        str
    site_name:       Optional[str] = None
    audit_id:        Optional[str] = None
    gsc_property_id: Optional[str] = None
    llm_provider:    str = "anthropic"
    llm_model:       Optional[str] = None


# ── URL helpers ───────────────────────────────────────────────────────────────

def _normalise_url(url: str) -> str:
    """Ensure URL has a scheme."""
    url = url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return "https://" + url


def _site_root(site_url: str) -> str:
    """Return scheme + netloc, e.g. https://example.com"""
    parsed = urlparse(_normalise_url(site_url))
    return f"{parsed.scheme}://{parsed.netloc}"


def _path_score(url: str) -> int:
    """Lower = more important. Home → 0, top-level → 1, deep → 2+."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    return len(parts)


def _guess_title(url: str) -> str:
    """Derive a human title from a URL path segment."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return "Home"
    last = path.split("/")[-1]
    # Remove extension
    last = re.sub(r'\.(html?|php|aspx?)$', '', last, flags=re.IGNORECASE)
    # Slugs → Title Case
    return re.sub(r'[-_]+', ' ', last).title()


# ── Background generation task ────────────────────────────────────────────────

async def _generate_llms_txt(job_id: str) -> None:
    """Background task: collect pages, call LLM, write llms.txt content."""
    from api.routes.schema_gen import call_llm_for_schema

    async with AsyncSessionLocal() as db:
        job = await db.get(LlmsTxtJob, job_id)
        if not job:
            return

        async def _upd(status: str = None, progress: int = None, msg: str = None):
            if status   is not None: job.status           = status
            if progress is not None: job.progress         = progress
            if msg      is not None: job.progress_message = msg
            await db.commit()

        try:
            await _upd(status="running", progress=5, msg="Collecting pages…")

            site_root = _site_root(job.site_url)
            pages: dict[str, dict] = {}   # url → info dict

            # ── 1. Collect from audit results ──────────────────────────────
            if job.audit_id:
                audit = await db.get(Audit, job.audit_id)
                if audit:
                    results = (await db.execute(
                        select(AuditResult)
                        .where(AuditResult.audit_id == job.audit_id)
                        .order_by(AuditResult.page_url)
                    )).scalars().all()

                    for r in results:
                        url = r.page_url.strip()
                        if url and url not in pages:
                            pages[url] = {
                                "url":         url,
                                "title":       _guess_title(url),
                                "audit_score": r.score,
                                "audit_type":  r.audit_type,
                                "depth":       _path_score(url),
                            }

            # ── 2. Enrich from GSC pages ───────────────────────────────────
            if job.gsc_property_id:
                gsc_rows = (await db.execute(
                    select(GscPageRow)
                    .where(GscPageRow.property_id == job.gsc_property_id)
                    .order_by(GscPageRow.clicks.desc())
                )).scalars().all()

                for r in gsc_rows:
                    url = r.page.strip()
                    if url not in pages:
                        pages[url] = {
                            "url":   url,
                            "title": _guess_title(url),
                            "depth": _path_score(url),
                        }
                    pages[url]["gsc_clicks"]   = r.clicks
                    pages[url]["gsc_position"] = round(r.position, 1) if r.position else None

            await _upd(progress=25, msg=f"Found {len(pages)} pages — asking LLM…")

            if not pages:
                # Fallback: just use the site URL itself
                pages[site_root + "/"] = {
                    "url": site_root + "/",
                    "title": "Home",
                    "depth": 0,
                }

            # ── 3. Build the LLM prompt ────────────────────────────────────
            # Sort by depth then clicks descending — most important first
            sorted_pages = sorted(
                pages.values(),
                key=lambda p: (p.get("depth", 99), -(p.get("gsc_clicks") or 0))
            )

            site_name = job.site_name or _guess_title(job.site_url) or "Website"

            page_lines = []
            for p in sorted_pages[:120]:   # cap at 120 pages to fit context
                line = f"- {p['title']} | {p['url']}"
                extras = []
                if p.get("gsc_clicks"):
                    extras.append(f"clicks={p['gsc_clicks']}")
                if p.get("gsc_position"):
                    extras.append(f"pos={p['gsc_position']}")
                if p.get("audit_score") is not None:
                    extras.append(f"score={round(p['audit_score'])}")
                if extras:
                    line += f" ({', '.join(extras)})"
                page_lines.append(line)

            user_content = (
                f"Site name: {site_name}\n"
                f"Site URL: {site_root}\n\n"
                f"Pages ({len(page_lines)} total):\n"
                + "\n".join(page_lines)
            )

            # ── 4. Call LLM ────────────────────────────────────────────────
            await _upd(progress=40, msg="Generating with LLM…")

            provider = job.llm_provider.upper()
            model    = job.llm_model or _DEFAULT_MODELS.get(job.llm_provider, "claude-haiku-4-5-20251001")

            text, in_tok, out_tok = await call_llm_for_schema(
                provider     = provider,
                model        = model,
                system_prompt= _SYSTEM_PROMPT,
                user_content = user_content,
                max_tokens   = 4096,
            )
            create_tracked_task(track_cost(
                source="llms_txt",
                provider=job.llm_provider,
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                source_id=job.id,
                website=job.site_url,
            ), name=f"llms-txt-track-cost-{job.id}", timeout=300)

            await _upd(progress=85, msg="Validating output…")

            # ── 5. Validate + clean ────────────────────────────────────────
            content = text.strip()

            # Ensure it starts with H1
            if not content.startswith("# "):
                content = f"# {site_name}\n\n" + content

            # Count links in output
            link_count = len(re.findall(r'\[.+?\]\(https?://', content))

            # ── 6. Persist ────────────────────────────────────────────────
            job.generated_content = content
            job.page_count        = link_count
            job.completed_at      = datetime.utcnow()
            await _upd(status="completed", progress=100, msg=f"Done — {link_count} pages indexed")

        except Exception as exc:
            job.error = str(exc)[:1000]
            await _upd(status="failed", progress=0, msg=f"Error: {str(exc)[:200]}")


# ── CRUD endpoints ─────────────────────────────────────────────────────────────

@router.post("/jobs", status_code=201)
async def create_job(req: CreateJobRequest):
    """Create a new llms.txt generation job and start the background task."""
    if not req.site_url.strip():
        raise HTTPException(status_code=400, detail="site_url is required.")

    job_id = str(uuid.uuid4())
    site_url = _normalise_url(req.site_url.strip())

    async with AsyncSessionLocal() as db:
        job = LlmsTxtJob(
            id              = job_id,
            name            = req.name or f"llms.txt — {site_url}",
            site_url        = site_url,
            site_name       = req.site_name or None,
            audit_id        = req.audit_id or None,
            gsc_property_id = req.gsc_property_id or None,
            llm_provider    = req.llm_provider,
            llm_model       = req.llm_model or None,
            status          = "pending",
            progress        = 0,
            progress_message= "Queued…",
        )
        db.add(job)
        await db.commit()

    create_tracked_task(_generate_llms_txt(job_id), name=f"llms-txt-generate-{job_id}", timeout=300)
    return {"job_id": job_id, "status": "pending"}


@router.get("/jobs")
async def list_jobs():
    """Return all llms.txt jobs, newest first."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(LlmsTxtJob).order_by(LlmsTxtJob.created_at.desc())
        )).scalars().all()

    return [
        {
            "id":             r.id,
            "name":           r.name,
            "site_url":       r.site_url,
            "status":         r.status,
            "progress":       r.progress,
            "progress_message": r.progress_message,
            "page_count":     r.page_count,
            "llm_provider":   r.llm_provider,
            "created_at":     r.created_at.isoformat() if r.created_at else None,
            "completed_at":   r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in rows
    ]


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Return a single job including the generated content."""
    async with AsyncSessionLocal() as db:
        job = await db.get(LlmsTxtJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "id":                job.id,
        "name":              job.name,
        "site_url":          job.site_url,
        "site_name":         job.site_name,
        "audit_id":          job.audit_id,
        "gsc_property_id":   job.gsc_property_id,
        "llm_provider":      job.llm_provider,
        "llm_model":         job.llm_model,
        "status":            job.status,
        "progress":          job.progress,
        "progress_message":  job.progress_message,
        "error":             job.error,
        "generated_content": job.generated_content,
        "page_count":        job.page_count,
        "created_at":        job.created_at.isoformat() if job.created_at else None,
        "completed_at":      job.completed_at.isoformat() if job.completed_at else None,
    }


@router.get("/jobs/{job_id}/download", response_class=PlainTextResponse)
async def download_job(job_id: str):
    """Download the generated llms.txt as a plain-text file."""
    async with AsyncSessionLocal() as db:
        job = await db.get(LlmsTxtJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.generated_content:
        raise HTTPException(status_code=400, detail="Content not yet generated")

    from fastapi.responses import Response
    return Response(
        content=job.generated_content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="llms.txt"'},
    )


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    """Delete a job."""
    async with AsyncSessionLocal() as db:
        await db.execute(sql_delete(LlmsTxtJob).where(LlmsTxtJob.id == job_id))
        await db.commit()
