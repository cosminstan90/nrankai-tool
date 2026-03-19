# geo_tool — Haiku 4.5 Task Guide
**Checked and approved by: Claude Sonnet 4.6**
**Project path:** `D:\Projects\geo_tool`
**Purpose:** Give Haiku 4.5 enough context to implement new features without codebase exploration.

---

## 1. What This Project Is

A FastAPI + SQLite website audit tool. Users submit a website URL → the app scrapes pages → sends each page's text to an LLM for scoring → stores results. A Jinja2 + Alpine.js + Tailwind CSS + Chart.js frontend displays results, trends, briefs, and analytics.

**Stack:**
| Layer | Tech |
|---|---|
| Web framework | FastAPI (async) |
| DB driver | SQLAlchemy async + aiosqlite (SQLite file at `data/analyzer.db`) |
| Templates | Jinja2 (`api/templates/`) |
| Frontend JS | Alpine.js (x-data, x-init, x-model, x-show, x-text) |
| Styling | Tailwind CSS (CDN), color palette: `slate-*`, `sky-*`, `emerald-*`, `amber-*`, `red-*` |
| Charts | Chart.js (CDN) |
| Partial updates | HTMX |
| Auth | Optional HTTP Basic auth middleware |

---

## 2. Critical File Map

```
api/
├── main.py                  ← App entry point. All page routes (@app.get). Import everything here.
├── models/
│   └── database.py          ← ALL SQLAlchemy models + engine + get_db + init_db
├── routes/
│   ├── __init__.py          ← Exports all routers. ADD NEW ROUTERS HERE.
│   ├── audits.py            ← POST/GET /api/audits
│   ├── content_briefs.py    ← /api/briefs (GenerateBriefsRequest etc.)
│   ├── settings.py          ← /api/settings/weights (NEW - just added)
│   ├── costs.py             ← /api/costs pattern to copy for simple CRUD routers
│   └── ...17 other routes
├── templates/
│   ├── base.html            ← Sidebar nav. ADD LINKS HERE for new pages.
│   ├── index.html           ← Dashboard / home page
│   ├── page_view.html       ← Per-URL multi-audit-type view
│   ├── site_health.html     ← Per-domain aggregated view
│   ├── briefs.html          ← Content briefs list + generate panel
│   ├── settings.html        ← Score weights editor (NEW - just added)
│   └── ...22 more templates
├── middleware/
│   └── auth.py
└── provider_registry.py     ← LLM provider/model definitions + helper fns
```

---

## 3. Key Database Models (in `api/models/database.py`)

### Core models you'll query most:

```python
# Audit — one per site-wide audit run
class Audit(Base):
    __tablename__ = "audits"
    id            = Column(String(36), primary_key=True)   # UUID string
    website       = Column(String(255))                     # e.g. "example.com"
    audit_type    = Column(String(50))                      # e.g. "SEO_AUDIT"
    provider      = Column(String(20))                      # "anthropic", "openai", etc.
    model         = Column(String(100))
    status        = Column(String(20))                      # pending|scraping|analyzing|completed|failed
    created_at    = Column(DateTime)
    completed_at  = Column(DateTime, nullable=True)
    pages_analyzed = Column(Integer, default=0)
    average_score  = Column(Float, nullable=True)
    language       = Column(String(30), nullable=True)

# AuditResult — one per page per audit
class AuditResult(Base):
    __tablename__ = "audit_results"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    audit_id      = Column(String(36), ForeignKey("audits.id", ondelete="CASCADE"), index=True)
    page_url      = Column(String(512), index=True)
    score         = Column(Integer, nullable=True)           # 0-100
    classification = Column(String(50), nullable=True)       # "excellent"|"good"|"average"|"weak"|"very_poor"
    result_json   = Column(Text, nullable=True)              # Full LLM JSON output (parse with json.loads)
    created_at    = Column(DateTime)

# AuditWeightConfig — configurable composite weights (NEWLY ADDED)
class AuditWeightConfig(Base):
    __tablename__ = "audit_weight_configs"
    audit_type    = Column(String(50), primary_key=True)
    weight        = Column(Float, nullable=False)
    updated_at    = Column(DateTime)

# ContentBrief — one LLM-generated brief per page
class ContentBrief(Base):
    __tablename__ = "content_briefs"
    id            = Column(Integer, primary_key=True, autoincrement=True)
    audit_id      = Column(String(36))
    page_url      = Column(String(512))
    brief_json    = Column(Text)     # JSON matching content_brief.yaml output schema
    status        = Column(String(20))  # generated|approved|in_progress|completed|failed
    priority      = Column(String(10))  # critical|high|medium|low
    created_at    = Column(DateTime)
```

