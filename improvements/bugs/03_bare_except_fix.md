Fix bare `except: pass` and `except Exception: pass` that silently swallow errors.

## Problem
~10 locations in `api/main.py`, `api/models/database.py`, `api/middleware/auth.py` catch
exceptions and do nothing. This means schema migration failures, DB init errors, and auth
errors are invisible — the app starts but behaves incorrectly.

## Fix
For each bare except, determine the appropriate action:

### Schema migration failures (main.py lines 74-99)
```python
# Before
try:
    await conn.execute(text("ALTER TABLE ..."))
except:
    pass

# After
try:
    await conn.execute(text("ALTER TABLE ..."))
except Exception:
    pass  # Column already exists — expected on subsequent startups
```
Keep the pass BUT add a comment explaining WHY it's intentional (column already exists).
For truly unexpected errors, log them:
```python
except Exception as e:
    logger.debug(f"ALTER TABLE skipped (likely already exists): {e}")
```

### DB init failures (database.py)
```python
# Before
except:
    pass

# After
except Exception as e:
    logger.error(f"DB initialization error: {e}", exc_info=True)
    raise  # Don't swallow — app shouldn't start with broken DB
```

### Auth middleware (auth.py line 73)
Review the specific exception being caught. If it's decoding a Basic Auth header,
a malformed header should return 401, not silently pass.

## Files
- `api/main.py` lines 74-99
- `api/models/database.py`
- `api/middleware/auth.py` line 73
