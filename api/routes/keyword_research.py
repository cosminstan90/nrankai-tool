"""
Keyword Research Tool — DataForSEO + LLM question detection.

Two-pass keyword expansion:
  Pass 0 : seed keywords as-is
  Pass 1 : DataForSEO keywords_for_keywords on seeds
  Pass 2 : DataForSEO keywords_for_keywords on top-N pass-1 results (by volume)

After expansion, an LLM scans the full keyword list and flags questions.

Endpoints
---------
POST   /api/keyword-research/sessions               create session + start background task
GET    /api/keyword-research/sessions               list sessions
GET    /api/keyword-research/sessions/{id}/status   poll progress
DELETE /api/keyword-research/sessions/{id}          delete session + results
"""

import asyncio
import base64
import json
import os
import re
import uuid
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy import delete as sql_delete

from api.models.database import AsyncSessionLocal, KeywordSession, KeywordResult

router = APIRouter(prefix="/api/keyword-research", tags=["keyword-research"])


# ── Location / Language presets (DataForSEO location codes) ──────────────────
LOCATION_PRESETS: dict = {
    "RO": {"location_code": 1037, "language_code": "ro", "language_name": "Romanian"},
    "US": {"location_code": 2840, "language_code": "en", "language_name": "English (US)"},
    "UK": {"location_code": 2826, "language_code": "en", "language_name": "English (UK)"},
    "DE": {"location_code": 2276, "language_code": "de", "language_name": "German"},
    "FR": {"location_code": 2250, "language_code": "fr", "language_name": "French"},
    "IT": {"location_code": 2380, "language_code": "it", "language_name": "Italian"},
    "ES": {"location_code": 2724, "language_code": "es", "language_name": "Spanish"},
    "PL": {"location_code": 2616, "language_code": "pl", "language_name": "Polish"},
    "NL": {"location_code": 2528, "language_code": "nl", "language_name": "Dutch"},
    "BG": {"location_code": 2100, "language_code": "bg", "language_name": "Bulgarian"},
}

LLM_DEFAULT_MODELS: dict = {
    "ANTHROPIC":  "claude-haiku-4-5-20251001",
    "OPENAI":     "gpt-4o-mini",
    "MISTRAL":    "mistral-small-latest",
    "GOOGLE":     "gemini-2.0-flash-lite",
    "PERPLEXITY": "sonar",
}


# ── Pydantic request models ───────────────────────────────────────────────────
class CreateSessionRequest(BaseModel):
    name:          str
    seed_keywords: List[str]
    location:      str = "RO"
    pass2_limit:   int = 50
    llm_provider:  str = "anthropic"

    @field_validator("seed_keywords")
    @classmethod
    def validate_seeds(cls, v: list) -> list:
        cleaned = [k.strip() for k in v if k.strip()]
        if not cleaned:
            raise ValueError("At least one seed keyword is required")
        if len(cleaned) > 20:
            raise ValueError("Maximum 20 seed keywords per session")
        return cleaned

    @field_validator("pass2_limit")
    @classmethod
    def validate_pass2(cls, v: int) -> int:
        if not (1 <= v <= 200):
            raise ValueError("pass2_limit must be between 1 and 200")
        return v


# ── DataForSEO helpers ────────────────────────────────────────────────────────
def _dfs_configured() -> bool:
    return bool(os.getenv("DATAFORSEO_LOGIN") and os.getenv("DATAFORSEO_PASSWORD"))


def _dfs_auth() -> str:
    login = os.getenv("DATAFORSEO_LOGIN", "")
    pw    = os.getenv("DATAFORSEO_PASSWORD", "")
    return "Basic " + base64.b64encode(f"{login}:{pw}".encode()).decode()


async def _dfs_keywords_for_keywords(
    keywords:      List[str],
    location_code: int,
    language_code: str,
) -> List[dict]:
    """
    Call DataForSEO keywords_for_keywords/live.
    Batches in groups of 200 (API maximum).
    Returns deduplicated list of {keyword, search_volume, cpc, competition}.
    """
    if not _dfs_configured() or not keywords:
        return []

    seen:    set  = set()
    results: list = []
    BATCH   = 200

    async with httpx.AsyncClient(timeout=90.0) as client:
        for i in range(0, len(keywords), BATCH):
            batch = keywords[i : i + BATCH]
            payload = [{
                "keywords":      batch,
                "location_code": location_code,
                "language_code": language_code,
            }]
            try:
                resp = await client.post(
                    "https://api.dataforseo.com/v3/keywords_data/google_ads"
                    "/keywords_for_keywords/live",
                    headers={
                        "Authorization":  _dfs_auth(),
                        "Content-Type":   "application/json",
                    },
                    json=payload,
                )
                data = resp.json()
            except Exception as exc:
                print(f"[keyword_research] DataForSEO request error: {exc}")
                continue

            for task in data.get("tasks", []):
                if task.get("status_code") != 20000:
                    print(f"[keyword_research] DataForSEO task error: {task.get('status_message')}")
                    continue
                for result_item in (task.get("result") or []):
                    for kw_item in (result_item.get("items") or []):
                        kw = (kw_item.get("keyword") or "").strip()
                        if kw and kw.lower() not in seen:
                            seen.add(kw.lower())
                            results.append({
                                "keyword":       kw,
                                "search_volume": kw_item.get("search_volume"),
                                "cpc":           kw_item.get("cpc"),
                                "competition":   kw_item.get("competition"),
                            })

    return results


