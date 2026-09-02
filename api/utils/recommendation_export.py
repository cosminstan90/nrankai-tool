"""
Shared CSV/HTML/Trello export engine for page-level recommendation lists.

Etapa 5.1 of the consolidation (docs/CONSOLIDATION_PLAN.md). action_cards.py
and content_briefs.py both produce, per page, a list of "change items" with
the same conceptual shape (a title, current state, recommended replacement,
a reason, and an effort/impact tag) even though their field names differ
(category/action/reason/difficulty vs. type/section/rationale/impact) and
their generation prompts are entirely separate -- one hardcoded in Python,
one loaded from the frozen prompts/content_brief.yaml. Rather than force
those two genuinely different generation pipelines into one, this module
unifies only the presentation layer they already duplicated: CSV, HTML, and
Trello export formatting. JSON export stays model-specific (each model's own
to_dict() is already exactly right for that) and isn't included here.
"""

from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import List, Optional

# Vocabulary-agnostic tag coloring: action_cards uses difficulty
# (easy/medium/hard), content_briefs uses impact (critical/high/medium) --
# same visual severity spectrum, different words. Matches the original
# action_cards HTML export's easy=green/medium=orange/hard=red coding.
_TAG_COLORS = {
    "easy":     ("#D1FAE5", "#065F46"),
    "low":      ("#D1FAE5", "#065F46"),
    "medium":   ("#FED7AA", "#9A3412"),
    "hard":     ("#FEE2E2", "#991B1B"),
    "high":     ("#FEE2E2", "#991B1B"),
    "critical": ("#FEE2E2", "#991B1B"),
}
_DEFAULT_TAG_COLOR = ("#E0E7FF", "#3730A3")


def _tag_color(tag: Optional[str]) -> tuple:
    return _TAG_COLORS.get((tag or "").lower(), _DEFAULT_TAG_COLOR)

from fastapi import Response


@dataclass
class ExportItem:
    """One recommendation item within a page (an action_cards 'action', or a
    content_briefs 'content_changes' entry)."""
    title: str
    category: Optional[str] = None     # action_cards.category / content_briefs.type
    tag: Optional[str] = None          # action_cards.difficulty / content_briefs.impact
    current: Optional[str] = None
    recommended: Optional[str] = None
    reason: Optional[str] = None
    completed: Optional[bool] = None   # None when the source has no completion concept


@dataclass
class ExportPage:
    """One page's worth of recommendations, plus the score/priority context."""
    page_url: str
    page_title: Optional[str] = None
    priority: Optional[str] = None
    current_score: Optional[float] = None
    target_score: Optional[float] = None
    progress_label: Optional[str] = None   # e.g. "3/5" completed actions -- only action_cards has this
    items: List[ExportItem] = field(default_factory=list)


