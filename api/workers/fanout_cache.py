"""
Fanout Cache (Prompt 16)
========================
Avoids re-calling AI APIs for identical (prompt, engine, model, locale) combinations
within a configurable TTL window.

TTL buckets:
    adhoc   = 4 h
    daily   = 20 h
    weekly  = 160 h
    monthly = 700 h

cache_key = SHA-256(f"{prompt.lower().strip()}|{engine}|{model}|{locale}")
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import FanoutCacheEntry
from api.workers.fanout_analyzer import FanoutResult, FanoutSource

logger = logging.getLogger("fanout_cache")

# TTL in hours per scheduling bucket
_TTL_HOURS = {
    "adhoc":   4,
    "daily":   20,
    "weekly":  160,
    "monthly": 700,
}
_DEFAULT_TTL = "adhoc"


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _result_to_dict(result: FanoutResult) -> dict:
    return {
        "prompt":               result.prompt,
        "provider":             result.provider,
        "model":                result.model,
        "fanout_queries":       result.fanout_queries,
        "sources":              [{"url": s.url, "title": s.title, "domain": s.domain, "snippet": s.snippet}
                                 for s in result.sources],
        "search_call_count":    result.search_call_count,
        "total_fanout_queries": result.total_fanout_queries,
        "total_sources":        result.total_sources,
    }


def _dict_to_result(data: dict) -> FanoutResult:
    sources = [
        FanoutSource(url=s["url"], title=s.get("title", ""), domain=s.get("domain", ""), snippet=s.get("snippet", ""))
        for s in data.get("sources", [])
    ]
    result = FanoutResult(
        prompt=data["prompt"],
        provider=data["provider"],
        model=data["model"],
        fanout_queries=data.get("fanout_queries", []),
        sources=sources,
        search_call_count=data.get("search_call_count", 0),
        total_fanout_queries=data.get("total_fanout_queries", 0),
        total_sources=data.get("total_sources", 0),
    )
    result.from_cache = True   # type: ignore[attr-defined]
    return result


# ── Public API ────────────────────────────────────────────────────────────────

class FanoutCache:
    """
    Async cache for FanoutResult objects backed by the fanout_cache SQLite table.
    """

    @staticmethod
    def make_key(prompt: str, engine: str, model: str, locale: str = "en-US") -> str:
        raw = f"{prompt.lower().strip()}|{engine}|{model}|{locale}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def prompt_hash(prompt: str) -> str:
        return hashlib.sha256(prompt.lower().strip().encode()).hexdigest()

    @staticmethod
    def _expires_at(ttl_mode: str) -> datetime:
        hours = _TTL_HOURS.get(ttl_mode, _TTL_HOURS[_DEFAULT_TTL])
        return datetime.now(timezone.utc) + timedelta(hours=hours)

    @classmethod
    async def get(
        cls,
        db: AsyncSession,
        prompt: str,
        engine: str,
        model: str,
        locale: str = "en-US",
    ) -> Optional[FanoutResult]:
        """Return cached FanoutResult or None if miss / expired."""
        key = cls.make_key(prompt, engine, model, locale)
        now = datetime.now(timezone.utc)
        row = (
            await db.execute(
                select(FanoutCacheEntry).where(
                    FanoutCacheEntry.cache_key == key,
                    FanoutCacheEntry.expires_at > now,
                )
            )
        ).scalar_one_or_none()

        if row is None:
            return None

        # Increment hit counter (fire-and-forget)
        try:
            row.hit_count = (row.hit_count or 0) + 1
            await db.commit()
        except Exception:
            await db.rollback()

        try:
            data = json.loads(row.result_json)
            return _dict_to_result(data)
        except Exception as exc:
            logger.warning("Cache deserialisation error for key %s: %s", key, exc)
            return None

    @classmethod
    async def set(
        cls,
        db: AsyncSession,
        result: FanoutResult,
        engine: str,
        model: str,
        locale: str = "en-US",
        ttl_mode: str = _DEFAULT_TTL,
    ) -> None:
        """Upsert a FanoutResult into the cache."""
        key        = cls.make_key(result.prompt, engine, model, locale)
        ph         = cls.prompt_hash(result.prompt)
        expires    = cls._expires_at(ttl_mode)
        result_str = json.dumps(_result_to_dict(result))

        existing = (
            await db.execute(select(FanoutCacheEntry).where(FanoutCacheEntry.cache_key == key))
        ).scalar_one_or_none()

        if existing:
            existing.result_json = result_str
            existing.expires_at  = expires
            existing.hit_count   = (existing.hit_count or 0) + 1
        else:
            db.add(FanoutCacheEntry(
                cache_key   = key,
                prompt_hash = ph,
                engine      = engine,
                model       = model,
                locale      = locale,
                result_json = result_str,
                expires_at  = expires,
            ))

        try:
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning("Cache set error: %s", exc)

    @classmethod
    async def get_stats(cls, db: AsyncSession) -> dict:
        """Return cache statistics."""
        now = datetime.now(timezone.utc)
        total  = (await db.execute(select(func.count(FanoutCacheEntry.id)))).scalar_one()
        active = (await db.execute(
            select(func.count(FanoutCacheEntry.id)).where(FanoutCacheEntry.expires_at > now)
        )).scalar_one()
        total_hits = (await db.execute(select(func.sum(FanoutCacheEntry.hit_count)))).scalar_one() or 0
        return {
            "total_entries":   total,
            "active_entries":  active,
            "expired_entries": total - active,
            "total_hits":      int(total_hits),
        }

    @classmethod
    async def cleanup_expired(cls, db: AsyncSession) -> int:
        """Delete expired cache entries. Returns count deleted."""
        now = datetime.now(timezone.utc)
        result = await db.execute(
            delete(FanoutCacheEntry).where(FanoutCacheEntry.expires_at <= now)
        )
        await db.commit()
        deleted = result.rowcount
        logger.info("Cache cleanup: removed %d expired entries", deleted)
        return deleted

    @classmethod
    async def clear_by_engine(cls, db: AsyncSession, engine: Optional[str] = None) -> int:
        """Clear all entries for a specific engine (or all if engine=None)."""
        stmt = delete(FanoutCacheEntry)
        if engine:
            stmt = stmt.where(FanoutCacheEntry.engine == engine)
        result = await db.execute(stmt)
        await db.commit()
        return result.rowcount


# ── Cached wrapper ────────────────────────────────────────────────────────────

async def analyze_prompt_cached(
    prompt: str,
    provider: str = "openai",
    model: Optional[str] = None,
    locale: str = "en-US",
    ttl_mode: str = _DEFAULT_TTL,
    db: Optional[AsyncSession] = None,
) -> FanoutResult:
    """
    Drop-in replacement for analyze_prompt() that checks the cache first.

    If *db* is None the cache is skipped and a live call is made.
    """
    from api.workers.fanout_analyzer import analyze_prompt, PROVIDER_DEFAULTS

    resolved_model = model or PROVIDER_DEFAULTS.get(provider, "")

    if db is not None:
        cached = await FanoutCache.get(db, prompt, provider, resolved_model, locale)
        if cached is not None:
            logger.info("Cache HIT for prompt=%.40r engine=%s model=%s", prompt, provider, resolved_model)
            return cached

    result = await analyze_prompt(prompt, provider=provider, model=resolved_model)

    if db is not None:
        try:
            await FanoutCache.set(db, result, provider, resolved_model, locale, ttl_mode)
        except Exception as exc:
            logger.warning("Failed to store result in cache: %s", exc)

    return result
