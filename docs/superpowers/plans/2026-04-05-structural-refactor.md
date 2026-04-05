# Structural Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the geo_tool codebase from a flat root layout with sys.path hacks into a clean layered package structure with `core/` (analysis engine), `api/` (web layer), and a lean `main.py`.

**Architecture:** Root-level business logic modules move into a `core/` package, eliminating all `sys.path.insert` hacks. `api/main.py` page-render routes move to `api/routes/pages.py`, reducing it to app setup only. `api/models/database.py` splits into domain model files with a backward-compatible re-exporter.

**Tech Stack:** Python, FastAPI, SQLAlchemy async, Alembic, git mv

---

## Overview of all file changes

```
DELETED (temp/debug):
  debug_briefs.py, debug_briefs2.py, debug_gsc.py
  tmp_check.py, tmp_scrape_test.py, test_insert.py

MOVED (docs cleanup):
  CHANGELOG*.md, README_*.md, IMPLEMENTATION_*.md → docs/changelogs/ and docs/

CREATED:
  core/__init__.py
  core/audit_builder.py         (was audit_builder.py)
  core/compare_audits.py        (was compare_audits.py)
  core/config.py                (was config.py)
  core/content_chunker.py       (was content_chunker.py)
  core/cross_reference_analyzer.py (was cross_reference_analyzer.py)
  core/determine_score.py       (was determine_score.py)
  core/direct_analyzer.py       (was direct_analyzer.py)
  core/generate_dashboard.py    (was generate_dashboard.py)
  core/generate_report.py       (was generate_report.py)
  core/history_tracker.py       (was history_tracker.py)
  core/html2llm_converter.py    (was html2llm_converter.py)
  core/logger.py                (was logger.py)
  core/monitor_completion_LLM_batch.py (was monitor_completion_LLM_batch.py)
  core/perplexity_researcher.py (was perplexity_researcher.py)
  core/prompt_loader.py         (was prompt_loader.py)
  core/scrape_state.py          (was scrape_state.py)
  core/validate_audit.py        (was validate_audit.py)
  core/web_scraper.py           (was web_scraper.py)
  core/website_llm_analyzer.py  (was website_llm_analyzer.py)

  api/routes/pages.py           (extracted from api/main.py)

  api/models/audit.py           (split from database.py)
  api/models/analytics.py       (split from database.py)
  api/models/content.py         (split from database.py)
  api/models/infra.py           (split from database.py)

MODIFIED:
  main.py                       (root CLI — update imports to core.*)
  api/main.py                   (remove page routes, add pages_router)
  api/routes/__init__.py        (add pages_router export)
  api/routes/audits.py          (sys.path → core.*)
  api/routes/compare.py         (sys.path → core.*)
  api/routes/cross_reference.py (sys.path → core.*)
  api/routes/gsc.py             (sys.path → core.*)
  api/routes/health.py          (sys.path → core.*)
  api/workers/audit_worker.py   (sys.path → core.*)
  api/workers/lead_audit_worker.py (sys.path → core.*)
  api/models/database.py        (becomes re-exporter: from .audit import * etc.)
```

---

## Task 1: Delete temp/debug files

**Files:**
- Delete: `debug_briefs.py`, `debug_briefs2.py`, `debug_gsc.py`
- Delete: `tmp_check.py`, `tmp_scrape_test.py`, `test_insert.py`

- [ ] **Step 1: Delete files**

```bash
cd D:/Projects/geo_tool
git rm debug_briefs.py debug_briefs2.py debug_gsc.py tmp_check.py tmp_scrape_test.py test_insert.py
```

- [ ] **Step 2: Commit**

```bash
git commit -m "chore: delete temp and debug files"
```

---

## Task 2: Consolidate documentation files

**Files:**
- Create: `docs/changelogs/` directory
- Move: All `CHANGELOG_*.md` files → `docs/changelogs/`
- Move: `README_*.md`, `IMPLEMENTATION_*.md`, `IMPLEMENTATION_SUMMARY*.md` → `docs/`
- Keep at root: `README.md`, `DEPLOY.md`, `QUICK_START.md`

