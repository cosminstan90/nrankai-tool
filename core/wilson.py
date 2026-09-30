"""
Wilson score interval -- Pasul 9 of docs/superpowers/plans/2026-09-30-next-steps.md.

A visibility scan asked each provider a tracking query exactly once. LLM
responses vary between runs, so 1/5 vs 2/5 citations was mostly noise --
the "ing.ro absent" observation that never reproduced twice was probably
exactly this. samples_per_query lets a tracker ask N times and report a
confidence interval instead of a single, over-precise percentage.

Wilson's interval (not the naive normal approximation) because it stays
inside [0, 1] and remains sensible at small n and at p near 0 or 1 -- exactly
the range citation rates live in (many trackers see 0/3 or 3/3, not 50/100).
"""
import math
from typing import Optional, Tuple


def wilson_interval(successes: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    """
    95% (default z=1.96) Wilson score interval, as percentages (0-100) to
    match citation_rate's own scale. (None, None) when n == 0 -- there is no
    interval for zero observations, and 0-100 would misrepresent that as
    "could be anything", which is a different claim from "not measured".
    """
    if n <= 0:
        return None, None

    phat = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) / n) + (z2 / (4 * n * n)))

    low = (center - margin) / denom
    high = (center + margin) / denom
    return max(0.0, low) * 100, min(1.0, high) * 100


def intervals_overlap(low_a: Optional[float], high_a: Optional[float],
                      low_b: Optional[float], high_b: Optional[float]) -> Optional[bool]:
    """
    True if [low_a, high_a] and [low_b, high_b] overlap. None if either
    interval is missing (n == 0 on either side) -- "did it change" has no
    answer when one side was never measured.
    """
    if low_a is None or low_b is None:
        return None
    return low_a <= high_b and low_b <= high_a
