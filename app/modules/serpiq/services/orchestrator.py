"""
SerpIQ — Orchestrator
=====================
Coordinates the full analysis pipeline for a single SerpIQ snapshot:

  1. Derive keyword (from URL path or use input directly)
  2. Fire SERP fetch + URL analysis in parallel (URL input only)
  3. Call verdict engine → (verdict, evidence, difficulty)
  4. Optionally call Claude for a 150-word content brief
  5. Persist SiqSnapshot + SiqSerpItem rows to DB
  6. Return snapshot dict

Public interface
----------------
  SerpIQOrchestrator
    run_snapshot(input_type, input_value, location_code, language_code,
                 generate_brief, user_id) -> dict
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("serpiq.orchestrator")

# ---------------------------------------------------------------------------
# Prompt template for Claude brief
# ---------------------------------------------------------------------------

_BRIEF_PROMPT = """Ești un expert SEO senior. Analizezi keyword-ul: "{keyword}"

Context SERP:
- Poziția URL-ului introdus: {position}
- Verdict analiză: {verdict}
- Dificultate estimată: {difficulty}
- Top competitor: {top_competitor}
- Medie cuvinte top-3 rezultate: {avg_words} cuvinte

Motive verdict:
{evidence_bullets}

Scrie un brief de conținut de 150 de cuvinte în română pentru a crea sau îmbunătăți
conținutul care targetează acest keyword. Include:
- Unghiul principal de abordare
- 3-4 subteme esențiale de acoperit
- Sfaturi specifice pentru a depăși competitorii
- Tipul de conținut recomandat (articol, pagina produs, landing page etc.)

