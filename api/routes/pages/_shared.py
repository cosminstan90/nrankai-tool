"""Shared state for HTML page routes — templates instance, constants, helpers."""

from pathlib import Path
from typing import Optional

from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import AuditWeightConfig

# Templates directory: pages/_shared.py → pages/ → routes/ → api/ → project root/templates
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent.parent / "templates"))

# ============================================================================
# COMPOSITE SCORE WEIGHTS — used by per-URL and per-site views
# ============================================================================
_COMPOSITE_WEIGHTS = {
    'SEO_AUDIT': 0.20,
    'GEO_AUDIT': 0.15,
    'CONTENT_QUALITY': 0.12,
    'TECHNICAL_SEO': 0.12,
    'UX_CONTENT': 0.10,
    'ACCESSIBILITY_AUDIT': 0.08,
    'BRAND_VOICE': 0.07,
    'LEGAL_GDPR': 0.06,
    'INTERNAL_LINKING': 0.05,
    'READABILITY_AUDIT': 0.05,
    'COMPETITOR_ANALYSIS': 0.04,
    'CONTENT_FRESHNESS': 0.04,
    'AI_OVERVIEW_OPTIMIZATION': 0.04,
    'SPELLING_GRAMMAR': 0.03,
    'TRANSLATION_QUALITY': 0.03,
    'LOCAL_SEO': 0.03,
    'SECURITY_CONTENT_AUDIT': 0.03,
    'E_COMMERCE': 0.03,
}

_AUDIT_TYPE_LABELS = {
    'SEO_AUDIT': 'SEO', 'GEO_AUDIT': 'GEO', 'CONTENT_QUALITY': 'Content Quality',
    'TECHNICAL_SEO': 'Technical SEO', 'UX_CONTENT': 'UX Content',
    'ACCESSIBILITY_AUDIT': 'Accessibility', 'BRAND_VOICE': 'Brand Voice',
    'LEGAL_GDPR': 'Legal / GDPR', 'INTERNAL_LINKING': 'Internal Linking',
    'READABILITY_AUDIT': 'Readability', 'COMPETITOR_ANALYSIS': 'Competitors',
    'CONTENT_FRESHNESS': 'Content Freshness', 'AI_OVERVIEW_OPTIMIZATION': 'AI Overview',
    'SPELLING_GRAMMAR': 'Spelling & Grammar', 'TRANSLATION_QUALITY': 'Translation',
    'LOCAL_SEO': 'Local SEO', 'SECURITY_CONTENT_AUDIT': 'Security Content',
    'E_COMMERCE': 'E-Commerce',
}


async def _load_weights(db: AsyncSession) -> dict:
    """Return effective weight dict — DB rows override hardcoded defaults.

    Falls back to _COMPOSITE_WEIGHTS when the audit_weight_configs table
    is empty (fresh install or after a reset).
    """
    result = await db.execute(select(AuditWeightConfig))
    rows = result.scalars().all()
    if not rows:
        return _COMPOSITE_WEIGHTS
    # Merge: start from defaults, overlay DB values
    merged = dict(_COMPOSITE_WEIGHTS)
    for row in rows:
        merged[row.audit_type] = row.weight
    return merged


def _compute_composite(scored_map: dict, weights: Optional[dict] = None) -> Optional[int]:
    """Compute weighted composite score from {audit_type: score} dict.

    Args:
        scored_map: Mapping of audit_type → score (None scores are skipped).
        weights: Override weight dict; defaults to _COMPOSITE_WEIGHTS.
    """
    w_map = weights if weights is not None else _COMPOSITE_WEIGHTS
    weighted_sum = 0.0
    weight_sum = 0.0
    for atype, score in scored_map.items():
        if score is not None:
            w = w_map.get(atype.upper(), 0.02)
            weighted_sum += score * w
            weight_sum += w
    if weight_sum == 0:
        return None
    return round(weighted_sum / weight_sum)


def _repair_guide_json(raw_json_str: str):
    """Parse guide_json and repair any {"raw": "..."} audit entries using json_repair."""
    import json
    try:
        gj = json.loads(raw_json_str)
        if isinstance(gj, dict) and "results" in gj:
            from json_repair import repair_json
            for key, val in gj["results"].items():
                if isinstance(val, dict) and "raw" in val and isinstance(val["raw"], str):
                    try:
                        repaired = repair_json(val["raw"], return_objects=True)
                        if isinstance(repaired, dict) and repaired:
                            gj["results"][key] = repaired
                    except Exception:
                        pass
        return gj
    except Exception:
        return None