**No migration tool** — new tables are created automatically at startup by `Base.metadata.create_all(bind=sync_engine)` in `init_db()`. Just add a new class inheriting `Base` and restart.

---

## 4. Adding a New Feature — Step-by-Step Checklist

### A. New API route file (e.g., `api/routes/my_feature.py`)

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from api.models.database import get_db  # always use get_db for request-scoped sessions

router = APIRouter(prefix="/api/my-feature", tags=["my_feature"])

class MyRequest(BaseModel):
    some_field: str

@router.get("")
async def list_things(db: AsyncSession = Depends(get_db)):
    ...

@router.post("")
async def create_thing(req: MyRequest, db: AsyncSession = Depends(get_db)):
    ...
```

### B. Register the router (2 files to update)

**`api/routes/__init__.py`** — append:
```python
from .my_feature import router as my_feature_router
```

**`api/main.py`** — two changes:
```python
# Line 38 import block: add my_feature_router to the from api.routes import ... line
# Line ~181 after the existing include_router calls:
app.include_router(my_feature_router)
```

### C. New DB model (if needed)

Append to `api/models/database.py` **before** the `DEFAULT_TEMPLATES` list:
```python
class MyNewModel(Base):
    __tablename__ = "my_new_table"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    some_field = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
```
Then add `MyNewModel` to the import line in `api/main.py` and any route files that need it.

### D. New page template

Create `api/templates/my_page.html`:
```html
{% extends "base.html" %}
{% block title %}My Page{% endblock %}
{% block content %}
<div class="container mx-auto px-4 py-8 max-w-5xl">
    <h1 class="text-3xl font-bold text-gray-900 mb-2">My Page</h1>
    ...
</div>
{% endblock %}
{% block scripts %}
<script>
// page-specific JS here
</script>
{% endblock %}
```

### E. Add page route in `api/main.py`

```python
@app.get("/my-page", response_class=HTMLResponse)
async def my_page(request: Request, db: AsyncSession = Depends(get_db)):
    """My new page."""
    return templates.TemplateResponse("my_page.html", {
        "request": request,
        # pass data here
    })
```

### F. Add sidebar link in `api/templates/base.html`

Find the appropriate section (TOOLS, ANALYSIS, etc.) and add:
```html
<a href="/my-page"
    class="flex items-center space-x-2.5 px-2 py-1.5 rounded-md text-sm font-medium transition-colors
    {% if p.startswith('/my-page') %}bg-slate-700 text-white{% else %}text-slate-400 hover:bg-slate-800 hover:text-slate-100{% endif %}">
    <span class="text-base leading-none">🔮</span><span>My Page</span>
</a>
```
The variable `p` is `request.url.path` — already available in base.html context.

---

## 5. Alpine.js Patterns Used in This Project

**Component initialization:**
```html
<div x-data="myComponent()" x-init="init()">
    <span x-text="someValue"></span>
    <div x-show="isVisible">...</div>
    <input x-model="inputVal">
    <button @click="doSomething()">Click</button>
</div>

<script>
function myComponent() {
    return {
        someValue: '',
        isVisible: false,
        inputVal: '',

        init() { /* called on mount */ },

        async doSomething() {
            const resp = await fetch('/api/my-feature', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ some_field: this.inputVal })
            });
            const data = await resp.json();
        }
    };
}
</script>
```

**Toast notification pattern** (copy from `settings.html`):
```html
<div x-show="toast.visible"
     x-transition:enter="transition ease-out duration-200"
     x-transition:enter-start="opacity-0 translate-y-1"
     x-transition:enter-end="opacity-100 translate-y-0"
     class="mt-4 p-3 rounded-lg text-sm font-medium"
     :class="toast.error ? 'bg-red-50 text-red-700 border border-red-200'
                         : 'bg-green-50 text-green-700 border border-green-200'"
     x-text="toast.message">
