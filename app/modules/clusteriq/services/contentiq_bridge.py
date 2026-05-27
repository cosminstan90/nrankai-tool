"""
ClusterIQ → ContentIQ Bridge
==============================
Creates ContentBrief records from ClusterIQ cluster verdicts.

A ContentBrief created here has audit_id=None / result_id=None (standalone mode)
and appears in the standard /briefs list.  After creation, CluDecision.brief_id
is set so the overview counter can track which clusters have a brief.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.clusteriq import CluCluster, CluDecision, CluUrl, CluUrlCluster
from api.models.content import ContentBrief

logger = logging.getLogger("clusteriq.bridge")


async def create_contentiq_brief_from_cluster(cluster_id: int) -> int:
    """
    Build a ContentBrief from a ClusterIQ cluster decision and persist it.

    Supported verdicts: Optimize, Create, Consolidate.
    Returns the new ContentBrief.id.
    Raises ValueError if the cluster has no decision or an unsupported verdict.
    """
    async with AsyncSessionLocal() as db:
        # ── Load cluster ────────────────────────────────────────────────
        cluster = (await db.execute(
            select(CluCluster).where(CluCluster.id == cluster_id)
        )).scalar_one_or_none()
        if cluster is None:
            raise ValueError(f"Cluster {cluster_id} not found")

        # ── Load latest decision ─────────────────────────────────────────
        decision = (await db.execute(
            select(CluDecision)
            .where(CluDecision.cluster_id == cluster_id)
            .order_by(CluDecision.created_at.desc())
        )).scalar_one_or_none()
        if decision is None:
            raise ValueError(f"No decision for cluster {cluster_id}")

        verdict = decision.verdict
        if verdict not in ("Optimize", "Create", "Consolidate"):
            raise ValueError(
                f"generate-brief only supports Optimize/Create/Consolidate verdicts, got '{verdict}'"
            )

        # ── Load member URLs ──────────────────────────────────────────────
        member_rows = (await db.execute(
            select(CluUrlCluster.url_id, CluUrlCluster.role, CluUrl.url, CluUrl.title)
            .join(CluUrl, CluUrl.id == CluUrlCluster.url_id)
            .where(CluUrlCluster.cluster_id == cluster_id)
        )).all()
        member_urls = [r.url for r in member_rows]
        primary_url = cluster.primary_url or (member_urls[0] if member_urls else None)
        keyword     = cluster.primary_keyword or cluster.cluster_label or "unknown topic"

        # ── Build brief_json ──────────────────────────────────────────────
        intent_map = {
            "Optimize":    "improve ranking for existing content",
            "Create":      "create new content to capture missing demand",
            "Consolidate": "consolidate duplicate content into one canonical page",
        }
        evidence_texts: list[str] = []
        for e in (decision.evidence or []):
            if isinstance(e, dict):
                txt = e.get("text") or str(e)
            else:
                txt = str(e)
            evidence_texts.append(txt)

        brief_data = {
            "source":          "clusteriq",
            "cluster_id":      cluster_id,
            "decision_id":     decision.id,
            "verdict":         verdict,
            "target_keyword":  keyword,
            "primary_url":     primary_url,
            "member_urls":     member_urls[:20],
            "intent":          intent_map.get(verdict, verdict),
            "total_impressions": cluster.total_impressions or 0,
            "total_clicks":    cluster.total_clicks or 0,
            "confidence":      decision.confidence,
            "evidence":        evidence_texts[:6],
            "llm_brief":       decision.notes,   # pre-generated LLM brief if any
            "created_at":      datetime.now(timezone.utc).isoformat(),
        }

        executive_summary = _build_executive_summary(
            keyword, verdict, primary_url, cluster.total_impressions, decision.notes
        )

        # ── Persist ContentBrief ──────────────────────────────────────────
        brief = ContentBrief(
            audit_id          = None,
            result_id         = None,
            page_url          = primary_url or keyword,
            brief_json        = json.dumps(brief_data, ensure_ascii=False),
            status            = "pending",
            priority          = _pick_priority(verdict, cluster.total_impressions),
            provider          = "anthropic",
            model             = "clusteriq",
            executive_summary = executive_summary,
            created_at        = datetime.now(timezone.utc),
        )
        db.add(brief)
        await db.flush()   # populate brief.id

        # ── Back-link decision → brief ────────────────────────────────────
        decision.brief_id = brief.id
        await db.commit()

        logger.info(
            "[Bridge] Created ContentBrief #%d for cluster=%d (verdict=%s, keyword='%s')",
            brief.id, cluster_id, verdict, keyword,
        )
        return brief.id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_executive_summary(
    keyword: str,
    verdict: str,
    primary_url: str | None,
    impressions: int | None,
    llm_brief: str | None,
) -> str:
    if llm_brief:
        return llm_brief[:500]

    imp_str = f"{impressions:,} impressions" if impressions else "impressions unknown"
    if verdict == "Optimize":
        return (
            f"ClusterIQ: keyword '{keyword}' are {imp_str} dar CTR scazut. "
            f"Pagina principala {primary_url or '(necunoscuta)'} necesita optimizare "
            f"title, meta, content pentru a urca in top 3."
        )
    if verdict == "Create":
        return (
            f"ClusterIQ: keyword gap detectat pentru '{keyword}'. "
            f"Nu exista o pagina dedicata care sa serveasca aceasta cerere ({imp_str}). "
            f"Creaza o pagina noua tintind keyword-ul principal."
        )
    if verdict == "Consolidate":
        return (
            f"ClusterIQ: mai multe URL-uri se canibalizeaza pentru '{keyword}' ({imp_str}). "
            f"Consolideaza in {primary_url or '(URL principal)'} si redirecteaza restul (301)."
        )
    return f"ClusterIQ brief for cluster keyword '{keyword}' (verdict: {verdict})."


def _pick_priority(verdict: str, impressions: int | None) -> str:
    imp = impressions or 0
    if verdict in ("Fix", "Consolidate") or imp > 5000:
        return "high"
    if imp > 500:
        return "medium"
    return "low"
