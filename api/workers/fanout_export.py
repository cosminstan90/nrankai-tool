"""
Fan-Out Exporter (Prompt 17)
============================
Generates CSV / JSON exports and plain-text client reports from fan-out data.
Uses only stdlib: csv, json, io.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Optional


_TOOL    = "nrankai-fanout-analyzer"
_VERSION = "1.0"


class FanoutExporter:
    """
    Converts fan-out ORM objects / dicts into export-ready strings.
    All methods are synchronous and dependency-free.
    """

    # ── Session ───────────────────────────────────────────────────────────────

    def session_to_csv(self, session) -> str:
        """
        Return a UTF-8-BOM CSV string with three sections:
            1. Session info  2. Fan-out queries  3. Sources
        """
        buf = io.StringIO()
        buf.write("\ufeff")   # UTF-8 BOM for Excel
        writer = csv.writer(buf)

        # Section 1 — Session metadata
        writer.writerow(["=== SESSION INFO ==="])
        writer.writerow(["ID",           session.id])
        writer.writerow(["Prompt",       session.prompt])
        writer.writerow(["Provider",     session.provider])
        writer.writerow(["Model",        session.model])
        writer.writerow(["Engine",       getattr(session, "engine", session.provider) or session.provider])
        writer.writerow(["Locale",       getattr(session, "locale", "en-US") or "en-US"])
        writer.writerow(["Cluster",      getattr(session, "prompt_cluster", "") or ""])
        writer.writerow(["Cost USD",     f"{getattr(session, 'run_cost_usd', 0) or 0:.6f}"])
        writer.writerow(["Target URL",   session.target_url or ""])
        writer.writerow(["Target Found", "Yes" if session.target_found else "No"])
        writer.writerow(["Created At",   session.created_at.isoformat() if session.created_at else ""])
        writer.writerow([])

        # Section 2 — Fan-out queries
        writer.writerow(["=== FAN-OUT QUERIES ==="])
        writer.writerow(["#", "Query"])
        for i, q in enumerate(session.queries or [], start=1):
            writer.writerow([i, q.query_text])
        writer.writerow([])

        # Section 3 — Sources
        writer.writerow(["=== SOURCES ==="])
        writer.writerow(["#", "URL", "Title", "Domain", "Is Target", "Position"])
        for s in session.sources or []:
            writer.writerow([
                s.source_position or "",
                s.url,
                s.title or "",
                s.domain or "",
                "Yes" if s.is_target else "No",
                s.source_position or "",
            ])

        return buf.getvalue()

    def session_to_json(self, session) -> dict:
        """Return the full session dict with export_meta."""
        data = session.to_dict(include_children=True)
        data["export_meta"] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tool":         _TOOL,
            "version":      _VERSION,
        }
        return data

    # ── Tracking timeline ─────────────────────────────────────────────────────

    def tracking_timeline_to_csv(self, config, runs: list) -> str:
        """CSV with one row per tracking run."""
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf)

        writer.writerow(["=== TRACKING TIMELINE ==="])
        writer.writerow(["Config",  config.name])
        writer.writerow(["Domain",  config.target_domain or ""])
        writer.writerow(["Schedule", config.schedule])
        writer.writerow([])
        writer.writerow(["Date", "Mention Rate %", "Composite Score", "Avg Position", "Sources", "Cost USD", "Trend", "Model Version"])
        for r in runs:
            writer.writerow([
                r.run_date,
                f"{(r.mention_rate or 0) * 100:.1f}",
                f"{r.composite_score or 0:.2f}",
                f"{r.avg_source_position or 0:.1f}",
                r.total_unique_sources or 0,
                f"{r.cost_usd or 0:.6f}",
                "",
                r.model_version or "",
            ])
        return buf.getvalue()

    # ── Competitive report ────────────────────────────────────────────────────

    def competitive_report_to_csv(self, report: dict) -> str:
        """CSV with ranking + head-to-head matrix."""
        if not report:
            return "\ufeffNo report data\n"

        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf)

        # Ranking
        writer.writerow(["=== OVERALL RANKING ==="])
        writer.writerow(["Rank", "Domain", "Mention Rate %", "Avg Position", "Score"])
        for row in report.get("overall_ranking", []):
            domain = row.get("domain", "")
            comp   = (report.get("competitors") or {}).get(domain, {})
            writer.writerow([
                row.get("rank", ""),
                domain,
                f"{(comp.get('mention_rate') or 0) * 100:.1f}",
                f"{comp.get('avg_position') or 0:.1f}",
                f"{row.get('score') or 0:.1f}",
            ])
        writer.writerow([])

        # Head-to-head matrix
        writer.writerow(["=== HEAD-TO-HEAD MATRIX ==="])
        domains = list((report.get("competitors") or {}).keys())
        writer.writerow(["Prompt", "Cluster"] + domains + ["Winner"])
        for row in report.get("head_to_head", []):
            results = row.get("results", {})
            cells   = []
            for d in domains:
                r = results.get(d, {})
                cells.append(f"✓ pos{r['position']}" if r.get("found") else "—")
            writer.writerow([row.get("prompt", ""), row.get("cluster", "")] + cells + [row.get("winner", "")])

        return buf.getvalue()

    # ── Discovery ─────────────────────────────────────────────────────────────

    def discovery_result_to_csv(self, result: dict) -> str:
        """CSV with prompt + mention status + top competitor."""
        buf = io.StringIO()
        buf.write("\ufeff")
        writer = csv.writer(buf)
        writer.writerow(["Prompt", "Mentioned", "Engines", "Position", "Top Competitor"])
        for r in result.get("results", []):
            writer.writerow([
                r.get("prompt", ""),
                "Yes" if r.get("mentioned") else "No",
                ", ".join(r.get("engines", [])),
                r.get("position") or "",
                (r.get("competitors_found") or [""])[0],
            ])
        return buf.getvalue()

    # ── Client report ─────────────────────────────────────────────────────────

    def generate_client_report_text(
        self,
        brand: str,
        domain: str,
        discovery: Optional[dict] = None,
        timeline: Optional[dict]  = None,
        competitive: Optional[dict] = None,
    ) -> str:
        """
        Plain-text structured report ready to paste into an email.
        """
        lines: list[str] = []
        now   = datetime.now(timezone.utc).strftime("%B %d, %Y")

        lines += [
            f"AI VISIBILITY REPORT — {brand.upper()}",
            f"Generated: {now}  |  Domain: {domain}  |  Tool: {_TOOL}",
            "=" * 60,
            "",
        ]

        # Executive Summary
        lines.append("EXECUTIVE SUMMARY")
        lines.append("-" * 30)
        mention_rate = None
        if timeline:
            runs = timeline.get("runs", [])
            if runs:
                mention_rate = runs[-1].get("mention_rate")
        if discovery:
            mention_rate = mention_rate or discovery.get("mention_rate")

        if mention_rate is not None:
            pct = round(mention_rate * 100, 1)
            lines.append(f"  • Current AI mention rate: {pct}%")
            if pct >= 60:
                lines.append("  • Status: STRONG — your brand appears in most AI-generated answers.")
            elif pct >= 30:
                lines.append("  • Status: MODERATE — your brand appears in some AI-generated answers.")
            else:
                lines.append("  • Status: WEAK — your brand rarely appears in AI-generated answers.")
        else:
            lines.append("  • Mention rate: no data yet.")
        lines.append("")

        # Top performing prompts
        if discovery:
            lines.append("TOP PERFORMING PROMPTS (Brand mentioned)")
            lines.append("-" * 30)
            for r in (discovery.get("mentioned_in") or [])[:5]:
                lines.append(f"  ✓ {r.get('prompt', '')}")
            lines.append("")

            # Gaps
            lines.append("VISIBILITY GAPS (Brand NOT mentioned)")
            lines.append("-" * 30)
            for r in (discovery.get("not_mentioned_in") or [])[:5]:
                lines.append(f"  ✗ {r.get('prompt', '')}")
                rivals = (r.get("top_competitors") or [])[:2]
                if rivals:
                    lines.append(f"    → Competitors: {', '.join(rivals)}")
            lines.append("")

        # Timeline trend
        if timeline:
            runs = timeline.get("runs", [])
            if len(runs) >= 2:
                first = runs[0].get("mention_rate", 0) or 0
                last  = runs[-1].get("mention_rate", 0) or 0
                delta = round((last - first) * 100, 1)
                sign  = "+" if delta >= 0 else ""
                lines.append("TREND")
                lines.append("-" * 30)
                lines.append(f"  • Change over {len(runs)} runs: {sign}{delta}pp")
                lines.append("")

        # Competitive summary
        if competitive:
            lines.append("COMPETITIVE POSITIONING")
            lines.append("-" * 30)
            for row in (competitive.get("overall_ranking") or [])[:5]:
                domain_r = row.get("domain", "")
                score    = row.get("score", 0)
                marker   = " ← YOUR DOMAIN" if domain_r == domain else ""
                lines.append(f"  #{row.get('rank')} {domain_r}  (score {score:.1f}){marker}")
            lines.append("")

            lines.append("RECOMMENDATIONS")
            lines.append("-" * 30)
            for rec in (competitive.get("recommendations") or [])[:5]:
                lines.append(f"  • [{rec.get('type','').upper()}] {rec.get('title','')}")
                lines.append(f"    {rec.get('rationale','')}")
            lines.append("")

        lines.append("=" * 60)
        lines.append(f"Report generated by {_TOOL} v{_VERSION}")
        lines.append("https://app.nrankai.com")

        return "\n".join(lines)