</div>
```

**Server-rendered data injection** (for initial state from Jinja2):
```html
<!-- In template: -->
<div x-data="myApp()" x-init="init()">

<script>
function myApp() {
    return {
        init() {
            const raw = {{ my_json_var | safe }};  // Jinja2 injects server-rendered JSON
            this.items = raw.items;
        }
    };
}
</script>
```

In the route:
```python
import json as _json
return templates.TemplateResponse("my_page.html", {
    "request": request,
    "my_json_var": _json.dumps({"items": [...]}),
})
```

---

## 6. Chart.js Pattern (Multi-line trend chart)

The standard pattern used in `site_health.html` and `page_view.html`:

```javascript
const palette = [
    'rgba(14,165,233,0.9)',   // sky
    'rgba(16,185,129,0.9)',   // emerald
    'rgba(245,158,11,0.9)',   // amber
    'rgba(239,68,68,0.9)',    // red
    'rgba(168,85,247,0.9)',   // purple
    'rgba(236,72,153,0.9)',   // pink
    'rgba(20,184,166,0.9)',   // teal
    'rgba(234,179,8,0.9)',    // yellow
    'rgba(99,102,241,0.9)',   // indigo
    'rgba(249,115,22,0.9)',   // orange
];

const datasets = entries.map(([key, points], i) => {
    const color = palette[i % palette.length];
    return {
        label: points[0]?.label || key,
        data: sortedDates.map(d => scoreMap[d] ?? null),
        borderColor: color,
        backgroundColor: color.replace('0.9', '0.12'),
        borderWidth: 2,
        pointRadius: 4,
        tension: 0.3,
        spanGaps: false,
    };
});

new Chart(ctx, {
    type: 'line',
    data: { labels: sortedDates, datasets },
    options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
            y: { min: 0, max: 100, grid: { color: 'rgba(0,0,0,0.05)' } },
            x: { grid: { display: false } }
        },
        plugins: { legend: { position: 'bottom' } }
    }
});
```

**Radar chart** (for per-URL score snapshots):
```javascript
new Chart(ctx, {
    type: 'radar',
    data: {
        labels: auditLabels,
        datasets: [{ label: 'Score', data: scores,
            borderColor: 'rgba(14,165,233,0.9)',
            backgroundColor: 'rgba(14,165,233,0.15)',
            pointBackgroundColor: 'rgba(14,165,233,0.9)',
            borderWidth: 2, pointRadius: 4 }]
    },
    options: {
        responsive: true, maintainAspectRatio: false,
        scales: { r: { min: 0, max: 100, ticks: { stepSize: 20 } } }
    }
});
```

---

## 7. SQLAlchemy Async Query Patterns

```python
from sqlalchemy import select, func, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession

# Get all rows
result = await db.execute(select(MyModel))
rows = result.scalars().all()

# Get one or None
result = await db.execute(select(MyModel).where(MyModel.id == some_id))
row = result.scalar_one_or_none()

# Count
result = await db.execute(select(func.count(MyModel.id)).where(...))
count = result.scalar()

# Avg
result = await db.execute(select(func.avg(MyModel.score)).where(...))
avg = result.scalar()

# Insert
new_row = MyModel(field1="val1", field2=42)
db.add(new_row)
await db.commit()
await db.refresh(new_row)  # populates auto-generated fields

# Delete
from sqlalchemy import delete
await db.execute(delete(MyModel).where(MyModel.audit_id == audit_id))
await db.commit()

# Upsert pattern (SQLite):
# delete matching rows, then re-insert
await db.execute(delete(MyModel).where(MyModel.pk == some_pk))
db.add(MyModel(pk=some_pk, ...))
await db.commit()

# Background task sessions (use AsyncSessionLocal, NOT get_db):
from api.models.database import AsyncSessionLocal
async with AsyncSessionLocal() as db:
    result = await db.execute(...)
