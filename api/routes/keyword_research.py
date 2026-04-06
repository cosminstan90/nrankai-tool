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

from api.utils.task_runner import create_tracked_task

import httpx
from fastapi import APIRouter, HTTPException
from api.utils.errors import raise_not_found, raise_bad_request
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
        raise_bad_request(f"Unknown location: {req.location}")

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

    create_tracked_task(_run_session(session_id), name=f"kw-research-session-{session_id}", timeout=600)
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
        raise_not_found("Session")
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


# ── CSV / Paste Import ────────────────────────────────────────────────────────

class ImportSessionRequest(BaseModel):
    name:         str
    raw_text:     str            # pasted CSV / TSV content
    llm_provider: str = "anthropic"


_ANALYSIS_SYSTEM = """\
You are a keyword strategist. Analyse the following keyword list and for each keyword determine:

1. INTENT — exactly one of:
   - informational : user wants to learn or research something
   - commercial    : user is comparing options or researching before buying
   - transactional : user wants to take action now (buy, sign up, book, download)
   - navigational  : user wants to reach a specific website or brand

2. CLUSTER — a short 2–4 word topic label. Be CONSISTENT: use the same label for closely related keywords.
   Examples: "pricing plans", "how to guides", "competitor comparison", "product features", "local service"

3. IS_QUESTION — true if the keyword is a question or implies seeking an answer, false otherwise

4. PRIORITY — integer 1–10:
   - 9–10 : transactional or commercial intent + any volume
   - 6–8  : commercial intent OR high-volume informational
   - 3–5  : informational at low/medium volume
   - 1–2  : navigational or brand-only terms

Respond ONLY with valid JSON, no preamble, using EXACTLY this structure:
{
  "keywords": [
    {"keyword": "exact text from input", "intent": "informational", "cluster": "cluster label", "is_question": false, "priority": 6}
  ]
}\
"""


def _parse_imported_text(raw: str) -> tuple:
    """
    Parse pasted CSV/TSV into (headers, list-of-dicts).
    Handles: tab-separated, comma-separated, plain one-per-line.
    """
    import csv as _csv, io as _io

    lines = [l for l in raw.strip().splitlines() if l.strip()]
    if not lines:
        return [], []

    # Detect separator
    first = lines[0]
    sep = "\t" if first.count("\t") >= first.count(",") else ","

    # If no separator at all → treat as plain keyword list
    if sep == "," and "," not in first and "\t" not in first:
        return ["keyword"], [{"keyword": l.strip()} for l in lines if l.strip()]

    reader = _csv.reader(_io.StringIO(raw.strip()), delimiter=sep)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], []

    # Detect header row
    header_words = {"keyword", "keywords", "query", "search term", "term", "volume",
                    "search volume", "cpc", "difficulty", "kd", "competition", "phrase"}
    first_lower = " ".join(c.lower().strip() for c in rows[0])
    is_header = any(w in first_lower for w in header_words)

    if is_header:
        headers = [c.strip() for c in rows[0]]
        data = rows[1:]
    else:
        headers = [f"col_{i}" for i in range(len(rows[0]))]
        data = rows

    return headers, [dict(zip(headers, r)) for r in data if any(c.strip() for c in r)]


def _map_columns(headers: list) -> dict:
    """Auto-map header names → keyword/volume/cpc/difficulty."""
    hl = {h.lower().strip(): h for h in headers}
    mapping = {"keyword": None, "volume": None, "cpc": None, "difficulty": None}

    for name in ["keyword", "keywords", "query", "search term", "term", "phrase", "search query"]:
        if name in hl:
            mapping["keyword"] = hl[name]; break
    if not mapping["keyword"] and headers:
        mapping["keyword"] = headers[0]  # fallback: first column

    for name in ["volume", "search volume", "avg. monthly searches", "monthly searches",
                 "monthly volume", "searches", "avg monthly searches"]:
        if name in hl:
            mapping["volume"] = hl[name]; break

    for name in ["cpc", "cost per click", "cpc (usd)", "average cpc",
                 "top of page bid (low range)", "suggested bid"]:
        if name in hl:
            mapping["cpc"] = hl[name]; break

    for name in ["difficulty", "keyword difficulty", "kd", "kd (%)", "seo difficulty",
                 "competition (indexed value)", "competition"]:
        if name in hl:
            mapping["difficulty"] = hl[name]; break

    return mapping


def _clean_number(val: str) -> Optional[float]:
    """Parse numeric strings including ranges ('1,000 - 10,000') and K/M suffixes."""
    if not val or not str(val).strip():
        return None
    s = str(val).strip()

    # Handle ranges → midpoint
    if " - " in s:
        parts = s.split(" - ")
        nums = [_clean_number(p) for p in parts]
        nums = [n for n in nums if n is not None]
        return sum(nums) / len(nums) if nums else None

    # Remove currency symbols, spaces; keep digits, dot, comma, K, M
    s = re.sub(r"[^\d.,KkMm]", "", s)
    # Handle K / M suffixes
    if s.upper().endswith("K"):
        try: return float(s[:-1].replace(",", "")) * 1_000
        except: return None
    if s.upper().endswith("M"):
        try: return float(s[:-1].replace(",", "")) * 1_000_000
        except: return None
    # Remove thousands commas: only remove if number looks like 1,234
    s = re.sub(r",(?=\d{3}(?:[,.]|$))", "", s)
    try:
        return float(s)
    except:
        return None


