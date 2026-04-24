Fix duplicate route definitions in `fanout.py` causing dead-letter endpoints.

## Problem
`api/routes/fanout.py` has duplicate route definitions on the same paths:
- First definition at line 899
- Second definition at line 1767 (same HTTP method + path)

FastAPI registers the FIRST match and silently ignores duplicates. This means the
handler at line 1767 is unreachable — any bug fix or feature added there has no effect.

## Fix

### Step 1 — Identify which routes are duplicated
Read lines 895-905 and 1763-1773 in `fanout.py` to see the exact path + method of each.

### Step 2 — Determine the intended behavior
- If both handlers do the same thing: delete the duplicate at line 1767
- If line 1767 is an updated version: delete the old one at line 899 and keep line 1767
- If they differ: rename one path (e.g., add a version prefix `/v2/`)

### Step 3 — Verify no other duplicates
```python
# Quick check for duplicate paths in the router:
from api.routes.fanout import router
paths = [(r.path, list(r.methods)) for r in router.routes]
from collections import Counter
dupes = [(p, m) for (p, m), c in Counter(map(tuple, [(*x,) for x in paths])).items() if c > 1]
print(dupes)
```

## Files
- `api/routes/fanout.py` lines 899, 1767