```

---

## 8. Audit Type Labels & Weights (reference)

```python
# _COMPOSITE_WEIGHTS (api/main.py lines 446-465) — also mirrored in api/routes/settings.py
_COMPOSITE_WEIGHTS = {
    'SEO_AUDIT': 0.20, 'GEO_AUDIT': 0.15, 'CONTENT_QUALITY': 0.12,
    'TECHNICAL_SEO': 0.12, 'UX_CONTENT': 0.10, 'ACCESSIBILITY_AUDIT': 0.08,
    'BRAND_VOICE': 0.07, 'LEGAL_GDPR': 0.06, 'INTERNAL_LINKING': 0.05,
    'READABILITY_AUDIT': 0.05, 'COMPETITOR_ANALYSIS': 0.04,
    'CONTENT_FRESHNESS': 0.04, 'AI_OVERVIEW_OPTIMIZATION': 0.04,
    'SPELLING_GRAMMAR': 0.03, 'TRANSLATION_QUALITY': 0.03,
    'LOCAL_SEO': 0.03, 'SECURITY_CONTENT_AUDIT': 0.03, 'E_COMMERCE': 0.03,
}

# _AUDIT_TYPE_LABELS (api/main.py lines 467-477)
_AUDIT_TYPE_LABELS = {
    'SEO_AUDIT': 'SEO', 'GEO_AUDIT': 'GEO', 'CONTENT_QUALITY': 'Content Quality',
    'TECHNICAL_SEO': 'Technical SEO', 'UX_CONTENT': 'UX Content',
    'ACCESSIBILITY_AUDIT': 'Accessibility', 'BRAND_VOICE': 'Brand Voice',
    'LEGAL_GDPR': 'Legal / GDPR', 'INTERNAL_LINKING': 'Internal Linking',
    'READABILITY_AUDIT': 'Readability', 'COMPETITOR_ANALYSIS': 'Competitors',
    'CONTENT_FRESHNESS': 'Content Freshness', 'AI_OVERVIEW_OPTIMIZATION': 'AI Overview',
    'SPELLING_GRAMMAR': 'Spelling & Grammar', 'TRANSLATION_QUALITY': 'Translation',
    'LOCAL_SEO': 'Local SEO', 'SECURITY_CONTENT_AUDIT': 'Security Content',
    'E_COMMERCE': 'E-Commerce',
}
```

---

## 9. Score Color-Coding Convention (used everywhere)

| Score range | Color class | Label |
|---|---|---|
| ≥ 85 | `emerald` | Excellent |
| 70–84 | `yellow` | Good |
| 50–69 | `orange` | Needs Work |
| < 50 | `red` | Poor |

Jinja2 pattern:
```html
<span class="px-3 py-1 rounded-full text-sm font-medium
    {% if score >= 85 %}bg-emerald-100 text-emerald-800
    {% elif score >= 70 %}bg-yellow-100 text-yellow-800
    {% elif score >= 50 %}bg-orange-100 text-orange-800
    {% else %}bg-red-100 text-red-800{% endif %}">
    {{ score }}
