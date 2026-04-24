"""
Score Weight Settings API Routes

Allows runtime configuration of the composite score weights that are
normally hardcoded in main.py (_COMPOSITE_WEIGHTS).  Values are stored
in the audit_weight_configs table.  When the table is empty the
application falls back to the hardcoded defaults automatically.

Endpoints
---------
GET  /api/settings/weights        → current effective weights (DB overlay on defaults)
PUT  /api/settings/weights        → upsert new weights (validates sum ≈ 1.0)
POST /api/settings/weights/reset  → delete all rows → revert to hardcoded defaults
"""

from datetime import datetime, timezone
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, AuditWeightConfig

router = APIRouter(prefix="/api/settings", tags=["settings"])

# ── Hardcoded defaults (must stay in sync with main.py _COMPOSITE_WEIGHTS) ──
_DEFAULTS: Dict[str, float] = {
    "SEO_AUDIT": 0.20,
    "GEO_AUDIT": 0.15,
    "CONTENT_QUALITY": 0.12,
    "TECHNICAL_SEO": 0.12,
    "UX_CONTENT": 0.10,
    "ACCESSIBILITY_AUDIT": 0.08,
    "BRAND_VOICE": 0.07,
    "LEGAL_GDPR": 0.06,
    "INTERNAL_LINKING": 0.05,
    "READABILITY_AUDIT": 0.05,
    "COMPETITOR_ANALYSIS": 0.04,
    "CONTENT_FRESHNESS": 0.04,
    "AI_OVERVIEW_OPTIMIZATION": 0.04,
    "SPELLING_GRAMMAR": 0.03,
    "TRANSLATION_QUALITY": 0.03,
    "LOCAL_SEO": 0.03,
    "SECURITY_CONTENT_AUDIT": 0.03,
    "E_COMMERCE": 0.03,
}

_LABELS: Dict[str, str] = {
    "SEO_AUDIT": "SEO",
    "GEO_AUDIT": "GEO",
    "CONTENT_QUALITY": "Content Quality",
    "TECHNICAL_SEO": "Technical SEO",
    "UX_CONTENT": "UX Content",
    "ACCESSIBILITY_AUDIT": "Accessibility",
    "BRAND_VOICE": "Brand Voice",
    "LEGAL_GDPR": "Legal / GDPR",
    "INTERNAL_LINKING": "Internal Linking",
    "READABILITY_AUDIT": "Readability",
    "COMPETITOR_ANALYSIS": "Competitors",
    "CONTENT_FRESHNESS": "Content Freshness",
    "AI_OVERVIEW_OPTIMIZATION": "AI Overview",
    "SPELLING_GRAMMAR": "Spelling & Grammar",
    "TRANSLATION_QUALITY": "Translation",
    "LOCAL_SEO": "Local SEO",
    "SECURITY_CONTENT_AUDIT": "Security Content",
    "E_COMMERCE": "E-Commerce",
}


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class WeightsPayload(BaseModel):
    weights: Dict[str, float]

    @field_validator("weights")
    @classmethod
    def validate_weights(cls, v: Dict[str, float]) -> Dict[str, float]:
        if not v:
            raise ValueError("weights dict must not be empty")
        for key, val in v.items():
            if key not in _DEFAULTS:
                raise ValueError(f"Unknown audit_type: {key}")
            if val < 0:
                raise ValueError(f"Weight for {key} must be non-negative")
        total = sum(v.values())
        if not (0.95 <= total <= 1.05):
            raise ValueError(
                f"Weights must sum to approximately 1.0 (got {total:.4f}). "
                "Adjust values so the total is between 0.95 and 1.05."
            )
        return v


# ── Helper ────────────────────────────────────────────────────────────────────

async def _fetch_db_weights(db: AsyncSession) -> Dict[str, float]:
    """Return {audit_type: weight} from DB, or {} if table is empty."""
    result = await db.execute(select(AuditWeightConfig))
    rows = result.scalars().all()
    return {row.audit_type: row.weight for row in rows}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/weights")
async def get_weights(db: AsyncSession = Depends(get_db)):
    """
    Return the current effective weights.

    If the DB table has rows they override the hardcoded defaults.
    The response includes the label, default weight, current weight,
    and whether the current value is custom (differs from default).
    """
    db_weights = await _fetch_db_weights(db)
    using_defaults = not bool(db_weights)

    weights_out = []
    for atype, default_w in _DEFAULTS.items():
        current_w = db_weights.get(atype, default_w)
        weights_out.append({
            "audit_type": atype,
            "label": _LABELS.get(atype, atype),
            "default_weight": default_w,
            "current_weight": current_w,
            "current_pct": round(current_w * 100, 1),
            "is_custom": atype in db_weights and abs(db_weights[atype] - default_w) > 1e-6,
        })

    return {
        "weights": weights_out,
        "using_defaults": using_defaults,
        "total": round(sum(db_weights.get(a, d) for a, d in _DEFAULTS.items()), 4),
    }


@router.put("/weights")
async def update_weights(payload: WeightsPayload, db: AsyncSession = Depends(get_db)):
    """
    Upsert weight values.

    Only the audit_types included in the payload are stored; types omitted
    will fall back to their hardcoded default.  Payload must cover ALL
    18 audit types so the sum-validation is meaningful.
    """
    # Delete existing rows and re-insert (simplest upsert for SQLite)
    await db.execute(delete(AuditWeightConfig))

    for atype, weight in payload.weights.items():
        row = AuditWeightConfig(
            audit_type=atype,
            weight=weight,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(row)

    await db.commit()
    return {"success": True, "message": "Weights saved successfully."}


@router.post("/weights/reset")
async def reset_weights(db: AsyncSession = Depends(get_db)):
    """Delete all custom weight rows so the app reverts to hardcoded defaults."""
    await db.execute(delete(AuditWeightConfig))
    await db.commit()
    return {"success": True, "message": "Weights reset to defaults."}
