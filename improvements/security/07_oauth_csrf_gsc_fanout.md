Fix OAuth CSRF vulnerability in GSC fanout OAuth flow.

## Problem
`api/routes/gsc_fanout.py`:

- Line 58: `state` parameter is set to `project_id` directly — predictable, not random.
- Line 90: In the OAuth callback, `project_id = state` with no nonce/session verification.

An attacker can initiate their own OAuth flow and craft a callback URL with a victim's
`project_id` as `state`, binding their Google account to the victim's project (account
hijacking / data exfiltration).

## Fix

### Step 1 — Generate a random nonce at authorization initiation
```python
import secrets

# In the authorize endpoint (line ~58):
nonce = secrets.token_urlsafe(32)
state = f"{project_id}:{nonce}"

# Store nonce server-side (in DB or cache) tied to project_id with 10-min TTL
await store_oauth_nonce(project_id, nonce, db)
# Or use a signed state with HMAC if no DB storage preferred:
import hmac, hashlib
secret = os.getenv("SECRET_KEY", "change-me")
sig = hmac.new(secret.encode(), state.encode(), hashlib.sha256).hexdigest()[:16]
state = f"{project_id}:{nonce}:{sig}"
```

### Step 2 — Verify nonce in callback (line ~90)
```python
# In the callback endpoint:
parts = state.split(":")
if len(parts) != 3:
    raise HTTPException(400, "Invalid OAuth state")
project_id, nonce, sig = parts

# Re-verify HMAC
expected_sig = hmac.new(secret.encode(), f"{project_id}:{nonce}".encode(), hashlib.sha256).hexdigest()[:16]
if not secrets.compare_digest(sig, expected_sig):
    raise HTTPException(400, "OAuth state signature invalid — possible CSRF")
```

### Step 3 — Check missing imports in `oauth_sync.py`
Lines 81 and 120 reference constants that exist in `_shared.py:52` but are not imported
in `oauth_sync.py`. Add the missing import:
```python
from api.routes.gsc._shared import CONSTANT_NAME  # replace with actual constant name
```
Verify by checking what `oauth_sync.py` uses at lines 81 and 120 against what `_shared.py:52` exports.

## Files
- `api/routes/gsc_fanout.py` lines 58, 90
- `api/routes/gsc/oauth_sync.py` lines 81, 120
- `api/routes/gsc/_shared.py` line 52