- [ ] **Step 1: Create docs dirs and move files**

```bash
cd D:/Projects/geo_tool
mkdir -p docs/changelogs

git mv CHANGELOG_v1.2.0.md docs/changelogs/
git mv CHANGELOG_v1_3_0_BENCHMARKING.md docs/changelogs/
git mv CHANGELOG_v1_4_0_SCHEDULED_AUDITS.md docs/changelogs/
git mv CHANGELOG_v1_5_0_GEO_MONITOR.md docs/changelogs/
git mv CHANGELOG_v2_0_0_CONTENT_BRIEF_GENERATOR.md docs/changelogs/
git mv CHANGELOG_v2_1_0_WHITE_LABEL_PDF.md docs/changelogs/
git mv CHANGELOG.md docs/changelogs/CHANGELOG_latest.md

git mv README_v1_3_0.md docs/
git mv README_v1_4_0.md docs/
git mv README_MASTER.md docs/
git mv GEO_MONITOR_README.md docs/
git mv IMPLEMENTATION_GUIDE_v1_3_0_BENCHMARKING.md docs/
git mv IMPLEMENTATION_GUIDE_v1_4_0_SCHEDULED_AUDITS.md docs/
git mv IMPLEMENTATION_SUMMARY.md docs/
git mv IMPLEMENTATION_SUMMARY_v2_1_0.md docs/
git mv WHITE_LABEL_PDF_GUIDE.md docs/
git mv HARDENING_NOTES_MASTER_V1.md docs/
git mv REFACTORING_NOTES.md docs/
git mv REFACTORING_SUMMARY.md docs/
git mv HANDOVER.md docs/
git mv CLI_USAGE_GUIDE.md docs/
git mv QUICK_START_GEO_MONITOR.md docs/
git mv LOGGING_REFACTOR_CHANGELOG.md docs/changelogs/
```

- [ ] **Step 2: Commit**

```bash
git commit -m "chore: consolidate docs into docs/ and docs/changelogs/"
```

---

## Task 3: Create `core/` package — move files

This is the core structural change. Root-level business logic becomes a proper package.

**Files:**
- Create: `core/__init__.py`
- `git mv` each root module into `core/`

- [ ] **Step 1: Create package and move files**

```bash
cd D:/Projects/geo_tool
mkdir core

git mv audit_builder.py core/
git mv compare_audits.py core/
git mv config.py core/
git mv content_chunker.py core/
git mv cross_reference_analyzer.py core/
git mv determine_score.py core/
git mv direct_analyzer.py core/
git mv generate_dashboard.py core/
git mv generate_report.py core/
git mv history_tracker.py core/
git mv html2llm_converter.py core/
git mv logger.py core/
git mv monitor_completion_LLM_batch.py core/
git mv perplexity_researcher.py core/
git mv prompt_loader.py core/
git mv scrape_state.py core/
git mv validate_audit.py core/
git mv web_scraper.py core/
git mv website_llm_analyzer.py core/
```

- [ ] **Step 2: Create `core/__init__.py`**

```python
"""Core analysis engine for geo_tool."""
```

- [ ] **Step 3: Commit the moves (before editing)**

```bash
git add core/__init__.py
git commit -m "refactor: move root business logic into core/ package"
```

---

## Task 4: Fix intra-core imports

Each file in `core/` imports other files that were also at root. These are now `core.*`.

**Files:**
- Modify: `core/direct_analyzer.py`
- Modify: `core/content_chunker.py`
- Modify: `core/audit_builder.py`
- Modify: `core/html2llm_converter.py`
- Modify: `core/website_llm_analyzer.py`
- Modify: `core/monitor_completion_LLM_batch.py`
- Modify: `core/validate_audit.py`
- Modify: `core/web_scraper.py`
- Modify: `core/compare_audits.py`
- Modify: `core/cross_reference_analyzer.py`
- Modify: `core/perplexity_researcher.py`
- Modify: `core/generate_dashboard.py`
- Modify: `core/generate_report.py`

- [ ] **Step 1: Fix `core/direct_analyzer.py` imports**

