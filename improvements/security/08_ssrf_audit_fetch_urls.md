Fix SSRF in audit URL fetches and sitemap counter.

## Problem
Multiple places fetch user-controlled URLs without IP/private-range validation:

1. `api/routes/audits.py:142` — single page audit fetches `url` from request body directly
2. `api/routes/audits.py:421` — sitemap counter fetches `url` from query parameter
3. `api/routes/gsc/optimizer.py:114` — page optimize fetches `req.url` from request body

Any of these can be pointed at `http://169.254.169.254/` (AWS metadata), `http://localhost:6379/`
(Redis), `http://10.0.0.1/` (internal services), etc.

`lead_audit_worker.py` already has IP blocking — replicate that pattern everywhere.

## Fix

### Create a shared URL validation utility (e.g., `api/utils/url_validator.py`)
```python
import ipaddress
import socket
from urllib.parse import urlparse

BLOCKED_HOSTS = {"localhost", "0.0.0.0", "metadata.google.internal", "169.254.169.254"}

def validate_external_url(url: str, field_name: str = "url") -> str:
    """Raises ValueError if URL points to private/internal network."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field_name} must use http or https")
    host = parsed.hostname or ""
    if host in BLOCKED_HOSTS:
        raise ValueError(f"{field_name} must not point to internal hosts")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"{field_name} must not point to private IP ranges")
    except ValueError as e:
        if "does not appear to be" not in str(e):
            raise  # re-raise our own validation errors
        # host is a domain name — optionally resolve and check resolved IP
        # For high security: resolve and re-check
    return url
```

### Apply in each location:
```python
from api.utils.url_validator import validate_external_url

# audits.py:142
url = validate_external_url(request_body.url, "website")

# audits.py:421
url = validate_external_url(query_param_url, "sitemap_url")

# optimizer.py:114
url = validate_external_url(req.url, "url")
```

### Also update Pydantic validators in schemas
Add `validate_external_url` call inside `@field_validator` for any URL field that
results in an outbound HTTP request.

## Files
- `api/routes/audits.py` lines 142, 421
- `api/routes/gsc/optimizer.py` line 114
- `api/models/schemas.py` — URL field validators
- New file: `api/utils/url_validator.py`