</span>
```

Alpine.js dynamic class pattern:
```html
:class="{
    'bg-emerald-100 text-emerald-800': score >= 85,
    'bg-yellow-100 text-yellow-800': score >= 70 && score < 85,
    'bg-orange-100 text-orange-800': score >= 50 && score < 70,
    'bg-red-100 text-red-800': score < 50
}"
```

---

## 10. Recently Completed Enhancements (DO NOT re-implement)

### Enhancement 1: Per-page score trend (api/main.py + page_view.html)
- `page_view()` now queries last 10 runs per audit_type for the viewed URL
- Passes `history_data: JSON string` to template
- `page_view.html` renders a Chart.js multi-line trend chart (section hidden if < 2 data points)

### Enhancement 2: Configurable composite weights
- New table `audit_weight_configs` (model: `AuditWeightConfig` in database.py)
- New file `api/routes/settings.py` with `GET/PUT/POST /api/settings/weights`
- `_load_weights(db)` async helper in `main.py` (falls back to `_COMPOSITE_WEIGHTS` when table empty)
- `_compute_composite(scored_map, weights=None)` now accepts optional weights override
- `page_view()` and `site_health()` both call `await _load_weights(db)` before computing
- New page `/settings` → `settings.html` (Alpine.js weight editor with save/reset)
- Sidebar link `⚖️ Score Weights` added in base.html under TOOLS

### Enhancement 3: Bulk brief generation with configurable threshold
- `GenerateBriefsRequest` now has `score_threshold: int = 70` with validator
- `select_pages_for_briefs(db, audit_id, page_ids, max_pages, score_threshold)` — threshold replaces hardcoded `< 70` / fill is `< threshold+15`
- `background_generate_briefs()` accepts and passes `score_threshold`
- `briefs.html` has `⚡ Generate Briefs` panel: threshold slider (10–100), max pages input, Generate button, toast feedback

### Enhancement 4: CSV export on Results page
- **New endpoint**: `GET /api/audits/{audit_id}/results/csv` in `api/routes/results.py`
  - Uses Python built-in `csv` + `io.StringIO`; returns `StreamingResponse` with `text/csv`
  - Columns: URL, Filename, Score, Classification — ordered by score descending
  - **CRITICAL route order**: this endpoint is registered BEFORE `/{audit_id}/results/{result_id}` to avoid FastAPI matching "csv" as an integer result_id
- **UI**: "Export CSV" button added to `results.html` header button group (alongside existing "Export Excel")

### Enhancement 5: Results page filtering
- **`audit_results_page` in `api/main.py`** now accepts optional query params:
  - `min_score: Optional[int]`, `max_score: Optional[int]` (ge=0, le=100)
  - `classification: Optional[str]` (e.g. "good", "needs_work")
  - `url_search: Optional[str]` (SQL ILIKE `%value%` on page_url)
- Filters applied to both result query and count query
- `filter_qs` string built from active filters and passed to template for pagination link preservation
- **UI**: Filter bar added in `results.html` (between score distribution chart and results table):
  - URL text input, min/max number inputs, classification dropdown
  - Filter submit button + conditional Clear button (shown when any filter is active)
- Pagination links now use `?{{ filter_qs }}page=N` to preserve filters across pages

### Enhancement 6: Score distribution bar chart on Results page
- `audit_results_page` now queries score distribution (group by score, bucket into 0-49/50-69/70-84/85-100)
- `score_distribution` dict and `filter_qs` passed to template
- `results.html` adds a Chart.js bar chart card ABOVE the filter bar:
  - 4 bars: Poor (red), Needs Work (yellow), Good (emerald), Excellent (green)
  - Legend shows exact page count per bucket
  - Chart only rendered when `score_distribution` has data

### Enhancement 7a: Site Health CSV export
- **New route** in `api/main.py`: `GET /sites/{website:path}/export/csv`
  - Queries same data as `site_health()` — latest audit per type, avg score per type
  - Returns CSV with columns: Audit Type, Label, Avg Score, Pages Analyzed, Weight %, Completed, Provider, Model
- **UI**: "Export CSV" button added to `site_health.html` Actions section

### Enhancement 7b: Site Health "Worst Pages" drill-down
- **`site_health()` in `api/main.py`** now also queries bottom 5 pages per audit type:
  - `worst_pages_by_type: dict[audit_type, list[{url, score, classification, audit_id}]]`
  - Passed to template as `worst_pages_by_type`
- **`site_health.html`** adds a new "Lowest Scoring Pages by Audit Type" grid section:
  - One card per audit type (only if data exists)
  - Each card shows audit type label, "View all" link (pre-filtered to max_score=69), and list of up to 5 pages with score badge + link to results filtered by URL

### Enhancement 7c: Gap Analysis — Chart.js category chart + copy-to-clipboard
- **`gap_analysis.html`** Category Summary section: added `<canvas id="categoryChart">` above the text bars
- Alpine.js component additions:
  - `_categoryChart: null` — stores Chart.js instance reference (destroyed before re-creating)
  - `renderCategoryChart(gaps)` — builds grouped bar chart (You vs Best Competitor) from `getCategorySummary()` output; called in `viewGap()` via `this.$nextTick(...)`
  - `copyRec(rec)` — copies single recommendation to clipboard (action + impact + effort + time); uses `navigator.clipboard.writeText` with `execCommand` fallback
  - `copyAllRecs()` — copies all recommendations (all 3 sections) as formatted plaintext; "Copy All" label briefly changes to "Copied!" for 2 seconds
  - `copyAllLabel: 'Copy All'` — reactive label for the copy-all button
- **UI**: Each recommendation card gets a hover-visible copy icon button (top-right); Recommendations panel header gets a "Copy All" button

---

## 11. API Response Convention

**Success:**
```python
return {"success": True, "data": ..., "message": "..."}
```

**Error (let FastAPI handle it):**
```python
raise HTTPException(status_code=404, detail="Not found")
raise HTTPException(status_code=400, detail="Validation message")
raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
```

**Pagination pattern** (from `results.py`):
```python
page: int = Query(1, ge=1)
page_size: int = 50
offset = (page - 1) * page_size
result = await db.execute(stmt.offset(offset).limit(page_size))
```

---

## 12. Common Gotchas

1. **Never use `get_db` in background tasks** — it is tied to a request context. Use `async with AsyncSessionLocal() as db:` instead.

2. **`_compute_composite()` is in `api/main.py`** (not a route file). It's a module-level function. To use weights from DB you must pass the result of `await _load_weights(db)` as the second arg.

3. **`result_json` is a TEXT column** — always parse with `json.loads(ar.result_json)` before use, and guard with `try/except`.

4. **`Audit.audit_type.startswith('SINGLE_')`** — single-page audits use this prefix. Exclude them from bulk stats with `~Audit.audit_type.startswith('SINGLE_')`.

5. **SQLite date ordering** — always use `desc(Audit.created_at)` or `desc(Audit.completed_at)` for "most recent first".

6. **Template `{% block scripts %}`** — Chart.js canvas must be in the DOM before the script runs. All page-specific JS goes inside this block, which renders after the HTML.

7. **`select` is imported from sqlalchemy in main.py** — already imported at line 41: `from sqlalchemy import select, func, desc`. Don't re-import in page routes; they share the module scope.

8. **`import json as _json`** — used inside page route functions (not module level) to avoid shadowing the stdlib `json`. Keep this pattern consistent.

9. **`AuditWeightConfig` must be imported in `api/main.py`** — it was added to the imports on line 37 in the last session.

10. **Sidebar link active state** — uses `p.startswith('/your-path')`, where `p` is provided by `base.html` as `{{ request.url.path }}`. Ensure your route path starts with a unique prefix.

---

## 13. Background Tasks Pattern

```python
from fastapi import BackgroundTasks
from api.models.database import AsyncSessionLocal