Find lines:
```python
import config
from prompt_loader import load_prompt, is_custom_audit, get_audit_definition
from logger import get_logger, setup_logging
from content_chunker import ContentChunker, ChunkMetadata, AuditResultMerger
```

Replace with:
```python
from core import config
from core.prompt_loader import load_prompt, is_custom_audit, get_audit_definition
from core.logger import get_logger, setup_logging
from core.content_chunker import ContentChunker, ChunkMetadata, AuditResultMerger
```

- [ ] **Step 2: Fix `core/html2llm_converter.py` imports**

Find:
```python
import config
```
Replace with:
```python
from core import config
```

- [ ] **Step 3: Fix `core/website_llm_analyzer.py` imports**

Find:
```python
import config
```
Replace with:
```python
from core import config
```

- [ ] **Step 4: Fix `core/web_scraper.py` imports**

Find:
```python
import config
```
Replace with:
```python
from core import config
```

- [ ] **Step 5: Fix `core/monitor_completion_LLM_batch.py` imports**

Find:
```python
from config import (
```
Replace with:
```python
from core.config import (
```

- [ ] **Step 6: Fix `core/audit_builder.py` imports**

Find:
```python
from logger import get_logger
```
Replace with:
```python
from core.logger import get_logger
```

- [ ] **Step 7: Fix `core/content_chunker.py` imports**

Find any `from logger import` or `import logger`:
```python
from core.logger import get_logger
```

- [ ] **Step 8: Fix `core/validate_audit.py` imports**

Find:
```python
import config  # (lazy import inside function)
```
The lazy import inside the function body needs to become:
```python
from core import config
```

- [ ] **Step 9: Fix remaining core files**

Run this to find any remaining bare root imports inside core/:
```bash
grep -rn "^import config\|^from config\|^import logger\|^from logger\|^import prompt_loader\|^from prompt_loader\|^import content_chunker\|^from content_chunker\|^import web_scraper\|^from web_scraper\|^import html2llm\|^from html2llm\|^import website_llm\|^from website_llm\|^import monitor_completion\|^from monitor_completion\|^import audit_builder\|^from audit_builder\|^import validate_audit\|^from validate_audit\|^import determine_score\|^from determine_score\|^import history_tracker\|^from history_tracker\|^import scrape_state\|^from scrape_state\|^import perplexity\|^from perplexity\|^import cross_reference\|^from cross_reference\|^import compare_audits\|^from compare_audits\|^import generate_\|^from generate_" D:/Projects/geo_tool/core/ --include="*.py"
```

Fix any hits with the `core.` prefix pattern.

- [ ] **Step 10: Commit**

```bash
git add core/
git commit -m "refactor: fix intra-core imports after package move"
```

---

## Task 5: Fix root `main.py` (CLI) imports

The CLI orchestrator at root needs to use `core.*` imports.

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Update imports in `main.py`**

Find:
```python
import config
import web_scraper
import html2llm_converter
import website_llm_analyzer
import determine_score
import cross_reference_analyzer
```

Replace with:
```python
from core import config
from core import web_scraper
from core import html2llm_converter
from core import website_llm_analyzer
from core import determine_score
from core import cross_reference_analyzer
```

- [ ] **Step 2: Commit**

```bash
git add main.py
git commit -m "refactor: update CLI main.py to use core.* imports"
```

---

## Task 6: Fix `api/` imports — remove sys.path hacks

Replace every `sys.path.insert` + bare root import in the API layer with `core.*`.

**Files:**
- Modify: `api/main.py`
- Modify: `api/routes/audits.py`
- Modify: `api/routes/compare.py`
- Modify: `api/routes/cross_reference.py`
- Modify: `api/routes/gsc.py`
- Modify: `api/routes/health.py`
- Modify: `api/workers/audit_worker.py`
- Modify: `api/workers/lead_audit_worker.py`

- [ ] **Step 1: Fix `api/main.py`**

Remove:
```python
parent_dir = Path(__file__).parent.parent
sys.path.insert(0, str(parent_dir))
```

