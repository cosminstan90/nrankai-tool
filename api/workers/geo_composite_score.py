"""
GEO Composite Score calculator (Prompt 22).
Pure-Python, no AI calls.

Formula (all components 0-100):
  score = mention_rate_pct * 0.30
        + position_score   * 0.25
        + engine_coverage  * 0.20
        + cluster_diversity * 0.15
        + trend_score      * 0.10
"""

from dataclasses import dataclass
from typing import Optional


def _clamp(value: float) -> float:
    """Clamp a float to [0, 100]."""
    return max(0.0, min(100.0, value))


def grade_label(score: float) -> str:
    """Return grade string for a composite score."""
    if score >= 90:
        return "Excellent"
    elif score >= 70:
        return "Good"
    elif score >= 50:
        return "Fair"
    elif score >= 30:
        return "Weak"
    return "Poor"


@dataclass
class CompositeScoreBreakdown:
    total_score: float
    grade: str
    mention_rate_pct: float
    position_score: float
    engine_coverage_score: float
    cluster_diversity_score: float
    trend_score: float
    trend: str  # "improving" | "declining" | "stable" | "no_data"
    previous_score: Optional[float]
    score_change: Optional[float]

    def to_dict(self) -> dict:
        return {
            "total_score": self.total_score,
            "grade": self.grade,
            "mention_rate_pct": self.mention_rate_pct,
            "position_score": self.position_score,
            "engine_coverage_score": self.engine_coverage_score,
            "cluster_diversity_score": self.cluster_diversity_score,
            "trend_score": self.trend_score,
            "trend": self.trend,
            "previous_score": self.previous_score,
            "score_change": self.score_change,
        }


def calculate(
    mention_rate: float,
    avg_position: Optional[float],
    engines_with_mention: int,
    total_engines: int,
    clusters_with_mention: int,
    clusters_tested: int,
    previous_score: Optional[float] = None,
) -> CompositeScoreBreakdown:
    """
    Compute GEO Composite Score and return a full breakdown.

    Args:
        mention_rate:          Fraction of queries where brand was mentioned (0.0–1.0).
        avg_position:          Average position in AI responses (1-based). None → score 0.
        engines_with_mention:  Number of engines that mentioned the brand.
        total_engines:         Total number of engines queried.
        clusters_with_mention: Number of query clusters where brand appeared.
        clusters_tested:       Total number of query clusters tested.
        previous_score:        Previous composite score for trend calculation (optional).

    Returns:
        CompositeScoreBreakdown with all components clamped to [0, 100].
    """
    # --- Component 1: mention rate (0-100) ---
    mention_rate_pct = _clamp(mention_rate * 100)

    # --- Component 2: position score ---
    if avg_position is None:
        position_score = 0.0
    else:
        position_score = _clamp(100.0 - (avg_position - 1) * 10)

    # --- Component 3: engine coverage ---
    if total_engines > 0:
        engine_coverage_score = _clamp(engines_with_mention / total_engines * 100)
    else:
        engine_coverage_score = 0.0

    # --- Component 4: cluster diversity ---
    if clusters_tested > 0:
        cluster_diversity_score = _clamp(clusters_with_mention / clusters_tested * 100)
    else:
        cluster_diversity_score = 0.0

    # --- Component 5: trend ---
    if previous_score is None:
        trend_score = 50.0
        trend = "no_data"
    else:
        # Compute current score without trend component to avoid circular dependency.
        # Use the weighted sum of the first four components as the "current" value.
        current_partial = (
            mention_rate_pct * 0.30
            + position_score * 0.25
            + engine_coverage_score * 0.20
            + cluster_diversity_score * 0.15
        )
        # Normalise partial to full scale: divide by 0.90 (sum of first four weights).
        current_estimate = current_partial / 0.90

        if current_estimate > previous_score * 1.05:
            trend_score = 100.0
            trend = "improving"
        elif current_estimate < previous_score * 0.95:
            trend_score = 0.0
            trend = "declining"
        else:
            trend_score = 50.0
            trend = "stable"

    # --- Weighted composite ---
    total_score = _clamp(
        mention_rate_pct * 0.30
        + position_score * 0.25
        + engine_coverage_score * 0.20
        + cluster_diversity_score * 0.15
        + trend_score * 0.10
    )

    score_change: Optional[float] = None
    if previous_score is not None:
        score_change = round(total_score - previous_score, 2)

    return CompositeScoreBreakdown(
        total_score=round(total_score, 2),
        grade=grade_label(total_score),
        mention_rate_pct=round(mention_rate_pct, 2),
        position_score=round(position_score, 2),
        engine_coverage_score=round(engine_coverage_score, 2),
        cluster_diversity_score=round(cluster_diversity_score, 2),
        trend_score=round(trend_score, 2),
        trend=trend,
        previous_score=previous_score,
        score_change=score_change,
    )
