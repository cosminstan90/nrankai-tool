# geo_tool — Project Summary
> Last updated: 2026-04-06 | Production: app.nrankai.com | Port: 8000

---

## Ce este

**geo_tool** este un SaaS de audit GEO/SEO construit pe FastAPI + SQLAlchemy async (aiosqlite).
Rulează local pe Windows, expus la `app.nrankai.com`. Interfața este server-rendered HTML (Jinja2) fără framework JS.

---

## Structura curentă (post-refactoring)

```
geo_tool/
├── core/                        # Motor de analiză (19 module)
│   ├── config.py                # Config & .env loading
│   ├── logger.py
│   ├── web_scraper.py           # Playwright scraping
│   ├── html2llm_converter.py    # HTML → LLM-friendly text
│   ├── direct_analyzer.py       # Analiză pagini via LLM
│   ├── audit_builder.py         # Pipeline orchestration
│   ├── generate_report.py       # Raport HTML/PDF
│   ├── generate_dashboard.py    # Dashboard data
│   ├── validate_audit.py        # Validare rezultate
│   ├── determine_score.py       # Scoring GEO
│   ├── history_tracker.py       # Tracking istoric audituri
│   ├── compare_audits.py        # Comparare audituri
│   ├── content_chunker.py       # Chunking conținut
│   ├── cross_reference_analyzer.py
│   ├── perplexity_researcher.py # Cercetare via Perplexity API
│   ├── prompt_loader.py         # Prompt templates loader
│   ├── scrape_state.py
│   ├── website_llm_analyzer.py
│   └── monitor_completion_LLM_batch.py
│
├── api/
│   ├── main.py                  # App FastAPI (~236 linii)
│   ├── models/
│   │   ├── _base.py             # Base, engine, AsyncSessionLocal
│   │   ├── audit.py             # Audit, AuditResult, AuditLog, etc.
│   │   ├── analytics.py         # GSC, GA4, Ads, Keywords, Insights
│   │   ├── content.py           # ContentBrief, Schema, Citations, etc.
│   │   ├── infra.py             # Benchmarks, Schedules, Costs, Billing
│   │   └── database.py          # Re-exporter backward-compat + init_db()
│   ├── routes/
│   │   ├── pages/               # 41 rute HTML (Jinja2) — subpackage
│   │   │   ├── _shared.py       # templates, constante, helpers
│   │   │   ├── dashboard.py     # / și /new (4 rute)
│   │   │   ├── audit_views.py   # /audits/*, /pages, /sites/*, /compare (7 rute)
│   │   │   ├── tool_views.py    # /schema, /keyword-research, /optimize, etc. (7 rute)
│   │   │   ├── integration_views.py  # /gsc/*, /ga4/*, /ads/* (7 rute)
│   │   │   ├── analytics_views.py    # /insights/*, /geo-monitor, /benchmarks (5 rute)
│   │   │   └── settings_views.py     # /settings, /briefs, /portfolio, etc. (11 rute)
│   │   ├── audits.py            # 11 endpoints audit CRUD
│   │   ├── gsc/                 # Google Search Console (subpackage)
│   │   │   ├── _shared.py       # OAuth helpers
│   │   │   ├── properties.py    # 7 endpoints
│   │   │   ├── oauth_sync.py    # 7 endpoints
│   │   │   └── optimizer.py     # 4 endpoints
│   │   ├── keyword_research.py  # 5 endpoints
│   │   ├── content_briefs.py    # 8 endpoints
│   │   ├── gap_analysis.py      # 5 endpoints (competitor gap)
│   │   ├── content_gaps.py      # 9 endpoints
│   │   ├── action_cards.py      # 6 endpoints
│   │   ├── citation_tracker.py  # 9 endpoints
│   │   ├── schema_gen.py        # 8 endpoints
│   │   ├── pdf_reports.py       # 6 endpoints
│   │   ├── geo_monitor.py       # 7 endpoints
│   │   ├── schedules.py         # 8 endpoints
│   │   ├── benchmarks.py        # 5 endpoints
│   │   ├── compare.py           # 6 endpoints
│   │   ├── costs.py             # 8 endpoints
│   │   ├── insights.py          # 5 endpoints
│   │   ├── ga4.py               # 7 endpoints
│   │   ├── ads.py               # 7 endpoints
│   │   ├── tracking.py          # 8 endpoints
│   │   ├── cross_reference.py   # 5 endpoints
│   │   ├── llms_txt.py          # 5 endpoints
│   │   ├── guide.py             # 5 endpoints
│   │   ├── templates_manager.py # 7 endpoints
│   │   └── health.py            # 2 endpoints
│   ├── utils/
│   │   ├── errors.py            # raise_not_found(), raise_bad_request(), raise_conflict()
│   │   └── task_runner.py       # create_tracked_task() — GC-safe + timeout
│   ├── workers/
│   │   ├── audit_worker.py      # Background pipeline audit (timeouts + retry)
│   │   └── lead_audit_worker.py # Worker pentru api.nrankai.com leads
│   └── templates/               # Jinja2 HTML templates
│
├── prompts/                     # Prompt YAML templates
├── migrations/                  # Alembic
└── docs/                        # Changelogs + documentatie
```

