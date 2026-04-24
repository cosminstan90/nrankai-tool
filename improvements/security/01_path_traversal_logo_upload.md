Fix path traversal vulnerability in logo file upload in `api/routes/pdf_reports.py`.

## Problem
Lines 69-71 and 146: `logo.filename` from user upload is used directly without sanitization.
An attacker can send a filename like `../../etc/passwd.png` to write arbitrary files on the server.

## Fix
1. In the upload handler (line 69), replace the filename extraction with a UUID-based name:
   ```python
   import uuid
   ext = logo.filename.rsplit(".", 1)[-1].lower()
   if ext not in {"png", "jpg", "jpeg", "webp", "gif", "svg"}:
       raise HTTPException(status_code=400, detail="Invalid file type")
   safe_filename = f"{uuid.uuid4().hex}.{ext}"
   ```
2. On line 146 where `branding.logo_path` is used, construct the path using `os.path.basename()` or use the UUID filename stored in DB.
3. Validate file content type via `logo.content_type` in addition to extension.
4. Add max file size check: `if len(content) > 5 * 1024 * 1024: raise HTTPException(400, "File too large")`

## Files
- `api/routes/pdf_reports.py` lines 57-75, 143-149