# ── LLM question detection ────────────────────────────────────────────────────
_QUESTION_SYSTEM = """\
You are a keyword analyst specialising in identifying search queries that are questions.

Given a list of keywords/phrases, identify ALL that are questions or clearly imply a user
asking something and seeking an answer. Be inclusive — if in doubt, include it.

Include keywords that:
• Start with question words in ANY language:
  - English : what, how, why, when, where, who, which, is, are, can, does, will, should,
               would, could, do, has, have, was, were
  - Romanian: ce, cum, de ce, când, unde, cine, care, este, sunt, poate, are, va, trebuie,
               care-i, cat, cât, câte
  - German  : was, wie, warum, wann, wo, wer, welche, welcher, welches, kann, darf, soll
  - French  : quoi, comment, pourquoi, quand, où, qui, quel, quelle, est-ce, peut-on
  - Spanish : qué, cómo, por qué, cuándo, dónde, quién, cuál, puedo, hay
  - Italian : cosa, come, perché, quando, dove, chi, quale, si può, è possibile
• Express curiosity or seek information even without explicit question words
• Could serve as the basis of an FAQ entry

Return ONLY valid JSON with NO extra text:
{"questions": ["exact keyword text from input", ...]}\
"""


def _extract_json_safe(text: str) -> dict:
    """Robustly extract JSON from LLM output (handles markdown fences, leading text)."""
    text = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"questions": []}


async def _detect_questions_llm(
    keywords:  List[str],
    provider:  str,
    model:     str,
) -> set:
    """
    Batch all keywords through the LLM to identify questions.
    Returns a set of keyword strings that are questions.
    """
    from api.routes.schema_gen import call_llm_for_schema  # reuse existing multi-provider helper

    question_set: set = set()
    BATCH = 200

    for i in range(0, len(keywords), BATCH):
        batch   = keywords[i : i + BATCH]
        kw_list = "\n".join(f"- {kw}" for kw in batch)
        try:
            text, _, _ = await call_llm_for_schema(
                provider     = provider,
                model        = model,
                system_prompt= _QUESTION_SYSTEM,
                user_content = f"Identify which of these keywords are questions:\n\n{kw_list}",
                max_tokens   = 4096,
            )
            parsed = _extract_json_safe(text)
            question_set.update(q.strip() for q in parsed.get("questions", []))
        except Exception as exc:
            print(f"[keyword_research] LLM batch {i // BATCH + 1} error: {exc}")

    return question_set


