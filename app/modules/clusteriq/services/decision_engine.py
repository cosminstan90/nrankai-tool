"""
ClusterIQ — Cluster Decision Engine
=====================================
Extends the KUCD logic from ContentIQ to cluster-level verdicts.

Verdicts (Eslam Saker / KUCD extension)
----------------------------------------
  Fix         — indexable URL with blocked traffic (bad canonical, noindex,
                broken redirect) but existing impressions signal
  Consolidate — 2+ URLs cannibalizing the same SERP (high keyword overlap)
  Optimize    — confirmed demand but weak rank (pos 5-20, low CTR)
  Keep        — primary URL ranks well (top 3-5, CTR > 5%)
  Prune       — zero search demand and all cluster URLs are indexable
  Create      — gap cluster (no good primary URL); NOT generated here,
                comes from competitor gap analysis (Prompt 8)

Priority order: Fix → Consolidate → Optimize → Keep → Prune → (Create)
"""

from __future__ import annotations

import json as _json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, update

from api.models._base import AsyncSessionLocal
from api.models.clusteriq import (
    CluCluster,
    CluDecision,
    CluDuplicate,
    CluProject,
    CluSerpData,
    CluUrl,
    CluUrlCluster,
)

logger = logging.getLogger("clusteriq.decisions")

# ---------------------------------------------------------------------------
# Tuneable thresholds (single place to adjust)
# ---------------------------------------------------------------------------

_FIX_MIN_IMPRESSIONS       = 50    # impressions needed to trigger Fix despite bad status
_CONSOLIDATE_MIN_IMP_URL   = 20    # impressions per URL to count as a cannibalizing URL
_CONSOLIDATE_MIN_URLS      = 2     # min URLs above threshold to call it cannibalization
_OPTIMIZE_MAX_POSITION     = 5.0   # avg_position > this value → consider Optimize
_OPTIMIZE_MIN_IMPRESSIONS  = 100   # impressions floor for Optimize
_OPTIMIZE_MAX_CTR          = 0.05  # CTR ceiling for Optimize (5%)
_KEEP_MAX_POSITION         = 5.0   # avg_position ≤ this → Keep candidate
_KEEP_MIN_CTR              = 0.05  # CTR floor for Keep (5%)

# Scoring weights for winner selection in duplicate cohorts
_SCORE_IMP_WEIGHT  = 2
_SCORE_CLK_WEIGHT  = 5
_SCORE_LINK_WEIGHT = 10


# ---------------------------------------------------------------------------
# Internal data containers
# ---------------------------------------------------------------------------

@dataclass
class _UrlStats:
    url_id:       int
    url:          str
    impressions:  int
    clicks:       int
    avg_position: float | None
    status_code:  int | None
    is_indexable: bool
    backlinks:    int       # from Ahrefs cache; 0 if not available
    is_primary:   bool = False

    @property
    def ctr(self) -> float:
        return self.clicks / self.impressions if self.impressions else 0.0

    @property
    def winner_score(self) -> float:
        return (
            self.impressions * _SCORE_IMP_WEIGHT
            + self.clicks    * _SCORE_CLK_WEIGHT
            + self.backlinks * _SCORE_LINK_WEIGHT
        )

    @property
    def ok_status(self) -> bool:
        return self.status_code == 200 if self.status_code is not None else True


