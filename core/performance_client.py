"""
Core Web Vitals fetchers -- Chrome UX Report (real-user field data) and
PageSpeed Insights v5 (lab data from one simulated Lighthouse run).

Etapa 2 of docs/IMPROVEMENTS_PLAN.md. Both are free Google APIs, keyed by a
single API key (no OAuth) -- PAGESPEED_API_KEY, which the Google Cloud
Console calls the "PageSpeed Insights API" key but which also authorizes
CrUX requests.

CrUX is the primary source: it's what real visitors actually experienced,
not a simulated run. PSI/Lighthouse is secondary -- useful for "what to fix"
diagnostics, not for "how are real users doing".
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

CRUX_RECORD_URL  = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
CRUX_HISTORY_URL = "https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord"
PSI_URL          = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Google's published thresholds (2026): good <= threshold, poor > threshold,
# everything between is "needs-improvement". Not returned by the CrUX API
# itself -- it only gives the raw p75 and a 3-bucket histogram; the rating
# below is derived from the same thresholds that define those buckets, which
# is simpler and doesn't depend on trusting histogram density math to agree
# with which bucket the p75 value actually falls in.
_THRESHOLDS = {
    "lcp":  (2500, 4000),   # ms
    "inp":  (200, 500),     # ms
    "cls":  (0.10, 0.25),   # unitless
    "fcp":  (1800, 3000),   # ms
    "ttfb": (800, 1800),    # ms
}

_CRUX_METRIC_KEYS = {
    "lcp":  "largest_contentful_paint",
    "inp":  "interaction_to_next_paint",
    "cls":  "cumulative_layout_shift",
    "fcp":  "first_contentful_paint",
    "ttfb": "experimental_time_to_first_byte",
}


def _rate(metric: str, value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    good, poor = _THRESHOLDS[metric]
    if value <= good:
        return "good"
    if value <= poor:
        return "needs-improvement"
    return "poor"


def _cwv_date_str(d: dict) -> Optional[str]:
    """CrUX's {"year": Y, "month": M, "day": D} -> "YYYY-MM-DD", or None."""
    if not d or "year" not in d:
        return None
    return f"{d['year']:04d}-{d.get('month', 1):02d}-{d.get('day', 1):02d}"


async def fetch_crux(url: str, strategy: str = "mobile", is_origin: bool = False) -> Optional[dict]:
    """
    Current CrUX field data for one URL (or, with is_origin=True, an entire
    origin -- useful when the specific URL has too little traffic for its own
    record but the site overall has enough).

    Returns None when CrUX genuinely has no data for this URL/origin/form
    factor combination -- a real, common, and entirely expected outcome (CrUX
    only covers origins/URLs above a minimum real-user traffic threshold), not
    an error. Callers must treat None as "no field data available", not retry
    it as a transient failure.
    """
    api_key = os.getenv("PAGESPEED_API_KEY")
    if not api_key:
        logger.warning("PAGESPEED_API_KEY not set -- skipping CrUX fetch for %s", url)
        return None

    form_factor = "PHONE" if strategy == "mobile" else "DESKTOP"
    body = {
        ("origin" if is_origin else "url"): url,
        "formFactor": form_factor,
        "metrics": list(_CRUX_METRIC_KEYS.values()),
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(CRUX_RECORD_URL, params={"key": api_key}, json=body)
        if resp.status_code == 404:
            return None  # no CrUX data for this URL/form factor -- expected, not an error
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("CrUX fetch failed for %s (%s): %s", url, strategy, exc)
        return None

    metrics = data.get("record", {}).get("metrics", {})
    p75 = {
        short: metrics.get(long_key, {}).get("percentiles", {}).get("p75")
        for short, long_key in _CRUX_METRIC_KEYS.items()
    }
    period = data.get("record", {}).get("collectionPeriod", {})

    return {
        "p75": p75,
        "rating": {m: _rate(m, v) for m, v in p75.items()},
        "period_start": _cwv_date_str(period.get("firstDate")),
        "period_end":   _cwv_date_str(period.get("lastDate")),
        "raw": data,
    }


async def fetch_crux_history(url: str, strategy: str = "mobile", is_origin: bool = False) -> list[dict]:
    """
    Up to 25 weekly (well: ~28-day rolling window) CrUX snapshots for one
    URL/origin -- lets a first check backfill months of real trend data
    instead of waiting 25 weeks for it to accumulate one entry at a time.

    Returns [] on no data or on error; a caller with no history to show is
    exactly like a caller with no current data to show (see fetch_crux) --
    both are "CrUX doesn't have this", not a fetch failure to retry.
    """
    api_key = os.getenv("PAGESPEED_API_KEY")
    if not api_key:
        return []

    form_factor = "PHONE" if strategy == "mobile" else "DESKTOP"
    body = {
        ("origin" if is_origin else "url"): url,
        "formFactor": form_factor,
        "metrics": list(_CRUX_METRIC_KEYS.values()),
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(CRUX_HISTORY_URL, params={"key": api_key}, json=body)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("CrUX history fetch failed for %s (%s): %s", url, strategy, exc)
        return []

    record = data.get("record", {})
    metrics = record.get("metrics", {})
    periods = record.get("collectionPeriods", [])

    series = {
        short: metrics.get(long_key, {}).get("percentilesTimeseries", {}).get("p75s", [])
        for short, long_key in _CRUX_METRIC_KEYS.items()
    }

    points = []
    for i, period in enumerate(periods):
        p75 = {m: (series[m][i] if i < len(series[m]) else None) for m in _CRUX_METRIC_KEYS}
        if all(v is None for v in p75.values()):
            continue
        points.append({
            "p75": p75,
            "rating": {m: _rate(m, v) for m, v in p75.items()},
            "period_start": _cwv_date_str(period.get("firstDate")),
            "period_end":   _cwv_date_str(period.get("lastDate")),
        })
    return points


async def fetch_psi(url: str, strategy: str = "mobile") -> Optional[dict]:
    """
    One PageSpeed Insights (Lighthouse) run for a URL -- lab data, one
    simulated pass, not real users. Returns None on any fetch/parse failure;
    unlike CrUX's 404, PSI failing usually means a transient error or the URL
    being unreachable by Google's crawler, worth logging distinctly.
    """
    api_key = os.getenv("PAGESPEED_API_KEY")
    if not api_key:
        logger.warning("PAGESPEED_API_KEY not set -- skipping PSI fetch for %s", url)
        return None

    params = {
        "url": url,
        "key": api_key,
        "strategy": strategy,
        "category": "performance",
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:  # PSI runs a real Lighthouse pass; can take 30s+
            resp = await client.get(PSI_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("PSI fetch failed for %s (%s): %s", url, strategy, exc)
        return None

    lh = data.get("lighthouseResult", {})
    audits = lh.get("audits", {})

    def _numeric(audit_id: str) -> Optional[float]:
        return audits.get(audit_id, {}).get("numericValue")

    perf_score = lh.get("categories", {}).get("performance", {}).get("score")

    return {
        "performance_score": round(perf_score * 100) if perf_score is not None else None,
        "lcp":         _numeric("largest-contentful-paint"),
        "cls":         _numeric("cumulative-layout-shift"),
        "tbt":         _numeric("total-blocking-time"),
        "fcp":         _numeric("first-contentful-paint"),
        "speed_index": _numeric("speed-index"),
        "raw": data,
    }