Find (line ~424 and ~1045):
```python
from prompt_loader import list_available_audits
```
Replace with:
```python
from core.prompt_loader import list_available_audits
```

- [ ] **Step 2: Fix `api/routes/audits.py`**

Remove:
```python
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
```

Find (lazy imports inside functions):
```python
from prompt_loader import list_available_audits
from prompt_loader import list_available_audits, load_prompt
from direct_analyzer import AsyncLLMClient, clean_json_response
```
Replace with:
```python
from core.prompt_loader import list_available_audits
from core.prompt_loader import list_available_audits, load_prompt
from core.direct_analyzer import AsyncLLMClient, clean_json_response
```

- [ ] **Step 3: Fix `api/routes/compare.py`**

Find:
```python
from direct_analyzer import DirectAnalyzer
from direct_analyzer import clean_json_response
```
Replace with:
```python
from core.direct_analyzer import DirectAnalyzer
from core.direct_analyzer import clean_json_response
```

- [ ] **Step 4: Fix `api/routes/cross_reference.py`**

Remove:
```python
sys.path.insert(0, root)
```

Find any bare root imports inside the function body and add `core.` prefix.

- [ ] **Step 5: Fix `api/routes/gsc.py`**

Find:
```python
from prompt_loader import load_prompt
```
Replace with:
```python
from core.prompt_loader import load_prompt
```

- [ ] **Step 6: Fix `api/routes/health.py`**

Find:
```python
from prompt_loader import list_available_audits
```
Replace with:
```python
from core.prompt_loader import list_available_audits
```

- [ ] **Step 7: Fix `api/workers/audit_worker.py`**

Remove:
```python
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
```

Find (lazy imports inside function bodies):
```python
import web_scraper
from direct_analyzer import run_direct_analysis
from perplexity_researcher import PerplexityResearcher
import html2llm_converter
import website_llm_analyzer
from monitor_completion_LLM_batch import monitor_job
import config
```
Replace with:
```python
from core import web_scraper
from core.direct_analyzer import run_direct_analysis
from core.perplexity_researcher import PerplexityResearcher
from core import html2llm_converter
from core import website_llm_analyzer
from core.monitor_completion_LLM_batch import monitor_job
from core import config
```

- [ ] **Step 8: Fix `api/workers/lead_audit_worker.py`**

Remove:
```python
sys.path.insert(0, str(_root))
```

Find:
```python
from prompt_loader import load_prompt
from direct_analyzer import AsyncLLMClient, clean_json_response
```
Replace with:
```python
from core.prompt_loader import load_prompt
from core.direct_analyzer import AsyncLLMClient, clean_json_response
```

- [ ] **Step 9: Verify no sys.path hacks remain**

```bash
grep -rn "sys.path.insert\|sys.path.append" D:/Projects/geo_tool/api/ --include="*.py"
```
Expected: no output.

