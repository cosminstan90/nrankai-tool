Replace all 152 occurrences of deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)`.

## Problem
Python 3.12 deprecates `datetime.utcnow()`. The project has ~152 instances across models,
workers, and routes. CLAUDE.md §6 explicitly forbids this pattern.

## Fix
Global search-and-replace:

### Step 1 — Fix import lines
Find files using `datetime.utcnow` and ensure they import `timezone`:
```python
# Before
from datetime import datetime

# After
from datetime import datetime, timezone
```

### Step 2 — Replace calls
```python
# Before
datetime.utcnow()

# After
datetime.now(timezone.utc)
```

### Step 3 — Fix SQLAlchemy model defaults
```python
# Before
created_at = Column(DateTime, default=datetime.utcnow)

# After
created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
```
Note: `default=datetime.now(timezone.utc)` (with parentheses) is WRONG — it captures
the value at class definition time. Always use a lambda or `server_default`.

### Step 4 — Fix string formatting using utcnow
```python
# Before
datetime.utcnow().strftime(...)

# After
datetime.now(timezone.utc).strftime(...)
```

## Verification
After fix, run: `grep -r "utcnow" api/` should return zero results.

## Files
All files in `api/models/`, `api/routes/`, `api/workers/`