def build_csv_response(pages: List[ExportPage], website: str, filename_stem: str) -> Response:
    """Build a CSV export Response, one row per (page, item)."""
    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Page URL", "Priority", "Current Score", "Target Score",
        "Item #", "Category", "Title", "Current Text",
        "Recommended Text", "Reason", "Tag", "Status",
    ])

    for page in pages:
        for i, item in enumerate(page.items, 1):
            writer.writerow([
                page.page_url,
                page.priority,
                page.current_score,
                page.target_score,
                i,
                item.category,
                item.title,
                (item.current or "")[:200],
                (item.recommended or "")[:500],
                item.reason or "",
                item.tag,
                ("✓" if item.completed else "☐") if item.completed is not None else "",
            ])

    csv_content = output.getvalue()
    filename = f"{filename_stem}_{website}_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def build_trello_export(pages: List[ExportPage], website: str, board_name_prefix: str) -> dict:
    """Build a Trello-importable board dict, one card per page grouped by priority."""
    lists = {
        "critical": {"name": "\U0001F534 Critical Priority", "cards": []},
        "high":     {"name": "\U0001F7E0 High Priority", "cards": []},
        "medium":   {"name": "\U0001F7E1 Medium Priority", "cards": []},
        "low":      {"name": "\U0001F7E2 Low Priority", "cards": []},
    }
    _TRELLO_MAX_DESC = 15_000

    for page in pages:
        checklist_items = [
            {
                "name": f"{item.title} ({item.tag})" if item.tag else item.title,
                "checked": bool(item.completed),
            }
            for item in page.items
        ]

        desc_lines = [f"**Page:** {page.page_url}"]
        if page.current_score is not None:
            desc_lines.append(f"**Current Score:** {page.current_score}/100")
        if page.target_score is not None:
            desc_lines.append(f"**Target Score:** {page.target_score}/100")
        desc_lines += ["", "## Items:", ""]

        for item in page.items:
            desc_lines.append(f"### {item.title}")
            if item.category:
                desc_lines.append(f"**Category:** {item.category}")
            if item.tag:
                desc_lines.append(f"**Tag:** {item.tag}")
            if item.current:
                desc_lines.append(f"\n**Current:** {item.current}")
            if item.recommended:
                desc_lines.append(f"\n**Recommended:**\n{item.recommended}")
            if item.reason:
                desc_lines.append(f"\n**Why:** {item.reason}")
            desc_lines.append("\n---\n")

        description = "\n".join(desc_lines)
        if len(description) > _TRELLO_MAX_DESC:
            description = description[:_TRELLO_MAX_DESC] + "\n\n*[truncated — see full export for remaining items]*"

        title = page.page_title or page.page_url
        if page.current_score is not None and page.target_score is not None:
            title = f"{title} ({page.current_score}→{page.target_score})"

        trello_card = {
            "name": title,
            "desc": description,
            "checklists": [{"name": "Implementation Checklist", "items": checklist_items}],
        }

        priority_key = (page.priority or "medium").lower()
        lists.get(priority_key, lists["medium"])["cards"].append(trello_card)

    return {
        "name": f"{board_name_prefix}: {website}",
        "lists": [v for v in lists.values() if v["cards"]],
    }


