"""
Notifier for nrankai-cloud after audit pipeline completion.

Sends a POST to /webhook/audit-complete with audit results.
Requires NRANKAI_CLOUD_URL and WORKER_API_KEY in .env.
"""

import httpx
import os
import json
from pathlib import Path
from datetime import datetime, timezone


async def notify_audit_complete(
    website: str,
    audit_type: str,
    prospect_id: str | None = None,
    campaign_id: str | None = None,
    scores_file: str | None = None,
) -> bool:
    cloud_url = os.getenv("NRANKAI_CLOUD_URL", "")
    api_key = os.getenv("WORKER_API_KEY", "")

    if not cloud_url or not api_key:
        print("[cloud_notifier] NRANKAI_CLOUD_URL or WORKER_API_KEY not set, skipping.")
        return False

    payload = {
        "website": website,
        "audit_type": audit_type,
        "prospect_id": prospect_id,
        "campaign_id": campaign_id,
        "status": "completed",
        "source": "nrankai-tool",
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    if scores_file and Path(scores_file).exists():
        try:
            with open(scores_file) as f:
                payload["scores"] = json.load(f)
        except Exception as e:
            print(f"[cloud_notifier] Could not read scores file: {e}")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{cloud_url}/webhook/audit-complete",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if response.status_code == 200:
                print(f"[cloud_notifier] Notified nrankai-cloud successfully.")
                return True
            else:
                print(f"[cloud_notifier] Unexpected status {response.status_code}: {response.text[:200]}")
                return False
    except Exception as e:
        print(f"[cloud_notifier] Failed to notify nrankai-cloud: {e}")
        return False
