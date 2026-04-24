Fix SSRF risk in webhook URL validation — block private IP ranges.

## Problem
`api/workers/audit_worker.py` `fire_webhook()` accepts any URL including internal ones
(http://localhost:8000/..., http://192.168.x.x/...). This allows SSRF if webhook URL
is user-controlled.

`lead_audit_worker.py` already has IP blocking — replicate that pattern here.

## Fix
Add a validation helper and call it before any outbound webhook HTTP request:

```python
import ipaddress
from urllib.parse import urlparse

def _validate_webhook_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Webhook URL must use http or https")
    hostname = parsed.hostname or ""
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError("Webhook URL must not point to private/internal addresses")
    except ValueError as e:
        if "does not appear to be an IPv4 or IPv6" not in str(e):
            raise
    if hostname in ("localhost", "0.0.0.0"):
        raise ValueError("Webhook URL must not point to localhost")
```

Call `_validate_webhook_url(url)` at the start of `fire_webhook()` before any HTTP call.

Also add the same validation in `api/models/schemas.py` Pydantic validator for any
`webhook_url` field — fail fast at input time, not at send time.

## Files
- `api/workers/audit_worker.py` — `fire_webhook()` function
- `api/models/schemas.py` — webhook URL fields