def build_html_response(
    pages: List[ExportPage],
    website: str,
    title_prefix: str,
    filename_stem: str,
    agency_name: str = "Your Agency",
) -> Response:
    """Build a standalone HTML report Response."""
    html = f"""<!DOCTYPE html>
<html lang="ro">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{escape(title_prefix)} - {escape(website)}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            background: #f5f5f5;
            padding: 20px;
        }}

        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .header {{
            border-bottom: 3px solid #4F46E5;
            padding-bottom: 20px;
            margin-bottom: 40px;
        }}

        .header h1 {{
            color: #1F2937;
            font-size: 32px;
            margin-bottom: 10px;
        }}

        .header .meta {{
            color: #6B7280;
            font-size: 14px;
        }}

        .card {{
            border: 2px solid #E5E7EB;
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 24px;
            break-inside: avoid;
        }}

        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 1px solid #E5E7EB;
        }}

        .card-url {{
            font-size: 14px;
            font-weight: 600;
            color: #4F46E5;
            word-break: break-all;
        }}

        .priority-badge {{
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
        }}

        .priority-critical {{ background: #FEE2E2; color: #991B1B; }}
        .priority-high {{ background: #FED7AA; color: #9A3412; }}
        .priority-medium {{ background: #FEF3C7; color: #92400E; }}
        .priority-low {{ background: #D1FAE5; color: #065F46; }}

        .score-section {{
            display: flex;
            gap: 24px;
            margin-bottom: 20px;
            padding: 12px;
            background: #F9FAFB;
            border-radius: 4px;
        }}

        .score-item {{
            flex: 1;
        }}

        .score-label {{
            font-size: 12px;
            color: #6B7280;
            text-transform: uppercase;
            margin-bottom: 4px;
        }}

        .score-value {{
            font-size: 24px;
            font-weight: 700;
            color: #1F2937;
        }}

        .action {{
            margin-bottom: 20px;
            padding: 16px;
            background: #F9FAFB;
            border-left: 4px solid #4F46E5;
            border-radius: 4px;
        }}

        .action-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }}

        .action-title {{
            font-size: 16px;
            font-weight: 600;
            color: #1F2937;
        }}

        .action-tag {{
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }}

        .action-content {{
            margin-top: 12px;
        }}

        .action-section {{
            margin-bottom: 12px;
        }}

        .action-label {{
            font-size: 11px;
            color: #6B7280;
            text-transform: uppercase;
            font-weight: 600;
            margin-bottom: 4px;
        }}

        .action-text {{
            font-size: 14px;
            color: #374151;
            padding: 8px;
            background: white;
            border-radius: 4px;
            white-space: pre-wrap;
            word-break: break-word;
        }}

        .action-reason {{
            font-size: 13px;
            color: #6B7280;
            font-style: italic;
            margin-top: 8px;
        }}

        .footer {{
            margin-top: 40px;
            padding-top: 20px;
            border-top: 1px solid #E5E7EB;
            text-align: center;
            color: #6B7280;
            font-size: 13px;
        }}

        @media print {{
            body {{ background: white; padding: 0; }}
            .container {{ box-shadow: none; padding: 20px; }}
            .card {{ page-break-inside: avoid; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{escape(title_prefix)} pentru {escape(website)}</h1>
            <div class="meta">
                Generated by {escape(agency_name)} on {datetime.now().strftime('%d %B %Y')}
            </div>
        </div>
"""

    for page in pages:
        score_bits = []
        if page.current_score is not None:
            score_bits.append(f"""
                <div class="score-item">
                    <div class="score-label">Current Score</div>
                    <div class="score-value">{page.current_score}/100</div>
                </div>""")
        if page.target_score is not None:
            score_bits.append(f"""
                <div class="score-item">
                    <div class="score-label">Target Score</div>
                    <div class="score-value">{page.target_score}/100</div>
                </div>""")
        if page.progress_label is not None:
            score_bits.append(f"""
                <div class="score-item">
                    <div class="score-label">Actions</div>
                    <div class="score-value">{escape(page.progress_label)}</div>
                </div>""")

        html += f"""
        <div class="card">
            <div class="card-header">
                <div class="card-url">{escape(page.page_url or "")}</div>
                <span class="priority-badge priority-{escape(page.priority or "")}">{escape(page.priority or "")}</span>
            </div>

            <div class="score-section">{''.join(score_bits)}
            </div>
"""

        for item in page.items:
            html += f"""
            <div class="action">
                <div class="action-header">
                    <div class="action-title">{"✓ " if item.completed else ("☐ " if item.completed is not None else "")}{escape(item.title or "")}</div>
                    <span class="action-tag" style="background: {_tag_color(item.tag)[0]}; color: {_tag_color(item.tag)[1]};">{escape(item.tag or "")}</span>
                </div>

                <div class="action-content">
"""
            if item.current:
                html += f"""
                    <div class="action-section">
                        <div class="action-label">Current:</div>
                        <div class="action-text">{escape(item.current)}</div>
                    </div>
"""
            if item.recommended:
                html += f"""
                    <div class="action-section">
                        <div class="action-label">Recommended:</div>
                        <div class="action-text">{escape(item.recommended)}</div>
                    </div>
"""
            if item.reason:
                html += f"""
                    <div class="action-reason">
                        \U0001F4A1 {escape(item.reason)}
                    </div>
"""
            html += """
                </div>
            </div>
"""

        html += """
        </div>
"""

    html += f"""
        <div class="footer">
            Generated by {escape(agency_name)} • {datetime.now().strftime('%d %B %Y')}
        </div>
    </div>
</body>
</html>
"""

    filename = f"{filename_stem}_{website}_{datetime.now().strftime('%Y%m%d')}.html"
    return Response(
        content=html,
        media_type="text/html",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