# ── Background processing task ────────────────────────────────────────────────
async def _run_session(session_id: str) -> None:
    async with AsyncSessionLocal() as db:
        session = await db.get(KeywordSession, session_id)
        if not session:
            return

        async def _upd(status: str = None, progress: int = None, msg: str = None):
            if status   is not None: session.status           = status
            if progress is not None: session.progress         = progress
            if msg      is not None: session.progress_message = msg
            await db.commit()

        try:
            await _upd(status="running", progress=5, msg="Starting keyword expansion…")

            loc_preset  = LOCATION_PRESETS.get(session.location_key, LOCATION_PRESETS["RO"])
            loc_code    = loc_preset["location_code"]
            lang_code   = loc_preset["language_code"]
            seeds: list = session.seed_keywords  # list[str]

            # ── Pass 1: expand all seed keywords ──────────────────────────────
            await _upd(progress=10, msg=f"Pass 1: expanding {len(seeds)} seed keyword(s)…")
            pass1_raw = await _dfs_keywords_for_keywords(seeds, loc_code, lang_code)

            # Deduplicate — exclude seeds themselves (they'll be stored as pass 0)
            seed_lower: set = {s.lower() for s in seeds}
            seen: set       = set(seed_lower)
            pass1: list     = []
            for item in pass1_raw:
                kl = item["keyword"].lower()
                if kl not in seen:
                    seen.add(kl)
                    pass1.append(item)

            await _upd(
                progress=30,
                msg=f"Pass 1 complete: {len(pass1)} unique keywords. Starting pass 2…"
            )

            # ── Pass 2: expand top-N pass-1 results ───────────────────────────
            p2_seeds = [
                item["keyword"]
                for item in sorted(
                    pass1, key=lambda x: x.get("search_volume") or 0, reverse=True
                )[: session.pass2_limit]
            ]

            pass2_raw = await _dfs_keywords_for_keywords(p2_seeds, loc_code, lang_code)
            pass2: list = []
            for item in pass2_raw:
                kl = item["keyword"].lower()
                if kl not in seen:
                    seen.add(kl)
                    pass2.append(item)

            await _upd(
                progress=55,
                msg=f"Pass 2 complete: {len(pass2)} new keywords. Saving to database…"
            )

            # ── Persist all keywords ───────────────────────────────────────────
            rows: list = []

            # Pass 0: seed keywords (no volume data at this stage)
            for kw in seeds:
                rows.append(KeywordResult(
                    session_id=session_id, keyword=kw,
                    pass_number=0, is_question=False,
                ))

            # Pass 1 results
            for item in pass1:
                rows.append(KeywordResult(
                    session_id=session_id, keyword=item["keyword"],
                    search_volume=item.get("search_volume"),
                    cpc=item.get("cpc"),
                    competition=item.get("competition"),
                    pass_number=1, is_question=False,
                ))

            # Pass 2 results
            for item in pass2:
                rows.append(KeywordResult(
                    session_id=session_id, keyword=item["keyword"],
                    search_volume=item.get("search_volume"),
                    cpc=item.get("cpc"),
                    competition=item.get("competition"),
                    pass_number=2, is_question=False,
                ))

            db.add_all(rows)
            await db.commit()

            total = len(rows)
            await _upd(
                progress=65,
                msg=f"{total} keywords saved. Detecting questions with LLM…"
            )

            # ── LLM question detection ─────────────────────────────────────────
            model_name = LLM_DEFAULT_MODELS.get(session.llm_provider.upper(), "claude-haiku-4-5-20251001")
            all_kw_strings = [r.keyword for r in rows]
            question_set   = await _detect_questions_llm(all_kw_strings, session.llm_provider, model_name)

            await _upd(
                progress=90,
                msg=f"Found {len(question_set)} questions. Updating flags…"
            )

            # Update is_question flags (match case-insensitively)
            q_lower = {q.lower() for q in question_set}
            fetched = (await db.execute(
                select(KeywordResult).where(KeywordResult.session_id == session_id)
            )).scalars().all()
            q_count = 0
            for row in fetched:
                if row.keyword.lower() in q_lower:
                    row.is_question = True
                    q_count += 1

            session.total_keywords  = total
            session.total_questions = q_count
            session.status          = "completed"
            session.progress        = 100
            session.progress_message = "Done"
            session.completed_at    = datetime.utcnow()
            await db.commit()

        except Exception as exc:
            import traceback
            session.status           = "failed"
            session.error            = str(exc)
            session.progress_message = f"Error: {exc}"
            await db.commit()
            traceback.print_exc()


# ── API endpoints ─────────────────────────────────────────────────────────────
@router.post("/sessions", status_code=201)
async def create_session(req: CreateSessionRequest):
    """Create a new keyword research session and start background processing."""
    if not _dfs_configured():
        raise HTTPException(
            status_code=400,
            detail="DataForSEO credentials not configured. "
                   "Add DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD to .env",
        )

    loc = LOCATION_PRESETS.get(req.location.upper())
    if not loc:
        raise HTTPException(status_code=400, detail=f"Unknown location: {req.location}")

    session_id = str(uuid.uuid4())
    async with AsyncSessionLocal() as db:
        session = KeywordSession(
            id             = session_id,
            name           = req.name,
            seed_keywords  = req.seed_keywords,
            location_key   = req.location.upper(),
            location_code  = loc["location_code"],
            language_code  = loc["language_code"],
            language_name  = loc["language_name"],
            pass2_limit    = req.pass2_limit,
            llm_provider   = req.llm_provider,
            status         = "pending",
            progress       = 0,
            progress_message = "Queued…",
        )
        db.add(session)
        await db.commit()

    asyncio.create_task(_run_session(session_id))
    return {"session_id": session_id, "status": "pending"}


@router.get("/sessions")
async def list_sessions():
    """Return all keyword research sessions, newest first."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(KeywordSession).order_by(KeywordSession.created_at.desc())
        )).scalars().all()

    return [
        {
            "id":               s.id,
            "name":             s.name,
            "seed_keywords":    s.seed_keywords,
            "location_key":     s.location_key,
            "language_name":    s.language_name,
            "status":           s.status,
            "progress":         s.progress,
            "progress_message": s.progress_message,
            "total_keywords":   s.total_keywords,
            "total_questions":  s.total_questions,
            "llm_provider":     s.llm_provider,
            "created_at":       s.created_at.isoformat()  if s.created_at  else None,
            "completed_at":     s.completed_at.isoformat() if s.completed_at else None,
            "error":            s.error,
        }
        for s in rows
    ]


@router.get("/sessions/{session_id}/status")
async def session_status(session_id: str):
    """Lightweight poll endpoint for the frontend progress bar."""
    async with AsyncSessionLocal() as db:
        session = await db.get(KeywordSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "status":           session.status,
        "progress":         session.progress,
        "progress_message": session.progress_message,
        "total_keywords":   session.total_keywords,
        "total_questions":  session.total_questions,
        "error":            session.error,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session and all its keyword results."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            sql_delete(KeywordResult).where(KeywordResult.session_id == session_id)
        )
        await db.execute(
            sql_delete(KeywordSession).where(KeywordSession.id == session_id)
        )
        await db.commit()
    return {"success": True}