@dataclass
class _ClusterCtx:
    cluster_id:              int
    project_id:              int
    cluster_label:           str
    primary_url_str:         str | None
    primary_keyword:         str | None
    total_impressions:       int
    total_clicks:            int
    url_count:               int
    search_demand_confirmed: bool
    urls:                    list[_UrlStats] = field(default_factory=list)

    @property
    def primary(self) -> _UrlStats | None:
        for u in self.urls:
            if u.is_primary:
                return u
        return self.urls[0] if self.urls else None

    @property
    def cannibalizing_urls(self) -> list[_UrlStats]:
        return [u for u in self.urls if u.impressions >= _CONSOLIDATE_MIN_IMP_URL]


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class ClusterDecisionEngine:
    """
    Generate Fix/Consolidate/Optimize/Keep/Prune verdicts for every cluster
    in a ClusterIQ project and persist them to clusteriq_decisions.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_cluster_verdict(self, cluster_id: int) -> CluDecision:
        """
        Evaluate a single cluster and return an unsaved CluDecision ORM object.
        The caller is responsible for DB persistence.
        """
        ctx = await self._load_cluster_context(cluster_id)
        verdict, confidence = self._apply_verdict_rules(ctx)
        evidence             = self._build_evidence(verdict, ctx)
        target_url           = self._pick_target_url(verdict, ctx)

        return CluDecision(
            cluster_id   = cluster_id,
            url_id       = ctx.primary.url_id if ctx.primary else None,
            verdict      = verdict,
            evidence     = evidence,
            target_url   = target_url,
            confidence   = confidence,
            generated_by = "kucd",
            created_at   = datetime.now(timezone.utc),
        )

    def generate_evidence(self, cluster_id: int, verdict: str) -> list[str]:
        """
        Synchronous variant that returns evidence strings only.
        Prefer generate_cluster_verdict (async) in production code;
        this is exposed for tests or one-off lookups.
        """
        raise NotImplementedError(
            "Use the async generate_cluster_verdict() — it includes evidence internally."
        )

    async def generate_llm_evidence(self, cluster_id: int) -> str:
        """
        For a Consolidate-verdict cluster: call Claude to produce a 150-word
        consolidation brief and append it to the decision's evidence array.

        Returns the generated brief text (empty string on error or non-Consolidate).
        The decision row is updated in-place in the DB.
        """
        try:
            import anthropic
        except ImportError:
            logger.warning("[LLM-evidence] anthropic package not installed")
            return ""

        # Load cluster context and existing decision
        ctx = await self._load_cluster_context(cluster_id)

        async with AsyncSessionLocal() as db:
            decision_row = (await db.execute(
                select(CluDecision)
                .where(CluDecision.cluster_id == cluster_id)
                .order_by(CluDecision.created_at.desc())
            )).scalar_one_or_none()

        if decision_row is None:
            logger.warning("[LLM-evidence] No decision for cluster=%d", cluster_id)
            return ""

        if decision_row.verdict != "Consolidate":
            logger.debug("[LLM-evidence] Skipping non-Consolidate cluster=%d", cluster_id)
            return ""

        # Build URL list for the prompt
        url_lines = []
        for u in ctx.urls[:8]:
            pos_str = f"{u.avg_position:.1f}" if u.avg_position else "N/A"
            url_lines.append(
                f"  - {u.url} | impressions={u.impressions:,} | clicks={u.clicks:,}"
                f" | pos={pos_str} | backlinks={u.backlinks}"
            )
        url_block = "\n".join(url_lines) if url_lines else "  (no URL data available)"

        kw = ctx.primary_keyword or ctx.cluster_label or "unknown keyword"
        prompt_text = (
            "Esti consultant SEO. Analizeaza urmatoarele URL-uri care se canibalizeaza "
            "pe aceleasi SERP-uri pentru keyword-ul principal '" + kw + "':\n\n"
            + url_block + "\n\n"
            "Genereaza un brief de maxim 150 de cuvinte pentru dev team, in romana, care sa specifice: "
            "(1) care URL devine canonical, (2) ce URL-uri se redirecteaza (301) si catre care URL, "
            "(3) ce continut se muta daca e cazul. "
            "Fii concret si direct. Nu folosi headere sau markdown."
        )

        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.warning("[LLM-evidence] ANTHROPIC_API_KEY not set")
            return ""

        try:
            client = anthropic.AsyncAnthropic(api_key=api_key)
            resp = await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=400,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt_text}],
            )
            brief_text: str = resp.content[0].text.strip()

            # Track LLM cost (best-effort)
            try:
                from api.models._base import AsyncSessionLocal as _ASL
                from api.models.infra import CostRecord
                async with _ASL() as _cdb:
                    _cdb.add(CostRecord(
                        source="clusteriq_llm_evidence",
                        source_id=str(cluster_id),
                        provider="anthropic",
                        model="claude-sonnet-4-20250514",
                        input_tokens=resp.usage.input_tokens,
                        output_tokens=resp.usage.output_tokens,
                        estimated_cost_usd=round(
                            resp.usage.input_tokens * 3e-6 + resp.usage.output_tokens * 15e-6, 6
                        ),
                    ))
                    await _cdb.commit()
            except Exception as _ce:
                logger.debug("[LLM-evidence] Cost tracking failed (non-fatal): %s", _ce)

        except Exception as exc:
            logger.error("[LLM-evidence] Claude API error for cluster=%d: %s", cluster_id, exc)
            return ""

        # Append llm_brief to decision evidence and persist
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(CluDecision)
                .where(CluDecision.cluster_id == cluster_id)
                .order_by(CluDecision.created_at.desc())
            )).scalar_one_or_none()

            if row is not None:
                existing_evidence = list(row.evidence or [])
                # Replace any previous llm_brief entry
                existing_evidence = [
                    e for e in existing_evidence
                    if not (isinstance(e, dict) and e.get("type") == "llm_brief")
                ]
                existing_evidence.append({"type": "llm_brief", "text": brief_text})
                row.evidence = existing_evidence
                row.notes    = brief_text
                row.generated_by = "llm"
                await db.commit()

        logger.info(
            "[LLM-evidence] Generated brief for cluster=%d (%d chars)",
            cluster_id, len(brief_text),
        )
        return brief_text

    async def generate_duplicate_cohort(self, project_id: int) -> int:
        """
        For every Consolidate cluster in the project, elect a winner URL and
        record all other cluster members as duplicates in clusteriq_duplicates.

        Returns the number of duplicate pairs persisted.
        """
        consolidate_cluster_ids = await self._load_consolidate_cluster_ids(project_id)
        if not consolidate_cluster_ids:
            return 0

        # Wipe stale duplicate records for the project
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(CluDuplicate).where(CluDuplicate.project_id == project_id)
            )
            await db.commit()

        total_pairs = 0
        for cluster_id in consolidate_cluster_ids:
            pairs = await self._build_duplicate_pairs(project_id, cluster_id)
            total_pairs += pairs

        logger.info(
            "[Duplicates] project=%d — %d duplicate pairs persisted",
            project_id, total_pairs,
        )
        return total_pairs

    async def run_all_decisions(self, project_id: int) -> dict[str, int]:
        """
        Generate and persist verdicts for every cluster in the project.
        Clears previous decisions before inserting (idempotent).

        Returns
        -------
        dict with verdict counts: {fix, consolidate, optimize, keep, prune, total}
        """
        cluster_ids = await self._load_cluster_ids(project_id)
        if not cluster_ids:
            logger.warning("[Decisions] project=%d — no clusters found", project_id)
            return self._empty_summary()

        # Wipe previous decisions
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(CluDecision).where(
                    CluDecision.cluster_id.in_(cluster_ids)
                )
            )
            await db.commit()

        summary: dict[str, int] = {"fix": 0, "consolidate": 0, "optimize": 0,
                                    "keep": 0, "prune": 0, "total": 0}

        # Batch-save in chunks of 50 to avoid huge transactions
        CHUNK = 50
        decisions: list[CluDecision] = []

        for cid in cluster_ids:
            try:
                decision = await self.generate_cluster_verdict(cid)
                decisions.append(decision)
                summary[decision.verdict.lower()] = summary.get(decision.verdict.lower(), 0) + 1
                summary["total"] += 1
            except Exception as exc:
                logger.error("[Decisions] cluster=%d error: %s", cid, exc)

            if len(decisions) >= CHUNK:
                await self._persist_decisions(decisions)
                decisions.clear()

        if decisions:
            await self._persist_decisions(decisions)

        # Generate duplicate cohorts for all Consolidate clusters
        await self.generate_duplicate_cohort(project_id)

        logger.info("[Decisions] project=%d — %s", project_id, summary)
        return summary

    # ------------------------------------------------------------------
    # Verdict logic
    # ------------------------------------------------------------------

    def _apply_verdict_rules(self, ctx: _ClusterCtx) -> tuple[str, float]:
        """
        Apply priority-ordered rules and return (verdict, confidence).
        """
        p = ctx.primary

        # ── Rule 1: Fix ───────────────────────────────────────────────
        if p is not None:
            technical_issue = (not p.ok_status) or (not p.is_indexable)
            if technical_issue and p.impressions > _FIX_MIN_IMPRESSIONS:
                return "Fix", self._confidence_fix(p)

        # ── Rule 2: Consolidate ───────────────────────────────────────
        if len(ctx.cannibalizing_urls) >= _CONSOLIDATE_MIN_URLS:
            return "Consolidate", self._confidence_consolidate(ctx)

        # ── Rule 3: Optimize ──────────────────────────────────────────
        if (
            p is not None
            and p.avg_position is not None
            and p.avg_position > _OPTIMIZE_MAX_POSITION
            and ctx.total_impressions >= _OPTIMIZE_MIN_IMPRESSIONS
            and ctx.total_clicks / max(ctx.total_impressions, 1) < _OPTIMIZE_MAX_CTR
        ):
            return "Optimize", self._confidence_optimize(p)

        # ── Rule 4: Keep ──────────────────────────────────────────────
        if (
            p is not None
            and p.avg_position is not None
            and p.avg_position <= _KEEP_MAX_POSITION
            and p.ctr >= _KEEP_MIN_CTR
        ):
            return "Keep", 0.95

        # ── Rule 5: Prune ─────────────────────────────────────────────
        if (
            not ctx.search_demand_confirmed
            and ctx.total_impressions == 0
            and all(u.is_indexable for u in ctx.urls)
        ):
            return "Prune", 0.70

        # ── Fallback: Optimize (mixed signals) ────────────────────────
        return "Optimize", 0.50

    # ------------------------------------------------------------------
    # Evidence generation
    # ------------------------------------------------------------------

    def _build_evidence(self, verdict: str, ctx: _ClusterCtx) -> list[str]:
        p       = ctx.primary
        reasons: list[str] = []

        if verdict == "Fix":
            if p and not p.ok_status:
                reasons.append(
                    f"Primary URL returnează {p.status_code or 'eroare'} "
                    f"dar are {ctx.total_impressions:,} impressions în ultimele 90 de zile."
                )
            if p and not p.is_indexable:
                reasons.append(
                    f"URL-ul primar este marcat non-indexabil (noindex / canonical greșit) "
                    f"deși acumulează {ctx.total_impressions:,} impressions."
                )

        elif verdict == "Consolidate":
            competing = ctx.cannibalizing_urls
            kw        = ctx.primary_keyword or ctx.cluster_label
            total_imp = sum(u.impressions for u in competing)
            reasons.append(
                f"{len(competing)} URL-uri rankează pe același SERP pentru "
                f"'{kw}', dispersând {total_imp:,} impressions."
            )
            for u in competing[:3]:
                reasons.append(
                    f"  • {u.url} — {u.impressions:,} imp, "
                    f"poziție medie {u.avg_position:.1f}" if u.avg_position else
                    f"  • {u.url} — {u.impressions:,} impressions"
                )

        elif verdict == "Optimize":
            if p:
                pos_str = f"{p.avg_position:.1f}" if p.avg_position else "necunoscută"
                ctr_pct = f"{p.ctr * 100:.1f}%"
                reasons.append(
                    f"Poziția medie {pos_str} cu CTR {ctr_pct} "
                    "sugerează optimizare title/content pentru a urca în top 3."
                )
                reasons.append(
                    f"Clusterul are {ctx.total_impressions:,} impressions — "
                    "cerere confirmată, dar CTR-ul este sub benchmark (5%)."
                )

        elif verdict == "Keep":
            if p:
                pos_str = f"{p.avg_position:.1f}" if p.avg_position else "top"
                reasons.append(
                    f"URL primar rankează la poziția {pos_str} cu CTR "
                    f"{p.ctr * 100:.1f}% — performanță sănătoasă."
                )
            reasons.append(
                f"Cluster cu {ctx.total_impressions:,} impressions și "
                f"{ctx.total_clicks:,} clicks confirmate. Menține strategia curentă."
            )

        elif verdict == "Prune":
            reasons.append(
                f"Clusterul are 0 impressions confirmate și toate "
                f"{ctx.url_count} URL-urile sunt indexabile."
            )
            reasons.append(
                "Conținut fără cerere de căutare demonstrată — candidat pentru "
                "eliminare sau consolidare cu pagini relevante."
            )

        if not reasons:
            reasons.append(
                f"Verdict '{verdict}' aplicat pe cluster '{ctx.cluster_label}' "
                f"({ctx.url_count} URL-uri, {ctx.total_impressions:,} impressions)."
            )

        return reasons

    # ------------------------------------------------------------------
    # Duplicate cohort builder
    # ------------------------------------------------------------------

    async def _build_duplicate_pairs(
        self, project_id: int, cluster_id: int
    ) -> int:
        """
        Build winner/duplicate pairs for a single Consolidate cluster.
        Returns the number of pairs saved.
        """
        ctx = await self._load_cluster_context(cluster_id)
        if len(ctx.urls) < 2:
            return 0

        sorted_urls = sorted(ctx.urls, key=lambda u: u.winner_score, reverse=True)
        winner      = sorted_urls[0]
        losers      = sorted_urls[1:]

        # Shared keywords between winner and each loser
        winner_keywords = await self._keywords_for_url(project_id, winner.url)
        winner_kw_set   = set(winner_keywords)

        pairs_saved = 0
        async with AsyncSessionLocal() as db:
            for loser in losers:
                loser_keywords = await self._keywords_for_url(project_id, loser.url)
                overlap        = sorted(winner_kw_set & set(loser_keywords))[:20]
                similarity     = len(overlap) / max(len(winner_kw_set), 1)

                dev_brief = self._build_dev_brief(winner, loser, overlap)

                db.add(CluDuplicate(
                    project_id       = project_id,
                    winner_url_id    = winner.url_id,
                    duplicate_url_id = loser.url_id,
                    similarity_score = round(similarity, 4),
                    overlap_keywords = overlap,
                    winner_reason    = (
                        f"Scor combinat: {winner.winner_score:.0f} vs "
                        f"{loser.winner_score:.0f}. "
                        f"Winner: {winner.impressions:,} imp, "
                        f"{winner.clicks:,} clicks, "
                        f"{winner.backlinks} backlinks."
                    ),
                    action    = "redirect",
                    dev_brief = dev_brief,
                ))
                pairs_saved += 1

            await db.commit()

        return pairs_saved

    def _build_dev_brief(
        self,
        winner: _UrlStats,
        loser:  _UrlStats,
        overlap_keywords: list[str],
    ) -> str:
        ratio_imp  = (winner.impressions / max(loser.impressions, 1))
        ratio_link = winner.backlinks - loser.backlinks
        kw_sample  = ", ".join(f"'{k}'" for k in overlap_keywords[:5])

        reasons: list[str] = []
        if ratio_imp >= 2:
            reasons.append(f"{ratio_imp:.1f}× mai multe impressions")
        if winner.clicks > loser.clicks:
            diff = winner.clicks - loser.clicks
            reasons.append(f"+{diff:,} clicks mai multe")
        if ratio_link > 0:
            reasons.append(f"{ratio_link} backlinks în plus")

        reason_str = " și ".join(reasons) if reasons else "scor combinat superior"

        brief = (
            f"Redirectează [{loser.url}] → [{winner.url}].\n"
            f"Motivare: {winner.url} are {reason_str} față de alternativă.\n"
        )
        if overlap_keywords:
            brief += f"Keyword-uri comune ({len(overlap_keywords)}): {kw_sample}."

        return brief

    # ------------------------------------------------------------------
    # Confidence helpers
    # ------------------------------------------------------------------

    def _confidence_fix(self, p: _UrlStats) -> float:
        if p.impressions > 500:
            return 0.95
        if p.impressions > 100:
            return 0.90
        return 0.75

    def _confidence_consolidate(self, ctx: _ClusterCtx) -> float:
        n = len(ctx.cannibalizing_urls)
        return min(0.95, 0.70 + (n - 1) * 0.08)

    def _confidence_optimize(self, p: _UrlStats) -> float:
        if p.avg_position is None:
            return 0.55
        gap = max(p.avg_position - _OPTIMIZE_MAX_POSITION, 0)
        return min(0.88, 0.60 + gap * 0.02)

    def _pick_target_url(self, verdict: str, ctx: _ClusterCtx) -> str | None:
        """For Consolidate, return the winner URL as the consolidation target."""
        if verdict != "Consolidate" or not ctx.urls:
            return None
        sorted_urls = sorted(ctx.urls, key=lambda u: u.winner_score, reverse=True)
        return sorted_urls[0].url

    # ------------------------------------------------------------------
    # DB loaders
    # ------------------------------------------------------------------

    async def _load_cluster_context(self, cluster_id: int) -> _ClusterCtx:
        """
        Load all data needed for verdict generation in one async call block.
        """
        async with AsyncSessionLocal() as db:
            cluster = (await db.execute(
                select(CluCluster).where(CluCluster.id == cluster_id)
            )).scalar_one()

            # URL members of this cluster
            member_rows = (await db.execute(
                select(
                    CluUrlCluster.url_id,
                    CluUrlCluster.role,
                    CluUrl.url,
                    CluUrl.status_code,
                    CluUrl.is_indexable,
                )
                .join(CluUrl, CluUrl.id == CluUrlCluster.url_id)
                .where(CluUrlCluster.cluster_id == cluster_id)
            )).all()

            # Aggregate GSC metrics per URL
            url_ids = [r.url_id for r in member_rows]
            url_strings = [r.url for r in member_rows]

            agg_rows = (await db.execute(
                select(
                    CluSerpData.url,
                    func.sum(CluSerpData.impressions).label("total_imp"),
                    func.sum(CluSerpData.clicks).label("total_clk"),
                    func.avg(CluSerpData.position).label("avg_pos"),
                )
                .where(
                    CluSerpData.project_id == cluster.project_id,
                    CluSerpData.url.in_(url_strings),
                )
                .group_by(CluSerpData.url)
            )).all()

            agg_by_url = {
                r.url: {
                    "imp": r.total_imp or 0,
                    "clk": r.total_clk or 0,
                    "pos": round(float(r.avg_pos), 1) if r.avg_pos else None,
                }
                for r in agg_rows
            }

        # Ahrefs cache lookup (best-effort; no error if table/row missing)
        backlinks_by_url = await self._ahrefs_backlinks_for_urls(
            cluster.project_id, url_strings
        )

        # Build _UrlStats list
        url_stats: list[_UrlStats] = []
        for row in member_rows:
            agg  = agg_by_url.get(row.url, {})
            stat = _UrlStats(
                url_id       = row.url_id,
                url          = row.url,
                impressions  = agg.get("imp", 0),
                clicks       = agg.get("clk", 0),
                avg_position = agg.get("pos"),
                status_code  = row.status_code,
                is_indexable = bool(row.is_indexable),
                backlinks    = backlinks_by_url.get(row.url, 0),
                is_primary   = row.url == cluster.primary_url,
            )
            url_stats.append(stat)

        # Sort: primary first, then by impressions DESC
        url_stats.sort(key=lambda u: (not u.is_primary, -u.impressions))

        return _ClusterCtx(
            cluster_id              = cluster_id,
            project_id              = cluster.project_id,
            cluster_label           = cluster.cluster_label,
            primary_url_str         = cluster.primary_url,
            primary_keyword         = cluster.primary_keyword,
            total_impressions       = cluster.total_impressions or 0,
            total_clicks            = cluster.total_clicks or 0,
            url_count               = cluster.url_count or len(url_stats),
            search_demand_confirmed = bool(cluster.search_demand_confirmed),
            urls                    = url_stats,
        )

    async def _ahrefs_backlinks_for_urls(
        self, project_id: int, urls: list[str]
    ) -> dict[str, int]:
        """
        Try to pull backlink counts from ciq_pages (ContentIQ cache).
        Returns {} silently if the table is empty or URLs were never audited.
        """
        if not urls:
            return {}
        try:
            from api.models.contentiq import CiqPage
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(CiqPage.url, CiqPage.ahrefs_backlinks)
                    .where(
                        CiqPage.url.in_(urls),
                        CiqPage.ahrefs_backlinks.isnot(None),
                    )
                    .order_by(CiqPage.ahrefs_backlinks.desc())
                )).all()
            # If a URL was audited multiple times, keep the max backlink count
            result: dict[str, int] = {}
            for row in rows:
                if row.url not in result:
                    result[row.url] = row.ahrefs_backlinks or 0
            return result
        except Exception as exc:
            logger.debug("[Decisions] Ahrefs cache lookup failed (non-fatal): %s", exc)
            return {}

    async def _load_cluster_ids(self, project_id: int) -> list[int]:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(CluCluster.id).where(CluCluster.project_id == project_id)
            )).scalars().all()
        return list(rows)

    async def _load_consolidate_cluster_ids(self, project_id: int) -> list[int]:
        all_ids = await self._load_cluster_ids(project_id)
        if not all_ids:
            return []
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(CluDecision.cluster_id)
                .where(
                    CluDecision.cluster_id.in_(all_ids),
                    CluDecision.verdict == "Consolidate",
                )
                .distinct()
            )).scalars().all()
        return list(rows)

    async def _keywords_for_url(self, project_id: int, url: str) -> list[str]:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(CluSerpData.keyword)
                .where(
                    CluSerpData.project_id == project_id,
                    CluSerpData.url == url,
                )
            )).scalars().all()
        return list(rows)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _persist_decisions(self, decisions: list[CluDecision]) -> None:
        async with AsyncSessionLocal() as db:
            db.add_all(decisions)
            await db.commit()

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_summary() -> dict[str, int]:
        return {"fix": 0, "consolidate": 0, "optimize": 0, "keep": 0, "prune": 0, "total": 0}
