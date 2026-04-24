Fix `old_score` reported incorrectly in `rerun_single_page` due to value being overwritten before read.

## Problem
`api/routes/compare.py`:
- Line 575: `page_result.score` is overwritten with the new score
- Line 615: `old_score` is read from `page_result.score` AFTER the overwrite

This means the response always reports `old_score == new_score`, making it impossible
to see what the score was before the rerun.

## Fix
Capture the old score BEFORE any update:

```python
# Line ~575 area — BEFORE overwriting page_result.score:
old_score = page_result.score  # capture first

# ... run new analysis ...

# Now update:
page_result.score = new_score  # overwrite happens here

# Line ~615 — use the captured value:
return {
    "old_score": old_score,   # captured before overwrite
    "new_score": new_score,
    ...
}
```

## Files
- `api/routes/compare.py` lines 575, 615
