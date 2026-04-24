Add object-level access control (IDOR fix) on audit, project, and fanout session endpoints.

## Problem
GET/DELETE endpoints in `audits.py`, `projects.py`, `fanout.py` fetch objects by ID without
verifying the requester has rights to see them. Any authenticated user can access any resource
by guessing or incrementing IDs.

## Context
This is a single-user internal tool (BasicAuth global). IDOR is lower risk than on a multi-tenant
SaaS but still worth fixing for defense-in-depth and future-proofing.

## Fix
Since this is a single-user tool, the simplest fix is to confirm the resource exists and return
404 (not 403) when not found — this avoids leaking existence. The current code already does this
in most places. Verify:

1. `api/routes/audits.py` — all GET/DELETE/PATCH by `audit_id`: confirm `result is not None` check exists
2. `api/routes/projects.py` — all GET/DELETE by `project_id`: same check
3. `api/routes/fanout.py` — GET/DELETE by session `id`: same check
4. For any endpoint missing the None check, add:
   ```python
   if not obj:
       raise HTTPException(status_code=404, detail="Not found")
   ```

If multi-user support is ever added, replace with a proper ownership check:
`if obj.user_id != current_user.id: raise HTTPException(404)`

## Files
- `api/routes/audits.py`
- `api/routes/projects.py`
- `api/routes/fanout.py`
