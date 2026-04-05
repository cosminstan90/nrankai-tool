# Website LLM Analyzer — Handover Document
## Session: Feb 26, 2026

---

## PROJECT OVERVIEW

Website LLM Analyzer — audit tool for SEO/GEO analysis using multiple LLM providers. FastAPI + SQLAlchemy + Jinja2 templates. Runs on Windows (dev) and Linux VPS (production).

**Stats:** 21 models, 19 routes, 22 templates, ~115 API endpoints, 4 LLM providers (Anthropic, OpenAI, Mistral, Gemini)

**Owner:** Cosmin — Senior SEO/GEO Specialist at ING România, 18y experience. Building this as consulting/agency tool.

---

## CURRENT STATE

### ✅ Working
- App starts and runs on Windows (`C:\geo_tool\`)
- Dashboard, navigation, all 17 phases implemented (incl. Phase 11 Before/After Tracking added this session)
- Scraping pipeline: Selenium + undetected_chromedriver → HTML → text conversion
- LLM analysis: direct mode (async concurrent) + batch mode
- Completed audit: ing.ro (545 pages, GEO_AUDIT, Anthropic) — results viewable
- Cost tracking, citation tracker, geo monitor, action cards, content gaps, gap analysis, templates, portfolio, schedules, schema gen, compare, benchmarks, PDF reports, branding

### ⚠️ Known Issues Still Open
- **View button on results page** — popup "Error loading details" may still appear on some endpoints if they return datetime objects as strings. Pattern: any API endpoint returning raw SQLAlchemy objects with `created_at` as datetime instead of string. Fix pattern: use `DateTimeField` in schemas or `.strftime()` in templates.
- **Benchmarks page** — may have similar datetime or query param issue (not yet investigated)
- **2 stuck audits** — bestetic.ro and ing.ro (first attempts) stuck on "analyzing" status with pages_analyzed=0. These are harmless zombies from before the `load_dotenv` fix. Can be deleted via DB or API.
- **Batch mode custom_id** — fixed (sanitized to `[a-zA-Z0-9_-]`) but not yet tested end-to-end

### 🔧 Key Fixes Applied This Session
1. **4 missing SQLAlchemy models** added to `database.py`: ActionCard, CompetitorGapAnalysis, ContentGap, BrandingConfig
2. **11 missing Pydantic schemas** added to `schemas.py`
3. **`models/__init__.py`** aligned exports with actual schemas
4. **`pdf_reports.py`** — `get_db_session` → `get_db` (6 occurrences)
5. **`action_cards.py`** — replaced missing `llm_providers` module with `call_llm_for_summary`
6. **`main.py`** — added `get_providers_for_ui`, `get_tier_presets` imports from provider_registry
7. **`main.py`** — added debug exception handler (shows real errors instead of generic 500)
8. **`costs.html`** — Chart.js containers wrapped in fixed-height divs
9. **Phase 11 implemented**: TrackingProject + TrackingSnapshot models, 8 API endpoints, template with Chart.js
10. **`website_llm_analyzer.py`** — batch custom_id sanitized with regex
11. **`audit_worker.py`** — added `load_dotenv()` for background tasks (ROOT CAUSE of silent analysis failures)
12. **`schemas.py`** — datetime fields accept both `str` and `datetime` (DateTimeField)
13. **`schema_gen.html` + `briefs.html`** — `created_at[:10]` → `.strftime('%Y-%m-%d')`
14. **`content_briefs.py`** — `audit_id` query param made optional

---

## FILE STRUCTURE

```
C:\geo_tool\                          # Windows dev
/opt/llm-analyzer/                    # Linux production (planned)

api/
  main.py                             # FastAPI app, page routes, debug handler
  provider_registry.py                # LLM provider config, get_providers_for_ui()
  models/
    database.py                       # 21 SQLAlchemy models
    schemas.py                        # 15 Pydantic schemas
    __init__.py                       # Schema exports
  routes/
    18 route files + __init__.py      # 19 routers total
  templates/
    22 HTML templates                 # Jinja2 + Alpine.js + Tailwind
  workers/
    audit_worker.py                   # Background pipeline (scrape→convert→analyze→score)
  middleware/
    auth.py                           # Basic HTTP auth

website_llm_analyzer.py               # Batch file preparation + job submission
direct_analyzer.py                    # Async concurrent LLM analysis
monitor_completion_LLM_batch.py       # Batch job monitor + result parsing
web_scraper.py                        # Selenium scraper (undetected_chromedriver)
config.py                             # Central config
prompts/                              # 20 YAML audit prompt templates
```

---

## DEPLOYMENT PLAN

**Target:** `https://geo.stancosmin.com` on VPS with CloudPanel (Ubuntu 24)

**Method:** systemd + CloudPanel reverse proxy (no Docker)

**Files ready:**
- `deploy_cloudpanel.sh` — automated setup script
- Creates systemd service `llm-analyzer` on port 8000
- CloudPanel handles domain + SSL (Let's Encrypt)

**Steps:**
1. Upload ZIP to VPS
2. `bash deploy_cloudpanel.sh`
3. In CloudPanel: Add Reverse Proxy site → `http://127.0.0.1:8000`
4. Enable SSL
5. Edit `.env` with API keys

---

## DEBUGGING PATTERNS

**Silent 500 errors:** Debug exception handler in `main.py` shows full traceback in browser. If removed for production, check `journalctl -u llm-analyzer -f`

**Audit stuck on "analyzing":** Usually means API key not loaded. Check `audit_worker.py` has `load_dotenv()`. Test: `python -c "from dotenv import load_dotenv; load_dotenv('.env'); import os; print(os.getenv('ANTHROPIC_API_KEY')[:20])"`

**Template errors:** Usually datetime objects passed where strings expected. Fix with `.strftime()` in Jinja2 or `DateTimeField` in Pydantic schemas.

**Import errors at startup:** Check `api/routes/__init__.py` matches actual route files. All routes use eager import.

---

## MONETIZATION DIRECTION

Consulting-led, tool-enabled. Target: Romanian market, clinics/ecommerce/agencies.
- One-time GEO audit: 300-500€ (cost ~1€ API)
- Monthly monitoring: 200-400€/mo
- Full strategy: 500-800€/mo
- Timeline: first paying clients within 2 months

Plan document: `Plan_Monetizare_GEO_Consulting.md`