**Total: ~232 rute HTTP**

---

## Ce poate face (features)

### Audit GEO/SEO
- Audit complet website: scraping Playwright + analiză LLM pagină cu pagină
- Scoruri pe categorii: E-E-A-T, Structură, Conținut, Tehnic, GEO
- Rapoarte PDF + HTML cu recomandări
- Audit programat (schedules) + monitorizare continuă (geo_monitor)
- Comparare între audituri (istoric)
- Resume audit întrerupt
- Cost tracking per audit (tokens LLM)

### Keyword Research
- Generare cuvinte cheie + clasificare
- Integrare Google Search Console (OAuth + sync)
- Integrare Google Ads (search terms, campanii)
- Integrare GA4 (pagini, canale)
- Page optimizer (GSC) cu Schema.org builder
- Cannibalization detector

### Content
- Content briefs generate
- Competitor gap analysis
- Content gaps (audit gaps)
- Action cards cu priorități
- Citation tracker (surse citate de LLM-uri)
- Schema markup generator
- Cross-reference analyzer
- LLMs.txt generator
- Repair guide generator

### Infrastructură
- Benchmark projects
- Tracking snapshots
- Portfolio view (multi-site)
- Client billing + marje
- Branding config
- Insights (carduri automate)
- Lead audit worker (integrat cu api.nrankai.com)

---

## Stack tehnic

| Componentă | Tehnologie |
|---|---|
| Backend | FastAPI + Python 3.11+ |
| DB | SQLite async (aiosqlite + SQLAlchemy) |
| Migrations | Alembic |
| Scraping | Playwright (Chromium) |
| LLM | OpenAI / Anthropic / Perplexity (configurabil) |
| Frontend | Jinja2 server-rendered, fără JS framework |
| Server | Uvicorn, Windows, port 8000 |
| Deps | uv (lockfile) |

---

## Ce trebuie îmbunătățit

### Prioritate mare
1. **Tests** — zero teste automate. Cel puțin unit tests pentru `core/` (scoring, validation, chunking) și integration tests pentru endpoint-urile critice.
2. **Auth / multi-tenant** — momentan fără autentificare; dacă se merge spre SaaS real, trebuie user accounts.
3. **`core/generate_dashboard.py` + `generate_report.py` (~2000 linii fiecare)** — cele mai mari fișiere din proiect, greu de navigat.

### Prioritate medie
4. **Config management** — `core/config.py` folosește `.env` direct; ar beneficia de Pydantic Settings cu validare la startup.
5. **Logging structurat** — mix de `print()` și logger custom; ar trebui unificat pe structlog sau logging standard cu JSON output.
6. **`api/utils/errors.py` adoptare completă** — helper-ele există dar nu sunt folosite încă în toate route-urile; migrare progresivă cu `raise_not_found()` în loc de HTTPException inline.

### Prioritate mică
7. **`api/routes/action_cards.py` (1172 linii)** — candidat pentru refactoring.
8. **API docs** — FastAPI auto-docs la `/docs` există dar fără descrieri pe endpoint-uri (docstrings lipsesc).
9. **Alembic migrations** — de verificat că sunt sincronizate cu modelele după split-ul din `database.py`.

---

## Refactoring-uri finalizate (sesiunea curentă)

| Task | Commit | Detalii |
|---|---|---|
| `core/` package | `e14df80` | 19 module mutate din root, sys.path hacks eliminate |
| `api/main.py` split | `e14df80` | 1710 → 236 linii, routes HTML extrase |
| `api/models/` split | `e14df80` | database.py 1592 → 236 linii, 5 fișiere domeniu |
| `api/routes/gsc/` subpackage | `e14df80` | gsc.py 1541 linii → 4 fișiere |
| Circular import fix | `520a66d` | audit_worker → track_cost lazy import |
| `api/routes/pages/` subpackage | `e37fb45` | pages.py 1422 linii → 6 submodule |
| `api/utils/errors.py` | `e37fb45` | raise_not_found, raise_bad_request, raise_conflict |
| `api/utils/task_runner.py` | `e37fb45` | create_tracked_task GC-safe + timeout |
| audit_worker timeouts + retry | `e37fb45` | per-step timeouts, 3x retry cu backoff pe analysis |
| Crash recovery la startup | `e37fb45` | reset audituri blocate în lifespan |

---

## Comenzi utile

```bash
# Start server
restart_server.bat

# Kill uvicorn stale
taskkill /F /IM uvicorn.exe

# Migrari DB
alembic upgrade head

# Install deps
uv sync
```

---

## Integrări externe active

| Serviciu | Scop |
|---|---|
| Google Search Console API | Date organic search |
| Google Analytics 4 API | Date trafic |
| Google Ads API | Search terms + campanii |
| OpenAI API | Analiză LLM pagini |
| Anthropic API | Analiză LLM (alternativ) |
| Perplexity API | Cercetare competitori |
| api.nrankai.com | Lead audit worker (polls /next, trimite rezultate) |