async def _analyze_keywords_llm(
    keywords_data: List[dict],
    provider: str,
    model: str,
) -> List[dict]:
    """
    Run combined LLM analysis: intent + cluster + is_question + priority.
    Returns list of {keyword, intent, cluster, is_question, priority}.
    Batches at 100 keywords per call.
    """
    from api.routes.schema_gen import call_llm_for_schema

    results: dict = {}   # keyword.lower() → analysis dict
    BATCH = 100

    kw_list = [d["keyword"] for d in keywords_data]
    for i in range(0, len(kw_list), BATCH):
        batch = kw_list[i: i + BATCH]
        lines = "\n".join(f"- {kw}" for kw in batch)
        try:
            text, _, _ = await call_llm_for_schema(
                provider=provider,
                model=model,
                system_prompt=_ANALYSIS_SYSTEM,
                user_content=f"Analyse these keywords:\n\n{lines}",
                max_tokens=8192,
                prefill="{",
            )
            parsed = _extract_json_safe(text)
            for item in parsed.get("keywords", []):
                kw = str(item.get("keyword", "")).strip()
                if kw:
                    results[kw.lower()] = item
        except Exception as exc:
            print(f"[keyword_research] LLM analysis batch {i // BATCH + 1} error: {exc}")

    # Merge analysis back into keywords_data
    merged = []
    for d in keywords_data:
        analysis = results.get(d["keyword"].lower(), {})
        merged.append({**d, **{
            "intent":         analysis.get("intent"),
            "cluster":        analysis.get("cluster"),
            "is_question":    bool(analysis.get("is_question", False)),
            "priority_score": float(analysis.get("priority", 5)),
        }})
    return merged


async def _run_import_session(session_id: str, keywords_data: List[dict], provider: str) -> None:
    """Background task: run LLM analysis on imported keywords and save results."""
    async with AsyncSessionLocal() as db:
        session = await db.get(KeywordSession, session_id)
        if not session:
            return

        async def _upd(progress: int, msg: str):
            session.progress = progress
            session.progress_message = msg
            await db.commit()

        try:
            session.status = "running"
            await _upd(5, f"Analysing {len(keywords_data)} keywords with LLM…")

            model_name = LLM_DEFAULT_MODELS.get(provider.upper(), "claude-haiku-4-5-20251001")
            analysed = await _analyze_keywords_llm(keywords_data, provider, model_name)

            await _upd(80, "Saving results to database…")

            rows = []
            for item in analysed:
                rows.append(KeywordResult(
                    session_id=session_id,
                    keyword=item["keyword"],
                    search_volume=int(item["search_volume"]) if item.get("search_volume") is not None else None,
                    cpc=item.get("cpc"),
                    competition=item.get("competition"),
                    pass_number=0,
                    is_question=item.get("is_question", False),
                    intent=item.get("intent"),
                    cluster=item.get("cluster"),
                    priority_score=item.get("priority_score"),
                ))
            db.add_all(rows)

            q_count = sum(1 for r in rows if r.is_question)
            session.total_keywords  = len(rows)
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


@router.post("/import", status_code=201)
async def import_session(req: ImportSessionRequest):
    """Create a keyword session from pasted CSV/TSV data and run LLM analysis."""
    if not req.raw_text.strip():
        raise_bad_request("Paste some keyword data first.")

    headers, rows = _parse_imported_text(req.raw_text)
    if not rows:
        raise_bad_request("Could not parse any rows from the pasted data.")

    mapping = _map_columns(headers)
    kw_col   = mapping["keyword"]
    vol_col  = mapping["volume"]
    cpc_col  = mapping["cpc"]
    diff_col = mapping["difficulty"]

    keywords_data = []
    seen: set = set()
    for row in rows:
        kw = row.get(kw_col, "").strip() if kw_col else ""
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        vol  = _clean_number(row.get(vol_col,  "")) if vol_col  else None
        cpc  = _clean_number(row.get(cpc_col,  "")) if cpc_col  else None
        diff = _clean_number(row.get(diff_col, "")) if diff_col else None
        # Normalise difficulty: if > 1 assume 0–100 scale → 0–1
        if diff is not None and diff > 1:
            diff = diff / 100
        keywords_data.append({
            "keyword":       kw,
            "search_volume": int(vol) if vol is not None else None,
            "cpc":           cpc,
            "competition":   diff,
        })

    if not keywords_data:
        raise_bad_request("No valid keywords found after parsing.")

    session_id = str(uuid.uuid4())
    seeds = [d["keyword"] for d in keywords_data[:5]]

    async with AsyncSessionLocal() as db:
        session = KeywordSession(
            id               = session_id,
            name             = req.name.strip() or f"Import — {len(keywords_data)} keywords",
            seed_keywords    = seeds,
            location_key     = "—",
            location_code    = 0,
            language_code    = "",
            language_name    = "Import",
            pass2_limit      = 0,
            llm_provider     = req.llm_provider,
            source           = "import",
            status           = "pending",
            progress         = 0,
            progress_message = "Queued for LLM analysis…",
        )
        db.add(session)
        await db.commit()

    create_tracked_task(_run_import_session(session_id, keywords_data, req.llm_provider), name=f"kw-import-session-{session_id}", timeout=600)
    return {"session_id": session_id, "keywords_found": len(keywords_data), "status": "pending"}
