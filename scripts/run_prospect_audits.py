#!/usr/bin/env python3
"""
Run GEO audits on pending dental prospects and save results back to cloud.

Usage (from geo_tool root):
    python scripts/run_prospect_audits.py
    python scripts/run_prospect_audits.py --segment dental --max 20

Requires in geo_tool .env:
    NRANKAI_WORKER_KEY=<worker key from stancosmin_cloud .env>
    NRANKAI_CLOUD_URL=https://api.nrankai.com
"""

import argparse
import asyncio
import json
import logging
import os
import sys

# Add geo_tool root to path so we can import core modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from dotenv import load_dotenv

load_dotenv()

CLOUD_URL = os.environ.get("NRANKAI_CLOUD_URL", "https://api.nrankai.com")
WORKER_KEY = os.environ.get("NRANKAI_N8N_KEY", os.environ.get("NRANKAI_WORKER_KEY", ""))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def fetch_next(segment: str) -> dict | None:
    """Fetch next prospect pending audit. Returns None if nothing left."""
    headers = {"Authorization": f"Bearer {WORKER_KEY}"}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{CLOUD_URL}/prospects/next-pending-audit",
            params={"segment": segment},
            headers=headers,
        )
    if r.status_code == 204:
        return None
    r.raise_for_status()
    return r.json()


async def save_result(prospect_id: int, data: dict) -> None:
    """POST audit results back to cloud."""
    headers = {
        "Authorization": f"Bearer {WORKER_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.patch(
            f"{CLOUD_URL}/prospects/{prospect_id}/audit-data",
            json=data,
            headers=headers,
        )
    if r.status_code != 200:
        logger.error("Failed to save result for prospect %d: %s", prospect_id, r.text)
    else:
        logger.info("Saved audit for prospect %d", prospect_id)


async def audit_prospect(prospect: dict) -> dict:
    """Run GEO audit on a single prospect's website."""
    from api.workers.lead_audit_worker import (
        _fetch_page_text,
        _run_geo_audit,
        _synthesize_result,
    )

    url = prospect["url"]
    lead = {
        "company_name": prospect.get("business_name", ""),
        "language": "en",
    }

    logger.info("Auditing %s — %s", prospect["id"], url)

    try:
        text = await _fetch_page_text(url)
        geo_data = await _run_geo_audit(text, "en")
        synthesized = await _synthesize_result(url, geo_data, lead, "en")

        top_issues = synthesized.get("top_issues", [])
        geo_score = int(synthesized.get("geo_score", 0))
        summary = synthesized.get("summary", {})

        gap_text = summary.get("executive", "") or summary.get("one_liner", "")

        return {
            "top_issues": top_issues,
            "geo_score": geo_score,
            "gap_report_text": gap_text,
            "status": "scored",
        }

    except Exception as e:
        logger.error("Audit failed for prospect %d (%s): %s", prospect["id"], url, e)
        # Mark as scored with empty issues so we don't retry endlessly
        return {
            "top_issues": [],
            "geo_score": 0,
            "gap_report_text": f"Audit failed: {str(e)[:200]}",
            "status": "scored",
        }


async def main(segment: str, max_prospects: int) -> None:
    if not WORKER_KEY:
        logger.error("NRANKAI_WORKER_KEY not set in .env — aborting")
        sys.exit(1)

    logger.info("Starting prospect audit runner (segment=%s, max=%d)", segment, max_prospects)
    logger.info("Cloud: %s", CLOUD_URL)

    processed = 0
    while processed < max_prospects:
        prospect = await fetch_next(segment)
        if prospect is None:
            logger.info("No more pending prospects. Done.")
            break

        result = await audit_prospect(prospect)
        await save_result(prospect["id"], result)
        processed += 1
        logger.info("Progress: %d / %d", processed, max_prospects)

        # Small delay to avoid hammering the LLM API
        await asyncio.sleep(2)

    logger.info("Finished. Processed %d prospects.", processed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run GEO audits on pending prospects")
    parser.add_argument("--segment", default="dental", help="Prospect segment to audit")
    parser.add_argument("--max", type=int, default=100, help="Max prospects to process")
    args = parser.parse_args()

    asyncio.run(main(args.segment, args.max))