# In route:
@router.post("/start")
async def start_job(req: MyRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(
        my_background_fn,
        param1=req.value1,
        param2=req.value2,
    )
    return {"status": "started"}

# Background function (cannot use request-scoped get_db):
async def my_background_fn(param1: str, param2: int):
    async with AsyncSessionLocal() as db:
        # do work
        await db.commit()
```

---

## 14. Key Import Lines in main.py (for reference when adding features)

```python
# Line 37 — database models import (extend this when adding new models):
from api.models.database import init_db, get_db, Audit, AuditResult, AuditSummary,
    BenchmarkProject, ScheduledAudit, GeoMonitorProject, GeoMonitorScan,
    ContentBrief, CrossReferenceJob, AuditWeightConfig

# Line 38 — router imports (extend this when adding new routers):
from api.routes import audits_router, results_router, ..., settings_router

# Line 41:
from sqlalchemy import select, func, desc

# Line 42:
from sqlalchemy.ext.asyncio import AsyncSession

# Line 16 — json module (import as _json inside functions, not here):
import json
```

---

## 15. Running / Restarting the Server

The server runs with uvicorn. After making Python changes, it will hot-reload automatically if running in dev mode. After making template changes, a browser refresh is sufficient (templates are rendered server-side per request). After adding a new DB model, the `init_db()` call on startup creates the table automatically — no manual migration needed.

To verify Python syntax before trusting a change works:
```bash
cd D:/Projects/geo_tool
python -c "import ast, pathlib; ast.parse(pathlib.Path('api/main.py').read_text(encoding='utf-8')); print('OK')"
```
