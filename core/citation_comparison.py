"""
Citation comparison report -- Pasul 16 of docs/superpowers/plans/2026-09-30-next-steps.md.

"Optimize for AI" is generic advice. This turns several actually-cited
pages' features (core/citation_features.py) plus the tracked site's own
page into a report: which features a clear MAJORITY of cited pages share
that the tracked page lacks. Only reports a difference the data actually
shows -- never a feature this function didn't measure for enough pages, and
never every difference (a feature only one cited page happens to have is
noise, not a pattern).

No LLM call in the comparison itself. build_recommendation_prompt() below
optionally turns a report into a phrased recommendation, but takes ONLY the
measured feature table -- never the pages themselves -- so the model
formulates from facts instead of "judging" raw content per the plan's
explicit instruction.
"""
from collections import Counter
from statistics import median
from typing import List, Optional

SMALL_SAMPLE_THRESHOLD = 3
MAJORITY_FRACTION = 0.5   # "most cited pages have X" -- strictly more than half
RECENT_DAYS_THRESHOLD = 365
DENSITY_GAP_FRACTION = 0.5   # own page's density below half the cited median counts as a gap

# (feature_key, human_label) -- boolean features reported as presence/absence.
_BOOLEAN_FEATURES = [
    ("has_tables", "a comparison table or data table"),
    ("has_lists", "a list (steps, options, criteria)"),
    ("has_qa_content", "visible Q&A / FAQ-style content"),
    ("has_author", "a visible author byline"),
    ("direct_answer_in_first_words", "a direct answer in the opening ~100 words"),
]


def compare_to_citations(own_features: dict, cited_features: List[dict]) -> dict:
    """
    own_features: the tracker's own best-ranked page for this query, from
    extract_features(). cited_features: one dict per page actually cited
    for this query (AI Overview references + LLM citation sources), also
    from extract_features().
    """
    m = len(cited_features)
    if m == 0:
        return {"sample_size": 0, "small_sample": True, "findings": [],
               "note": "no citing pages could be measured"}

    findings = []

    for key, label in _BOOLEAN_FEATURES:
        cited_with = sum(1 for f in cited_features if f.get(key))
        if cited_with / m > MAJORITY_FRACTION and not own_features.get(key):
            findings.append({
                "feature": key, "label": label,
                "cited_pages_with_it": cited_with, "cited_pages_total": m,
            })

    cited_densities = [f["numeric_density_per_100_words"] for f in cited_features
                      if f.get("numeric_density_per_100_words") is not None]
    if cited_densities:
        cited_median = median(cited_densities)
        own_density = own_features.get("numeric_density_per_100_words") or 0.0
        if cited_median > 0 and own_density < cited_median * DENSITY_GAP_FRACTION:
            findings.append({
                "feature": "numeric_density_per_100_words",
                "label": "a higher density of concrete figures (prices, percentages, sums)",
                "cited_pages_median": round(cited_median, 2), "own_page_value": round(own_density, 2),
            })

    cited_recency = [f["days_since_updated"] for f in cited_features if f.get("days_since_updated") is not None]
    if cited_recency:
        recent_count = sum(1 for d in cited_recency if d <= RECENT_DAYS_THRESHOLD)
        if recent_count / len(cited_recency) > MAJORITY_FRACTION:
            own_days = own_features.get("days_since_updated")
            if own_days is None or own_days > RECENT_DAYS_THRESHOLD:
                findings.append({
                    "feature": "days_since_updated",
                    "label": "a visible, recent publish/update date (most cited pages updated within the last year)",
                    "cited_pages_recent": recent_count, "cited_pages_measured": len(cited_recency),
                    "own_page_value": own_days,
                })

    type_counts = Counter()
    for f in cited_features:
        for t in (f.get("json_ld_types") or []):
            type_counts[t] += 1
    own_types = set(own_features.get("json_ld_types") or [])
    for schema_type, count in type_counts.items():
        if count / m > MAJORITY_FRACTION and schema_type not in own_types:
            findings.append({
                "feature": "json_ld_types", "label": f'"{schema_type}" structured data',
                "cited_pages_with_it": count, "cited_pages_total": m,
            })

    return {"sample_size": m, "small_sample": m < SMALL_SAMPLE_THRESHOLD, "findings": findings}


def build_recommendation_prompt(query: str, report: dict) -> Optional[str]:
    """
    A user-content string for an LLM call, containing ONLY the measured
    feature table -- never the cited pages' actual content -- so the model
    formulates a recommendation from facts rather than judging raw pages.
    None when there's nothing to recommend (no findings).
    """
    if not report.get("findings"):
        return None

    lines = [
        f'Query: "{query}"',
        f"Measured against {report['sample_size']} page(s) actually cited for this query"
        + (" (small sample -- say so)." if report.get("small_sample") else "."),
        "",
        "Features most cited pages have that the tracked page lacks:",
    ]
    for finding in report["findings"]:
        detail = ", ".join(f"{k}={v}" for k, v in finding.items() if k not in ("feature", "label"))
        lines.append(f"- {finding['label']} ({detail})")
    lines.append("")
    lines.append(
        "Write 2-3 sentences recommending what to add to the tracked page, "
        "based only on the measured differences above. Do not invent facts "
        "about the cited pages beyond what's listed."
    )
    return "\n".join(lines)
