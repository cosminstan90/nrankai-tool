Fix path traversal via `website` field in audit_worker and audits route.

## Problem
`website` input from the user flows unsanitized into filesystem operations:

1. `api/workers/audit_worker.py:170` — `_safe_dir()` builds a directory path from `website`
   but does NOT strip `/` or `../` sequences. An input like `../../etc` creates directories
   outside the intended working area.

2. `api/workers/audit_worker.py:205` — the result of `_safe_dir()` is used as working
   directory for `input_html/`, `input_llm/`, `output_*` subdirectories.

3. `api/routes/audits.py:619` — `shutil.rmtree(path_built_from_audit.website)` deletes
   arbitrary directories if `website` contains traversal sequences.

## Fix

### Step 1 — Sanitize `website` in `_safe_dir()`
```python
import re
from pathlib import Path

def _safe_dir(website: str, base_dir: Path) -> Path:
    # Strip scheme, remove all non-alphanumeric except dot and dash
    clean = re.sub(r'^https?://', '', website.lower().strip())
    clean = re.sub(r'[^a-z0-9.\-]', '_', clean)
    clean = clean.strip('._')          # no leading dots or underscores
    if not clean:
        raise ValueError(f"Invalid website for directory: {website!r}")
    target = (base_dir / clean).resolve()
    # Confirm the resolved path is still inside base_dir
    if not str(target).startswith(str(base_dir.resolve())):
        raise ValueError(f"Path traversal detected: {website!r}")
    return target
```

### Step 2 — Fix `shutil.rmtree` in `audits.py:619`
Apply the same `_safe_dir()` sanitization before constructing the path to delete.
Never build a delete path directly from user input. After sanitization, additionally
verify the path exists and is a subdirectory of the expected base before deletion:
```python
safe = _safe_dir(audit.website, BASE_WORK_DIR)
if safe.exists() and safe.is_dir():
    shutil.rmtree(safe)
```

### Step 3 — Add domain validation to Pydantic model (`api/models/schemas.py`)
The `website` field must reject path-looking inputs at the API boundary:
```python
@field_validator("website")
@classmethod
def validate_website(cls, v: str) -> str:
    v = v.strip()
    # Strip scheme for validation
    bare = re.sub(r'^https?://', '', v.lower())
    if not re.match(r'^[a-z0-9]([a-z0-9\-\.]{0,250}[a-z0-9])?$', bare):
        raise ValueError("Invalid website — must be a domain name, not a path")
    if '..' in v or '/' in bare:
        raise ValueError("Website must be a domain, not a path")
    return v
```

## Files
- `api/workers/audit_worker.py` lines 170, 205
- `api/routes/audits.py` line 619
- `api/models/schemas.py` — `website` field validator
