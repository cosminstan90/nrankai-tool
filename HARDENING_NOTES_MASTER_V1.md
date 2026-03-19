# MASTER v1.0 Hardened – Notes

## Fixes applied
- Fixed `api/main.py` dashboard page context bug (`providers_ui` / `get_tier_presets` undefined).
- Added `providers_ui` + `tier_presets` to `/new` page template context for provider selector scripts.
- Added missing `func` import in `api/models/database.py` used by `init_db_async()`.
- Added server-rendered pages for integrated modules:
  - `/schema`
  - `/citations`
  - `/portfolio`
  - `/costs`
  - `/gap-analysis`
  - `/content-gaps`
  - `/action-cards`
  - `/templates`
- Extended health endpoint provider reporting to include Gemini + Perplexity.
- Updated `uvicorn.run()` module path to `api.main:app` for direct execution consistency.
- Updated main navigation with links to all integrated modules.
- Added `extra_head` block support in `base.html` for templates like `action_cards.html`.

## Cleanup applied
- Removed Python cache folders (`__pycache__`) from package tree.
- Removed accidental brace-named directories created by archive expansion:
  - `api/{routes,models,workers,templates,static}`
  - `api/static/{css,js,img}`
- Excluded local utility files from runtime concerns (`.wget-hsts`, `.npmrc` remain but can be deleted safely).

## Next recommended step (runtime hardening)
1. Create a fresh venv and install both `requirements.txt` and `api/requirements.txt` (or merge them).
2. Start app and smoke-test all UI pages + API endpoints.
3. Validate database schema on an existing SQLite DB (column drift may exist from older versions).
4. Add Alembic migrations if this becomes long-lived.
