Add file size validation to all UploadFile endpoints.

## Problem
`UploadFile` fields in multiple routes have no `max_size` constraint. An attacker can upload
a multi-GB file causing memory exhaustion or disk fill (DoS).

## Affected files
- `api/routes/pdf_reports.py` — logo upload
- `api/routes/ads.py` — CSV upload (~line 234)
- `api/routes/ga4.py` — CSV upload

## Fix
For each UploadFile handler, read content and check size before processing:

```python
MAX_LOGO_SIZE = 5 * 1024 * 1024   # 5 MB
MAX_CSV_SIZE  = 20 * 1024 * 1024  # 20 MB

content = await file.read()
if len(content) > MAX_SIZE:
    raise HTTPException(status_code=413, detail="File too large")
# then use content (BytesIO) instead of re-reading file
```

Do NOT use `File(None, max_size=...)` — FastAPI's `max_size` parameter on File() is not
reliably enforced across all versions. Read-then-check is more portable.

Add size constants at the top of each file, not inline.