- [ ] **Step 10: Verify no bare root imports remain in api/**

```bash
grep -rn "^    from prompt_loader\|^    from direct_analyzer\|^    import web_scraper\|^    from perplexity\|^    import html2llm\|^    import website_llm\|^    from monitor_completion\|^    import config" D:/Projects/geo_tool/api/ --include="*.py"
```
Expected: no output.

- [ ] **Step 11: Start server and verify it boots**

```bash
cd D:/Projects/geo_tool
taskkill /F /IM uvicorn.exe 2>nul; python -m uvicorn api.main:app --port 8000 --timeout-keep-alive 5 2>&1 | head -20
```
Expected: `Application startup complete.`

- [ ] **Step 12: Commit**

```bash
git add api/
git commit -m "refactor: remove sys.path hacks in api/, import from core.* instead"
```

---

## Task 7: Extract page routes from `api/main.py`

`api/main.py` is 1710 lines because it contains all HTML-rendering view functions. These move to `api/routes/pages.py`.

**Files:**
- Create: `api/routes/pages.py`
- Modify: `api/main.py` (remove view functions, add `include_router`)
- Modify: `api/routes/__init__.py` (export `pages_router`)

- [ ] **Step 1: Identify what stays in `main.py`**

`api/main.py` should keep only:
- Module-level imports and env loading
- `lifespan` function (startup/shutdown)
- `app = FastAPI(...)` instantiation
- Middleware setup
- `app.include_router(...)` calls
- `app.exception_handler` and `app.mount`
- Helper functions `_load_weights`, `_compute_composite` (move these to `pages.py` too since only page views use them)

Everything decorated with `@app.get(..., response_class=HTMLResponse)` moves to `pages.py` as `@router.get(...)`.

- [ ] **Step 2: Create `api/routes/pages.py`**

```python
"""HTML page-rendering routes (view layer)."""

import os
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc, case
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import (
    Audit, AuditLog, AuditResult, AuditSummary, BenchmarkProject,
    ScheduledAudit, GeoMonitorProject, ContentBrief, CrossReferenceJob,
    AuditWeightConfig, CostRecord, KeywordSession, GscProperty, Ga4Property,
    AdsAccount, AsyncSessionLocal,
)
from api.models.database import get_db
from api.provider_registry import get_providers_for_ui, get_tier_presets
from core.prompt_loader import list_available_audits

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _load_weights(db: AsyncSession) -> dict:
    # (copy _load_weights body from main.py here)
    ...


def _compute_composite(scored_map: dict, weights: Optional[dict] = None) -> Optional[int]:
    # (copy _compute_composite body from main.py here)
    ...


# (paste all @app.get(..., response_class=HTMLResponse) functions here,
#  changing @app.get to @router.get)
```

- [ ] **Step 3: Cut view functions from `api/main.py`**

Move all functions decorated with `@app.get(..., response_class=HTMLResponse)` (and the helper functions `_load_weights`, `_compute_composite`) from `api/main.py` to `api/routes/pages.py`.

Also move:
```python
@app.get("/presentation", response_class=HTMLResponse)
@app.get("/sites/{website:path}/export/csv")  # returns FileResponse, also a view
```

After removal, `api/main.py` should be ~250 lines.

- [ ] **Step 4: Register the router in `api/main.py`**

In the section where all other `include_router` calls are made, add:
```python
from api.routes.pages import router as pages_router
app.include_router(pages_router)
```

Or add to `api/routes/__init__.py`:
```python
from .pages import router as pages_router
```
And import `pages_router` in `api/main.py` along with the others.

- [ ] **Step 5: Update `api/routes/__init__.py`**

Add:
```python
from .pages import router as pages_router
```
And add `pages_router` to the `__all__` list.

- [ ] **Step 6: Verify server boots and homepage loads**

```bash
cd D:/Projects/geo_tool
taskkill /F /IM uvicorn.exe 2>nul; python -m uvicorn api.main:app --port 8000 2>&1 | head -20
```
Then hit `http://localhost:8000/` and `http://localhost:8000/gsc` — expect 200 HTML responses.

- [ ] **Step 7: Commit**

```bash
git add api/main.py api/routes/pages.py api/routes/__init__.py
git commit -m "refactor: extract HTML page routes from main.py into routes/pages.py"
```

---

## Task 8: Split `api/models/database.py`

1592 lines, 38 classes. Split into 4 domain files. Keep `database.py` as a backward-compatible re-exporter so no other imports break.

**Files:**
- Create: `api/models/audit.py`
- Create: `api/models/analytics.py`
- Create: `api/models/content.py`
- Create: `api/models/infra.py`
- Modify: `api/models/database.py` (becomes re-exporter + DB setup)

**Class assignment:**

| File | Classes |
|------|---------|
| `audit.py` | Audit, AuditResult, AuditLog, AuditSummary, AuditTemplate, AuditWeightConfig, ResultNote |
| `analytics.py` | KeywordSession, KeywordResult, GscProperty, GscQueryRow, GscPageRow, Ga4Property, Ga4PageRow, Ga4ChannelRow, AdsAccount, AdsSearchTermRow, AdsCampaignRow, InsightRun, InsightCard, GoogleOAuthToken |
| `content.py` | ContentBrief, SchemaMarkup, CitationTracker, CitationScan, CompetitorGapAnalysis, ContentGap, ActionCard, CrossReferenceJob, UrlGuide, LlmsTxtJob |
| `infra.py` | BenchmarkProject, ScheduledAudit, GeoMonitorProject, GeoMonitorScan, TrackingProject, TrackingSnapshot, CostRecord, ClientBilling, BrandingConfig |

- [ ] **Step 1: Read `api/models/database.py` lines 1-55 to get the Base and engine setup**

```bash
head -55 D:/Projects/geo_tool/api/models/database.py
```

Note the `Base`, `engine`, `AsyncSessionLocal`, `get_db`, `init_db` definitions — these stay in `database.py`.

- [ ] **Step 2: Create `api/models/audit.py`**

```python
"""Audit-related ORM models."""
from api.models.database import Base
# (cut Audit, AuditResult, AuditLog, AuditSummary, AuditTemplate,
#  AuditWeightConfig, ResultNote class definitions here)
```

- [ ] **Step 3: Create `api/models/analytics.py`**

```python
"""Analytics ORM models (GSC, GA4, Ads, Keywords, Insights)."""
from api.models.database import Base
# (cut KeywordSession, KeywordResult, GscProperty, GscQueryRow, GscPageRow,
#  Ga4Property, Ga4PageRow, Ga4ChannelRow, AdsAccount, AdsSearchTermRow,
#  AdsCampaignRow, InsightRun, InsightCard, GoogleOAuthToken here)
```

- [ ] **Step 4: Create `api/models/content.py`**

```python
"""Content-related ORM models."""
from api.models.database import Base
# (cut ContentBrief, SchemaMarkup, CitationTracker, CitationScan,
#  CompetitorGapAnalysis, ContentGap, ActionCard, CrossReferenceJob,
#  UrlGuide, LlmsTxtJob here)
```

- [ ] **Step 5: Create `api/models/infra.py`**

```python
"""Infrastructure ORM models (benchmarks, schedules, monitoring, costs)."""
from api.models.database import Base
# (cut BenchmarkProject, ScheduledAudit, GeoMonitorProject, GeoMonitorScan,
#  TrackingProject, TrackingSnapshot, CostRecord, ClientBilling, BrandingConfig here)
```

- [ ] **Step 6: Trim `database.py` and add re-exports**

After extracting class definitions, `database.py` keeps:
- `Base`, `engine`, `AsyncSessionLocal`, `get_db`, `init_db`
- All `import` statements needed for setup

Add at the bottom of `database.py`:
```python
# Backward-compatible re-exports — all models remain importable from here
from api.models.audit import (
    Audit, AuditResult, AuditLog, AuditSummary, AuditTemplate,
    AuditWeightConfig, ResultNote,
)
from api.models.analytics import (
    KeywordSession, KeywordResult, GscProperty, GscQueryRow, GscPageRow,
    Ga4Property, Ga4PageRow, Ga4ChannelRow, AdsAccount, AdsSearchTermRow,
    AdsCampaignRow, InsightRun, InsightCard, GoogleOAuthToken,
)
from api.models.content import (
    ContentBrief, SchemaMarkup, CitationTracker, CitationScan,
    CompetitorGapAnalysis, ContentGap, ActionCard, CrossReferenceJob,
    UrlGuide, LlmsTxtJob,
)
from api.models.infra import (
    BenchmarkProject, ScheduledAudit, GeoMonitorProject, GeoMonitorScan,
    TrackingProject, TrackingSnapshot, CostRecord, ClientBilling, BrandingConfig,
)
```

- [ ] **Step 7: Verify server boots without import errors**

```bash
cd D:/Projects/geo_tool
python -c "from api.models.database import Audit, GscProperty, ContentBrief, BenchmarkProject; print('OK')"
```
Expected: `OK`

- [ ] **Step 8: Commit**

```bash
git add api/models/
git commit -m "refactor: split database.py into domain model files (audit, analytics, content, infra)"
```

---

## Task 9: Split `api/routes/gsc.py` into a subpackage

1541 lines. Split into property management, query/page data, and page optimizer sections.

**Files:**
- Create: `api/routes/gsc/__init__.py`
- Create: `api/routes/gsc/properties.py` (connect/disconnect, property list, import queries/pages)
- Create: `api/routes/gsc/data.py` (query rows, page rows, CSV exports, chart data)
- Create: `api/routes/gsc/optimizer.py` (page optimizer LLM analysis, FAQ audit, Schema builder)
- Delete: `api/routes/gsc.py`
- Modify: `api/routes/__init__.py` (import from `gsc` package, not `gsc.py`)

- [ ] **Step 1: Read section boundaries in `gsc.py`**

```bash
grep -n "^@router\." D:/Projects/geo_tool/api/routes/gsc.py | head -40
```

Use output to identify which endpoint groups belong to which file.

- [ ] **Step 2: Create `api/routes/gsc/` directory structure**

```bash
mkdir D:/Projects/geo_tool/api/routes/gsc
```

- [ ] **Step 3: Create `api/routes/gsc/properties.py`**

Contains: GSC OAuth connect/callback, property CRUD, query/page data import endpoints.

```python
"""GSC property management and data import."""
from fastapi import APIRouter
# ... (imports from gsc.py header)

router = APIRouter(prefix="/api/gsc", tags=["gsc"])

# paste property-related @router routes here
```

- [ ] **Step 4: Create `api/routes/gsc/data.py`**

Contains: query rows, page rows, aggregated chart data, CSV export endpoints.

```python
"""GSC query and page data endpoints."""
from fastapi import APIRouter
# ...

router = APIRouter(prefix="/api/gsc", tags=["gsc"])

# paste data endpoints here
```

- [ ] **Step 5: Create `api/routes/gsc/optimizer.py`**

Contains: page optimizer LLM analysis, FAQ audit (`/audit`), Keywords audit, Schema.org builder.

```python
"""GSC page optimizer — LLM-powered content analysis."""
from fastapi import APIRouter
# ...

router = APIRouter(prefix="/api/gsc", tags=["gsc"])

# paste optimizer endpoints here
```

- [ ] **Step 6: Create `api/routes/gsc/__init__.py`**

Merge the three sub-routers into one exported `router`:

```python
"""GSC routes package."""
from fastapi import APIRouter
from .properties import router as properties_router
from .data import router as data_router
from .optimizer import router as optimizer_router

router = APIRouter()
router.include_router(properties_router)
router.include_router(data_router)
router.include_router(optimizer_router)

__all__ = ["router"]
```

- [ ] **Step 7: Delete old `gsc.py`**

```bash
git rm D:/Projects/geo_tool/api/routes/gsc.py
git add D:/Projects/geo_tool/api/routes/gsc/
```

- [ ] **Step 8: Verify `api/routes/__init__.py` import still works**

The existing line `from .gsc import router as gsc_router` will now resolve to `api/routes/gsc/__init__.py` — Python package resolution handles this automatically. No change needed.

- [ ] **Step 9: Verify server boots and GSC pages load**

```bash
cd D:/Projects/geo_tool
python -c "from api.routes.gsc import router; print('GSC router OK, routes:', len(router.routes))"
```

- [ ] **Step 10: Commit**

```bash
git add api/routes/gsc/ api/routes/gsc.py
git commit -m "refactor: split gsc.py into gsc/ subpackage (properties, data, optimizer)"
```

---

## Final verification

- [ ] **Start full server and smoke test**

```bash
cd D:/Projects/geo_tool
taskkill /F /IM uvicorn.exe 2>nul
python -m uvicorn api.main:app --port 8000 2>&1 | head -30
```

Expected: `Application startup complete.` with no `ImportError` or `ModuleNotFoundError`.

- [ ] **Check line counts after refactor**

```bash
wc -l D:/Projects/geo_tool/api/main.py
wc -l D:/Projects/geo_tool/api/models/database.py
wc -l D:/Projects/geo_tool/api/routes/gsc/*.py
```

Expected:
- `api/main.py` < 300 lines
- `api/models/database.py` < 200 lines (setup + re-exports only)
- `api/routes/gsc/*.py` each < 600 lines
