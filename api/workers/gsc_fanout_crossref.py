"""
GSC × Fan-Out Cross-Reference (Prompt 27)
==========================================
Correlates AI fan-out queries with real Google Search Console data to classify
each query into one of four categories:

  SYNCED      ai_found=T  + serp_found=T  → ideal visibility
  AI_GAP      ai_found=F  + serp_found=T  → GEO optimisation needed (PRIORITY)
  AI_ONLY     ai_found=T  + serp_found=F  → SEO opportunity
  DOUBLE_GAP  ai_found=F  + serp_found=F  → create content

traffic_at_risk = sum of GSC clicks for AI_GAP queries.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional
from urllib.parse import quote

logger = logging.getLogger("gsc_fanout_crossref")


async def fetch_gsc_query_data(
    access_token: str,
    gsc_property: str,
    queries: List[str],
    date_range_days: int = 90,
    gl: str = "us",
) -> Dict[str, dict]:
    """
    Pull GSC search analytics for *queries* from the Search Console API.

    Returns:
        Dict[query_text → {clicks, impressions, ctr, position, found}]
    """
    try:
        import httpx
    except ImportError:
        logger.error("httpx not installed")
        return {}

    from datetime import datetime, timedelta, timezone

    end_date   = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=date_range_days)

    endpoint = (
        f"https://searchconsole.googleapis.com/v1/sites/"
        f"{quote(gsc_property, safe='')}/searchAnalytics/query"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
    }
    body = {
        "startDate":          start_date.isoformat(),
        "endDate":            end_date.isoformat(),
        "dimensions":         ["query"],
        "dimensionFilterGroups": [{
            "filters": [{
                "dimension":  "country",
                "operator":   "equals",
                "expression": gl.upper(),
            }]
        }],
        "rowLimit": 5000,
    }

    result: Dict[str, dict] = {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(endpoint, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        query_set = {q.lower().strip() for q in queries}
        for row in data.get("rows", []):
            q_text = (row.get("keys") or [""])[0].lower().strip()
            if q_text in query_set:
                result[q_text] = {
                    "clicks":      int(row.get("clicks", 0)),
                    "impressions": int(row.get("impressions", 0)),
                    "ctr":         round(row.get("ctr", 0), 4),
                    "position":    round(row.get("position", 0), 1),
                    "found":       True,
                }
    except Exception as exc:
        logger.warning("GSC API error: %s", exc)

    return result


def crossref_fanout_gsc(
    fanout_queries: List[str],
    gsc_data: Dict[str, dict],
    target_domain: Optional[str] = None,
) -> dict:
    """
    Classify each fan-out query by comparing AI fan-out presence vs GSC presence.
    All fan-out queries are considered ai_found=True by definition.
    """
    synced:     list = []
    ai_gap:     list = []
    ai_only:    list = []
    double_gap: list = []

    traffic_at_risk = 0

    for query in fanout_queries:
        gsc_row  = gsc_data.get(query.lower().strip())
        serp_found = gsc_row is not None and gsc_row.get("found", False)
        # All fan-out queries are considered found in AI by definition
        ai_found   = True

        entry = {
            "query":        query,
            "serp_found":   serp_found,
            "gsc":          gsc_row or {},
        }

        if ai_found and serp_found:
            synced.append(entry)
        elif not ai_found and serp_found:
            ai_gap.append(entry)
            traffic_at_risk += gsc_row.get("clicks", 0) if gsc_row else 0
        elif ai_found and not serp_found:
            ai_only.append(entry)
        else:
            double_gap.append(entry)

    return {
        "total_queries":    len(fanout_queries),
        "synced":           synced,
        "ai_gap":           ai_gap,
        "ai_only":          ai_only,
        "double_gap":       double_gap,
        "traffic_at_risk":  traffic_at_risk,
        "summary": {
            "synced_count":     len(synced),
            "ai_gap_count":     len(ai_gap),
            "ai_only_count":    len(ai_only),
            "double_gap_count": len(double_gap),
        },
    }
