# CHANGELOG — Website LLM Analyzer v1.1.0

## New Features (February 2026)

### 1. 🔐 Authentication Middleware
- **File:** `api/middleware/auth.py`
- Optional Basic HTTP Authentication using `AUTH_USERNAME` and `AUTH_PASSWORD` from `.env`
- Auto-disabled when credentials are not set (backward compatible)
- Skips auth for health check endpoint and static files
- Uses constant-time comparison to prevent timing attacks
- **How to enable:** Set `AUTH_USERNAME=admin` and `AUTH_PASSWORD=yourpassword` in `.env`

### 2. 📊 Dashboard Charts
- **Files:** `api/routes/compare.py` (API), `api/templates/index.html` (UI)
- Score Distribution doughnut chart (all completed audits)
- Score Trend line chart (recent audit scores over time, color-coded)
- Charts auto-load from `/api/charts/score-distribution` and `/api/charts/score-trend` endpoints
- Only displayed when there are completed audits
- Additional chart endpoints: `/api/charts/audits-by-type`, `/api/charts/websites-overview`

### 3. 🔄 Compare Audits Page
- **Files:** `api/routes/compare.py` (API), `api/templates/compare.html` (UI)
- New `/compare` page in navigation
- Select 2-4 completed audits to compare side by side
- Summary cards with score and mini distribution bars
- Grouped bar chart showing distribution comparison
- Page-by-page score delta table (sorted by biggest difference)
- Supports cross-website and cross-audit-type comparisons
- API endpoint: `GET /api/compare?audit_ids=id1,id2,...`

### 4. 📄 Printable Audit Report
- **Files:** `api/main.py` (route), `api/templates/report.html` (UI)
- New `/audits/{id}/report` page — standalone, print-optimized
- Executive summary with key metrics
- Score distribution visualization (chart + bars)
- Top priority issues section (high priority optimization opportunities)
- Full results table with all pages
- Print/Save as PDF button (uses browser's native print)
- Report buttons added to audit detail and results pages

### 5. 🔁 Re-run Single Page Analysis
- **Files:** `api/routes/compare.py` (API), `api/templates/results.html` (UI)
- New "Re-run" button on each page in the results table
- Endpoint: `POST /api/audits/{audit_id}/rerun/{result_id}`
- Re-analyzes a single page using the same provider/model
- Includes research context if available
- Updates the score in the database and recalculates audit average
- Saves updated JSON output file

### 6. 🎨 Improved Result Details Modal
- **File:** `api/templates/results.html`
- Structured rendering of audit scores (cards with color-coded values)
- Formatted optimization opportunities with priority badges
- Array values rendered as tags
- Collapsible raw JSON section for power users
- Better visual hierarchy and readability

## Files Added
```
api/middleware/__init__.py          — Middleware package
api/middleware/auth.py              — Basic HTTP Auth middleware
api/routes/compare.py              — Compare, charts, and re-run APIs
api/templates/compare.html          — Compare audits page
api/templates/report.html           — Printable audit report
CHANGELOG.md                        — This file
```

## Files Modified
```
api/routes/__init__.py              — Added compare_router export
api/main.py                         — Added auth middleware, compare router, report & compare page routes
api/templates/base.html             — Added "Compare" nav link
api/templates/index.html            — Added dashboard charts section + scripts
api/templates/results.html          — Added re-run button, report button, improved detail modal
api/templates/audit_detail.html     — Added Report button
```

## API Endpoints Added
```
GET  /compare                        → Compare audits page (HTML)
GET  /audits/{id}/report             → Printable report page (HTML)
GET  /api/charts/score-distribution  → Score distribution chart data
GET  /api/charts/score-trend         → Score trend over time
GET  /api/charts/audits-by-type      → Audit counts by type
GET  /api/charts/websites-overview   → Websites with latest scores
GET  /api/compare?audit_ids=...      → Compare audit data
POST /api/audits/{id}/rerun/{rid}    → Re-analyze single page
```

## Migration Notes
- **No database changes** — all new features use existing schema
- **Backward compatible** — auth is disabled by default
- **No new dependencies** — uses existing Chart.js, AlpineJS, htmx
