Move inline ALTER TABLE statements from `lifespan()` to proper Alembic migrations.

## Problem
`api/main.py` lines 74-99 contain multiple `ALTER TABLE` statements executed at startup.
This is a maintenance liability: the list grows with each schema change, order matters,
failures are silent, and it's impossible to roll back.

## Fix

### Step 1 — Generate a new Alembic revision for each pending change
```bash
alembic revision --autogenerate -m "add_missing_columns_batch1"
```

### Step 2 — Verify the generated migration in `migrations/versions/`
Check that `upgrade()` and `downgrade()` are correct.

### Step 3 — Remove each ALTER TABLE from `lifespan()` as it's moved to Alembic
Only remove after confirming the Alembic migration covers it.

### Step 4 — Run migrations at startup instead
In `lifespan()`, replace the raw ALTER TABLE block with:
```python
from alembic.config import Config
from alembic import command

alembic_cfg = Config("alembic.ini")
command.upgrade(alembic_cfg, "head")
```

### Important
- Do NOT delete `alembic.ini` or `migrations/env.py`
- Test migration on a copy of the DB before applying to production
- Each migration should be atomic and reversible

## Files
- `api/main.py` lines 74-99 (lifespan)
- `migrations/versions/` (new files)
- `alembic.ini`
