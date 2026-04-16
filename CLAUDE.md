# nrankai-tool — Context pentru Claude Code

## Ce este acest proiect
FastAPI web app pentru audit SEO/GEO al website-urilor folosind LLM-uri.
Rulează intern la **app.nrankai.com** — unealtă de uz personal, nu SaaS public.
Suportă ~20 tipuri de audit (SEO, GEO, content quality, GDPR, accessibility, etc.),
keyword research, GSC/GA4 integration, content briefs, PDF reports, schema gen.

**Production URL:** app.nrankai.com  
**GitHub:** cosminstan90/nrankai-tool

---

## Cum se pornește serverul local

```bat
restart_server.bat        # kill uvicorn + restart pe port 8000
```

Dacă serverul e blocat:
```bat
taskkill /F /IM uvicorn.exe
```

Serverul ascultă pe **port 8000**. Logs în `uvicorn.log`.

---

## Structura directoarelor

```
geo_tool/
  api/
    main.py               # FastAPI app, middleware, lifespan, router registration
    limiter.py            # Rate limiter singleton — importă de AICI, nu din main.py
    provider_registry.py  # LLM provider/model registry pentru UI
    middleware/
      auth.py             # BasicAuthMiddleware (global, din .env)
    models/
      _base.py            # SQLAlchemy engine, Base, AsyncSessionLocal, DATABASE_PATH
      database.py         # Re-export toate modelele (backward compat) + init_db()
      audit.py            # Audit, AuditResult, AuditLog, AuditSummary, AuditTemplate, etc.
      analytics.py        # Keywords, GSC, GA4, Ads, Insights models
      content.py          # ContentBrief, Schema, Citations, Gaps, Actions, Fanout models
      infra.py            # Benchmarks, Schedules, GeoMonitor, Costs, Branding models
    routes/               # Un fișier per feature (~30 routere)
      audits.py           # Core audit flow
      results.py          # Audit results + scoring
      pages/              # UI pages (HTML responses)
      fanout.py           # WLA Fan-Out Analyzer
      keyword_research.py # Keyword research + DataForSEO
      gsc/                # Google Search Console integration
      ...
    workers/
      audit_worker.py         # Core audit pipeline (scrape → convert → analyze → score)
      lead_audit_worker.py    # Polleaza nrankai.com pentru free-audit jobs
      fanout_analyzer.py      # Fan-Out query runner
      fanout_tracker_worker.py # Scheduled fan-out tracking
      webhook_sender.py       # n8n webhook notifications
    templates/            # Jinja2 HTML templates
    static/               # CSS, JS, assets
    prompts/              # Prompt files pentru fiecare tip de audit (NU modifica fără plan)
  core/                   # Business logic (engine modules)
  migrations/             # Alembic migrations
  prompts/                # Prompt files root-level (legacy)
  restart_server.bat      # Script restart server local
  .env                    # Variabile de mediu (nu e în git)
```

---

## Reguli de cod obligatorii

### 1. Rate limiting — importă din `api/limiter.py`
```python
# CORECT
from api.limiter import limiter

@router.get("/my-endpoint")
@limiter.limit("10/minute")
async def my_endpoint(request: Request, ...):  # request TREBUIE să fie primul param
    ...

# GREȘIT — circular import
from api.main import limiter
```

### 2. Autentificarea este globală, nu per-endpoint
`BasicAuthMiddleware` din `api/middleware/auth.py` e aplicat global în `main.py`.
Nu adăuga auth per-endpoint — dacă `AUTH_USERNAME`/`AUTH_PASSWORD` sunt setate în `.env`,
**toate** rutele sunt protejate automat (exceptând `/api/health`, `/static/`, `/favicon.ico`).

### 3. Database — async SQLAlchemy
```python
# Dependency în route handlers
from api.models.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

@router.get("/something")
async def handler(db: AsyncSession = Depends(get_db)):
    ...

# În background tasks — NU folosi Depends(get_db), deschide propria sesiune
from api.models._base import AsyncSessionLocal

async def my_background_task():
    async with AsyncSessionLocal() as db:
        ...
```

### 4. Apeluri AI externe — întotdeauna în try/except
```python
import anthropic

try:
    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = await client.messages.create(...)
except anthropic.APIError as e:
    logger.error(f"Anthropic API error: {e}")
    raise HTTPException(status_code=502, detail="AI service unavailable")
except Exception as e:
    logger.error(f"Unexpected error: {e}")
    raise
```
Același pattern pentru OpenAI, Mistral, Google Gemini, Perplexity.

### 5. Modele noi — adaugă în fișierul domeniului corect
Nu adăuga modele direct în `database.py` — ele sunt doar re-exportate de acolo.
- Audit-related → `api/models/audit.py`
- Analytics (keywords, GSC, GA4) → `api/models/analytics.py`
- Content (briefs, schema, fanout) → `api/models/content.py`
- Infrastructure (benchmarks, schedules, costs) → `api/models/infra.py`

### 6. Timestamps
```python
from datetime import datetime, timezone
datetime.now(timezone.utc)  # CORECT
datetime.utcnow()           # GREȘIT — deprecated
```

### 7. Cum se adaugă un router nou
```python
# 1. Creează api/routes/my_feature.py cu:
from fastapi import APIRouter
router = APIRouter(prefix="/api/my-feature", tags=["my-feature"])

# 2. Exportă din api/routes/__init__.py:
from .my_feature import router as my_feature_router

# 3. Înregistrează în api/main.py:
app.include_router(my_feature_router)
```

---

## Variabile de mediu importante

| Variabilă | Ce face |
|-----------|---------|
| `AUTH_USERNAME` / `AUTH_PASSWORD` | Activează BasicAuth global (opțional) |
| `ANTHROPIC_API_KEY` | Claude models (provider principal) |
| `OPENAI_API_KEY` | GPT-4o, GPT-4o-mini |
| `GEMINI_API_KEY` | Google Gemini |
| `MISTRAL_API_KEY` | Mistral models |
| `PERPLEXITY_API_KEY` | Perplexity (folosit în GEO audits) |
| `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` | Keyword research API |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | GSC + GA4 OAuth |
| `NRANKAI_WORKER_KEY` | Activează lead audit worker (nrankai.com integration) |
| `NRANKAI_CLOUD_URL` | URL cloud nrankai (default: nrankai.com) |
| `N8N_WEBHOOK_URL` | Auto-înregistrare webhook n8n la startup |
| `ALLOWED_ORIGINS` | CORS origins (default: app.nrankai.com) |

---

## Ce NU se modifică fără plan explicit

1. **`api/models/database.py`** — doar re-exporturi; nu adăuga modele direct aici
2. **`prompts/`** și **`api/prompts/`** — prompt files pentru audit engine; orice modificare schimbă outputul tuturor auditelor
3. **`core/`** — business logic engine; modificările necesită teste complete
4. **`migrations/`** — migrații Alembic; nu modifica manual, generează cu `alembic revision`
5. **`api/middleware/auth.py`** — auth global; orice bug lasă app-ul neprotejat sau inaccessibil

---

## Integrarea cu nrankai-cloud

`api/workers/lead_audit_worker.py` polleaza `api.nrankai.com/api/lead-audits/next` la fiecare 30 secunde.
- Activat dacă `NRANKAI_WORKER_KEY` e setat în `.env`
- Rulează un GEO audit complet per job, returnează rezultat + email-ready content
- Worker-ul e pornit automat în `lifespan()` din `main.py`