Fii concis, practic și specific. Fără anteturi/markdown."""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class SerpIQOrchestrator:
    """
    Stateless orchestrator — safe to instantiate once and reuse.

    All heavy work is async; the orchestrator creates its own DB session
    so it can be called from background tasks as well as route handlers.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_snapshot(
        self,
        input_type:    str,            # 'url' | 'keyword'
        input_value:   str,
        location_code: int  = 2040,
        language_code: str  = "ro",
        generate_brief: bool = False,
        user_id:        Optional[int] = None,
    ) -> dict:
        """
        Run a full SerpIQ analysis and return the saved snapshot as a dict.

        Never raises — on critical errors returns a minimal dict with
        ``error`` key populated and an empty result set.
        """
        t_start = time.monotonic()

        try:
            return await self._pipeline(
                input_type=input_type,
                input_value=input_value,
                location_code=location_code,
                language_code=language_code,
                generate_brief=generate_brief,
                user_id=user_id,
                t_start=t_start,
            )
        except Exception as exc:
            logger.exception("[SerpIQOrchestrator] unhandled error in run_snapshot: %s", exc)
            return {
                "error": str(exc),
                "input_type": input_type,
                "input_value": input_value,
            }

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    async def _pipeline(
        self,
        input_type:    str,
        input_value:   str,
        location_code: int,
        language_code: str,
        generate_brief: bool,
        user_id:       Optional[int],
        t_start:       float,
    ) -> dict:
        from app.modules.serpiq.services.serp_fetcher import SERPFetcher, extract_keyword_from_url
        from app.modules.serpiq.services.url_analyzer import URLAnalyzer
        from app.modules.serpiq.services.verdict_engine import SerpIQVerdictEngine

        # --- Derive keyword -----------------------------------------------
        if input_type == "url":
            keyword = extract_keyword_from_url(input_value)
        else:
            keyword = input_value.strip()

        logger.info(
            "[SerpIQOrchestrator] input_type=%s keyword=%r location=%d",
            input_type, keyword, location_code,
        )

        # --- Build service instances --------------------------------------
        try:
            fetcher = SERPFetcher()
        except ValueError as exc:
            logger.error("[SerpIQOrchestrator] cannot init SERPFetcher: %s", exc)
            raise

        analyzer = URLAnalyzer()
        engine   = SerpIQVerdictEngine()

        # --- Parallel fetch: SERP + (optionally) URL analysis -------------
        if input_type == "url":
            serp_task = asyncio.create_task(
                fetcher.fetch_serp(keyword, location_code=location_code, language_code=language_code)
            )
            url_task = asyncio.create_task(analyzer.analyze_url(input_value))
            serp_items, url_analysis = await asyncio.gather(serp_task, url_task)
        else:
            serp_items   = await fetcher.fetch_serp(keyword, location_code=location_code, language_code=language_code)
            url_analysis = None

        # --- Find position ------------------------------------------------
        position_found: Optional[int] = None
        if input_type == "url" and serp_items:
            position_found = analyzer.find_url_in_serp(input_value, serp_items)

        # --- Verdict engine -----------------------------------------------
        verdict, evidence = engine.generate_verdict(
            input_type=input_type,
            position_found=position_found,
            serp_items=serp_items,
            url_analysis=url_analysis,
        )
        difficulty = engine.analyze_serp_difficulty(serp_items)

        # --- Optional Claude brief ----------------------------------------
        llm_brief: Optional[str] = None
        if generate_brief:
            llm_brief = await self._generate_brief(
                keyword=keyword,
                verdict=verdict,
                evidence=evidence,
                position_found=position_found,
                difficulty=difficulty,
                serp_items=serp_items,
            )

        # --- Persist -------------------------------------------------------
        processing_ms = int((time.monotonic() - t_start) * 1000)
        snapshot = await self._save_snapshot(
            input_type=input_type,
            input_value=input_value,
            keyword=keyword,
            location_code=location_code,
            language_code=language_code,
            serp_items=serp_items,
            url_analysis=url_analysis,
            verdict=verdict,
            evidence=evidence,
            llm_brief=llm_brief,
            position_found=position_found,
            processing_ms=processing_ms,
            user_id=user_id,
        )

        return snapshot

    # ------------------------------------------------------------------
    # Claude brief generator
    # ------------------------------------------------------------------

    async def _generate_brief(
        self,
        keyword:       str,
        verdict:       str,
        evidence:      list[str],
        position_found: Optional[int],
        difficulty,    # SerpDifficulty dataclass
        serp_items:    list,
    ) -> Optional[str]:
        """Call Claude to generate a 150-word content brief. Never raises."""
        try:
            import anthropic

            # Build context values
            pos_label   = str(position_found) if position_found else "negăsit"
            diff_label  = getattr(difficulty, "difficulty_signal", "necunoscut")
            top_url     = None
            avg_words   = getattr(difficulty, "avg_word_count_top3", None)

            # Extract top competitor URL
            for item in serp_items[:3]:
                url = item.url if hasattr(item, "url") else (item.get("url") if isinstance(item, dict) else None)
                if url:
                    top_url = url
                    break

            evidence_text = "\n".join(f"- {e}" for e in evidence) or "- Nu există date suplimentare"

            prompt = _BRIEF_PROMPT.format(
                keyword=keyword,
                position=pos_label,
                verdict=verdict,
                difficulty=diff_label,
                top_competitor=top_url or "necunoscut",
                avg_words=avg_words or "necunoscut",
                evidence_bullets=evidence_text,
            )

            client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                temperature=0.4,
                messages=[{"role": "user", "content": prompt}],
            )

            brief = (response.content[0].text or "").strip()
            logger.info("[SerpIQOrchestrator] Claude brief generated (%d chars)", len(brief))
            return brief or None

        except Exception as exc:
            logger.warning("[SerpIQOrchestrator] brief generation failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # DB persistence
    # ------------------------------------------------------------------

    async def _save_snapshot(
        self,
        input_type:    str,
        input_value:   str,
        keyword:       str,
        location_code: int,
        language_code: str,
        serp_items:    list,
        url_analysis,
        verdict:       str,
        evidence:      list[str],
        llm_brief:     Optional[str],
        position_found: Optional[int],
        processing_ms: int,
        user_id:       Optional[int],
    ) -> dict:
        """Persist snapshot + serp items and return snapshot.to_dict(include_items=True)."""
        from api.models._base import AsyncSessionLocal
        from app.modules.serpiq.models import SiqSnapshot, SiqSerpItem

        # Serialise url_analysis to dict for JSON storage
        url_analysis_dict = None
        if url_analysis is not None:
            try:
                url_analysis_dict = url_analysis.to_dict() if hasattr(url_analysis, "to_dict") else dict(url_analysis)
            except Exception:
                pass

        # Serialise serp_items to list[dict] for JSON storage
        serp_raw = [
            item.to_dict() if hasattr(item, "to_dict") else dict(item)
            for item in serp_items
        ]

        async with AsyncSessionLocal() as db:
            snapshot = SiqSnapshot(
                user_id=user_id,
                input_type=input_type,
                input_value=input_value,
                keyword=keyword,
                location_code=location_code,
                language_code=language_code,
                serp_results=serp_raw,
                url_analysis=url_analysis_dict,
                verdict=verdict,
                verdict_evidence=evidence,
                llm_brief=llm_brief,
                position_found=position_found,
                processing_time_ms=processing_ms,
            )
            db.add(snapshot)
            await db.flush()  # get snapshot.id without committing

            # Persist individual SERP item rows
            for item in serp_items:
                if hasattr(item, "position"):
                    # SERPItem dataclass
                    row = SiqSerpItem(
                        snapshot_id=snapshot.id,
                        position=item.position,
                        url=item.url,
                        domain=item.domain,
                        title=item.title,
                        description=item.description,
                        word_count_estimated=item.word_count_estimated,
                        has_schema=item.has_schema,
                        schema_types=item.schema_types or [],
                        is_featured_snippet=item.is_featured_snippet,
                        is_local_pack=item.is_local_pack,
                        is_video=item.is_video,
                    )
                else:
                    # dict fallback
                    row = SiqSerpItem(
                        snapshot_id=snapshot.id,
                        position=item.get("position", 0),
                        url=item.get("url"),
                        domain=item.get("domain"),
                        title=item.get("title"),
                        description=item.get("description"),
                        word_count_estimated=item.get("word_count_estimated"),
                        has_schema=item.get("has_schema", False),
                        schema_types=item.get("schema_types") or [],
                        is_featured_snippet=item.get("is_featured_snippet", False),
                        is_local_pack=item.get("is_local_pack", False),
                        is_video=item.get("is_video", False),
                    )
                db.add(row)

            await db.commit()
            await db.refresh(snapshot)

            # Eager-load items (already flushed; re-query to fill relationship)
            from sqlalchemy import select
            items_result = await db.execute(
                select(SiqSerpItem)
                .where(SiqSerpItem.snapshot_id == snapshot.id)
                .order_by(SiqSerpItem.position)
            )
            loaded_items = items_result.scalars().all()

            # Build result dict manually — do NOT access snapshot.serp_items
            # (the relationship), as accessing it post-commit triggers lazy
            # loading outside a greenlet context.
            result = snapshot.to_dict(include_items=False)
            result["serp_items"] = [item.to_dict() for item in loaded_items]

        logger.info(
            "[SerpIQOrchestrator] snapshot #%d saved (verdict=%s, pos=%s, items=%d, %d ms)",
            result["id"], verdict, position_found, len(serp_raw), processing_ms,
        )
        return result
