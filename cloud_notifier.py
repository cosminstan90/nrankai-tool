"""
Notifier for nrankai-cloud after audit pipeline completion.

notify_audit_complete  — POST /webhook/audit-complete (existing flow)
attach_audit_findings  — POST /prospects/{id}/attach-audit (WLA findings push)

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


def _has_high_impact_finding(audit_result: dict) -> bool:
    """Return True if the audit result contains at least one critical/major issue."""
    issues = audit_result.get("top_issues", [])
    if not isinstance(issues, list):
        return False
    return any(
        isinstance(i, dict) and i.get("severity", "").lower() in ("critical", "major")
        for i in issues
    )


async def attach_audit_findings(
    prospect_id: int,
    audit_result: dict,
) -> bool:
    """Push raw WLA/GEO audit findings to nrankai-cloud so the prospect's
    email-preview can generate a personalized hook.

    Only calls the API when at least one critical/major finding exists.
    Requires NRANKAI_CLOUD_URL and WORKER_API_KEY in .env.
    """
    cloud_url = os.getenv("NRANKAI_CLOUD_URL", "")
    api_key = os.getenv("WORKER_API_KEY", "")

    if not cloud_url or not api_key:
        print("[cloud_notifier] NRANKAI_CLOUD_URL or WORKER_API_KEY not set, skipping attach.")
        return False

    if not _has_high_impact_finding(audit_result):
        print(f"[cloud_notifier] prospect={prospect_id}: no high-impact findings, skipping attach.")
        return False

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{cloud_url}/prospects/{prospect_id}/attach-audit",
                json={"audit_findings_json": audit_result},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if response.status_code == 200:
                print(f"[cloud_notifier] Attached audit findings to prospect {prospect_id}.")
                return True
            else:
                print(
                    f"[cloud_notifier] attach-audit failed {response.status_code}: "
                    f"{response.text[:200]}"
                )
                return False
    except Exception as e:
        print(f"[cloud_notifier] Failed to attach audit findings for prospect {prospect_id}: {e}")
        return False
