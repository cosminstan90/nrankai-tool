# GSC Indexer — Claude Code Build Prompts
# Sequential sessions for building the module into WLA (app.nrankai.com)
# Run these in order. Each prompt is a standalone Claude Code session.

---

## PROMPT 01 — DB Schema + Module Scaffold

```
You are building a new module called "GSC Indexer" inside the existing WLA FastAPI project at /opt/wla (or the local repo root).

The project uses:
- FastAPI + Python 3.11
- MariaDB via aiomysql (connection pool at app.state.db_pool)
- Jinja2 templates (base.html exists, extend it)
- Existing module pattern: each module lives in modules/<name>/ with router.py, templates/, __init__.py

Your task:

1. Create the folder structure:
   modules/gsc_indexer/
   ├── __init__.py
   ├── router.py          (empty stub with APIRouter, one GET "/" returning {"status": "ok"})
   ├── gsc_bot.py         (empty stub)
   ├── sitemap.py         (empty stub)
   ├── cli.py             (empty stub)
   └── templates/
       └── indexer.html   (empty stub extending base.html)

2. Create sessions/ directory at project root (add to .gitignore if not already)

3. Run this SQL against the MariaDB database (credentials in .env or config.py):

CREATE TABLE IF NOT EXISTS gsc_indexing_urls (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    client_id       INT DEFAULT NULL,
    url             VARCHAR(2048) NOT NULL,
    property_url    VARCHAR(512)  DEFAULT NULL,
    state           ENUM('queued','submitted','indexed','failed','skipped','already_indexed') DEFAULT 'queued',
    priority        TINYINT DEFAULT 0,
    submitted_at    DATETIME DEFAULT NULL,
    indexed_at      DATETIME DEFAULT NULL,
    last_checked    DATETIME DEFAULT NULL,
    retries         TINYINT DEFAULT 0,
    notes           VARCHAR(512) DEFAULT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_url (url(768))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS gsc_indexing_runs (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    property_url    VARCHAR(512) DEFAULT NULL,
    started_at      DATETIME DEFAULT NULL,
    finished_at     DATETIME DEFAULT NULL,
    submitted       INT DEFAULT 0,
    already_indexed INT DEFAULT 0,
    failed          INT DEFAULT 0,
    skipped         INT DEFAULT 0,
    triggered_by    ENUM('manual','scheduled','api') DEFAULT 'manual',
    notes           TEXT DEFAULT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS gsc_accounts (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    label           VARCHAR(128) NOT NULL,
    google_email    VARCHAR(256) NOT NULL,
    cookies_path    VARCHAR(512) NOT NULL,
    is_active       TINYINT(1) DEFAULT 1,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

4. Mount the router stub in main.py:
   from modules.gsc_indexer.router import router as gsc_router
   app.include_router(gsc_router, prefix="/gsc-indexer", tags=["GSC Indexer"])

5. Verify the app starts without errors (uvicorn main:app --reload).

Do not implement any logic yet. Scaffold only.
```

---

## PROMPT 02 — Sitemap Parser

```
You are working inside the WLA FastAPI project. The module modules/gsc_indexer/ already exists.

Implement modules/gsc_indexer/sitemap.py with the following:

Dependencies to install if missing: httpx, lxml

Function: fetch_urls_from_sitemap(sitemap_url: str, max_urls: int = 5000) -> list[str]

Logic:
- Fetch the sitemap URL with httpx (follow redirects, 20s timeout, User-Agent: "nrankai-indexer/1.0")
- If URL ends with .gz or response has gzip content-encoding, decompress with gzip module
- Parse XML with lxml (recover=True parser)
- If root tag contains "sitemapindex" → it's a sitemap index:
    - Find all <sitemap><loc> children
    - Recursively call _process() on each child sitemap URL
- Otherwise → it's a regular sitemap:
    - Find all <url><loc> elements
    - Append loc.text.strip() to url list
- Namespace: http://www.sitemaps.org/schemas/sitemap/0.9 (fall back to wildcard {*} if no namespace match)
- Deduplicate results preserving order
- Cap at max_urls
- Log warnings on fetch/parse failures (don't raise, just skip)
- Track visited sitemap URLs in a set to avoid infinite loops

Also implement: guess_sitemap_url(site_url: str) -> str
- Parses the domain from site_url and returns https://domain/sitemap.xml

Write a simple __main__ block at the bottom that accepts a sitemap URL as sys.argv[1] and prints found URLs, for quick CLI testing:
  python sitemap.py https://pagani.ro/sitemap.xml

After implementing, test it against https://pagani.ro/sitemap.xml and confirm URL count is printed.
```

---

## PROMPT 03 — Google Session Saver

```
You are working inside modules/gsc_indexer/gsc_bot.py in the WLA project.

Dependencies to install if missing: playwright
Run: playwright install chromium

Implement ONLY the session saving part of gsc_bot.py:

1. async def save_session(cookies_path: str)
   - Launch Chromium NON-headless (headless=False, slow_mo=50)
   - Open a new page and navigate to https://search.google.com/search-console
   - Print clear instructions to the terminal:
       "[GSC Bot] Log in to Google in the browser window."
       "[GSC Bot] Navigate into Search Console so all cookies are set."
       "[GSC Bot] Press ENTER here when done..."
   - Use input() to pause
   - Call ctx.storage_state() to capture cookies + localStorage
   - Write the JSON to cookies_path (create parent dirs if needed)
   - Print confirmation: "[GSC Bot] Session saved to {cookies_path}"
   - Close the browser

2. async def _load_context(playwright_instance, cookies_path: str, headless: bool = True) -> tuple[Browser, BrowserContext]
   - Read the JSON from cookies_path
   - Launch Chromium with headless param
   - Create context with storage_state from the JSON
   - Return (browser, context)

3. Add a __main__ block:
   if sys.argv[1] == "save":
       asyncio.run(save_session(sys.argv[2]))

Test by running:
   python gsc_bot.py save sessions/test_account.json

Confirm the session JSON file is created and contains cookies for google.com.
Do not implement the inspection/indexing logic yet.
```

---

## PROMPT 04 — Playwright GSC Automation Core

```
You are implementing the core automation logic in modules/gsc_indexer/gsc_bot.py.
The save_session() and _load_context() functions already exist.

Add the following, in this order:

Constants:
  GSC_INSPECTION_URL = "https://search.google.com/search-console/inspect"
  IndexingResult = Literal["submitted","already_indexed","rate_limited","not_found","failed","login_required"]

Implement: async def _inspect_url(page: Page, url: str, property_url: str) -> IndexingResult

Logic:
  1. Build target: f"{GSC_INSPECTION_URL}?resource_id={property_url}&id={url}"
  2. page.goto() with wait_until="networkidle", timeout=30000
     - On Timeout: return "failed"
  3. If "accounts.google.com" or "signin" in page.url: return "login_required"
  4. Wait up to 20s for any of these texts to appear in the page:
     "URL is on Google" | "URL is not on Google" | "URL is unknown to Google"
     (use page.wait_for_selector with text= selectors, catch TimeoutError)
  5. Sleep 2s for page to settle
  6. Read full body text with page.inner_text("body")
  7. If "URL is on Google" in text → call _click_request_indexing(page), return its result (submitted or already_indexed)
  8. If "URL is not on Google" or "URL is unknown to Google" → call _click_request_indexing(page)
  9. If "quota" or "limit" in text.lower() → return "rate_limited"
  10. Default: return "failed"

Implement: async def _click_request_indexing(page: Page) -> IndexingResult

Logic:
  1. Try to find button with text "Request indexing" (wait 5s), fall back to "Test Live URL"
  2. If no button found: return "failed"
  3. Click it, sleep 2s
  4. Wait up to 15s for success text: "Indexing requested" | "has been submitted" | "request has been"
     If found: return "submitted"
  5. Try clicking "Got it" / "OK" / "Done" / "Close" buttons if a dialog appeared
  6. Check body for "quota" / "limit" / "try again" → return "rate_limited"
  7. Default: return "submitted" (optimistic if no error detected)

Implement: async def run_batch(urls, property_url, cookies_path, delay_between=8.0, headless=True, on_result=None) -> dict

Logic:
  1. stats dict: submitted/already_indexed/failed/rate_limited/login_required/skipped = 0
  2. _load_context() → browser + ctx
  3. Open page, navigate to GSC home, sleep 2s (warms up cookies)
  4. For each url:
     - Call _inspect_url()
     - Increment stats[result]
     - If on_result: await on_result(url, result, notes_string)
     - If login_required: break
     - If rate_limited: sleep 60s, continue
     - Sleep delay_between between URLs
  5. Close context + browser
  6. Return stats

Use structured logging throughout (logging.getLogger("wla.indexer.bot")).
After implementing, do NOT test against real GSC yet — that's done in Prompt 05.
```

---

## PROMPT 05 — CLI Runner + Live Test

```
You are implementing modules/gsc_indexer/cli.py in the WLA project.
gsc_bot.py and sitemap.py are already complete.

Implement a CLI with argparse, two subcommands:

1. save-session
   --cookies  PATH  (required)
   → calls asyncio.run(save_session(args.cookies))

2. run
   --sitemap     URL   (mutually exclusive with --urls-file)
   --urls-file   PATH  (one URL per line)
   --property    URL   (required, GSC property e.g. https://pagani.ro/)
   --cookies     PATH  (required, saved session JSON)
   --limit       INT   (default 50)
   --max-urls    INT   (default 5000, applies to sitemap)
   --delay       FLOAT (default 8.0 seconds)
   --visible     FLAG  (show browser, for debugging)

   Logic:
   - Load URLs from sitemap or file
   - Slice to --limit
   - results = [] list of dicts {url, result, notes, ts}
   - on_result callback appends to results and prints icon + status:
       ✅ submitted | 🔵 already_indexed | ❌ failed | ⏸️ rate_limited | 🔒 login_required
   - After run_batch(), save CSV to results/run_YYYYMMDD_HHMMSS.csv
   - Print final stats summary

Logging: basicConfig at INFO level with timestamp format.

After implementing, run a LIVE TEST against pagani.ro:

  python cli.py run \
    --sitemap https://pagani.ro/sitemap.xml \
    --property https://pagani.ro/ \
    --cookies sessions/pagani_account.json \
    --limit 5 \
    --visible

Observe the browser, confirm it navigates GSC correctly, and report what state each URL returned.
If selectors fail, adjust them in gsc_bot.py → _inspect_url() or _click_request_indexing().
```

---

## PROMPT 06 — FastAPI Router + DB Layer

```
You are implementing modules/gsc_indexer/router.py in the WLA project.
The DB tables already exist. Use app.state.db_pool (aiomysql) via a get_db dependency.

Implement all DB helper functions first (async, use aiomysql.DictCursor):

- db_upsert_urls(conn, urls, property_url, client_id=None, priority=0)
  INSERT IGNORE / ON DUPLICATE KEY UPDATE priority

- db_get_queued(conn, property_url, limit) → list of {id, url}
  WHERE state IN ('queued','failed') AND retries < 3
  ORDER BY priority DESC, retries ASC, created_at ASC

- db_update_state(conn, url, state, notes=None)
  Handle submitted (set submitted_at), failed (increment retries), others (set last_checked)

- db_get_stats(conn, property_url=None) → dict with total/queued/submitted/indexed/already_indexed/failed/skipped

- db_get_account(conn, account_id) → dict or None

- db_log_run(conn, run_data: dict) → inserted id

Then implement these endpoints:

POST /urls/add          → AddUrlsRequest(urls, property_url, client_id, priority)
POST /urls/import-sitemap → SitemapImportRequest(sitemap_url, property_url, client_id, max_urls=500)
GET  /urls/list         → query params: property_url, state, limit=100, offset=0
GET  /stats             → query param: property_url
POST /run               → RunBatchRequest(property_url, account_id, limit=50, delay_between=8.0)
                          Runs run_batch() as a BackgroundTask
                          on_result callback acquires pool connection and calls db_update_state
                          After batch: calls db_log_run()
GET  /runs              → list last 20 runs from gsc_indexing_runs

GET  /                  → render templates/indexer.html with stats

Use Pydantic v2 models. All endpoints return JSON except GET /.
Verify all endpoints respond correctly with curl or httpx before finishing.
```

---

## PROMPT 07 — Dashboard UI

```
You are implementing modules/gsc_indexer/templates/indexer.html in the WLA project.
Extend base.html. Use only Tailwind CSS classes (already loaded in base).
No external JS libraries. Vanilla JS only.

Build a single-page dashboard with these sections:

1. HEADER ROW
   - Title "GSC Indexer" + subtitle "Automates Search Console Request Indexing"
   - "▶ Run Now" button (top right) — opens Run Settings panel or scrolls to it

2. STATS CARDS (5 cards in a row, responsive grid)
   - Queued (gray), Submitted (blue), Indexed (green), Already Indexed (emerald), Failed (red)
   - Values loaded via fetch("/gsc-indexer/stats") on page load
   - Auto-refresh every 30 seconds

3. IMPORT PANEL (two columns)
   Left: Sitemap import
     - Input: Sitemap URL
     - Input: GSC Property URL
     - Button: "Import from Sitemap" → POST /urls/import-sitemap
     - Show success/error message inline

   Right: Manual URL paste
     - Textarea: one URL per line
     - Input: GSC Property URL
     - Button: "Add to Queue" → POST /urls/add
     - Show success/error message inline

4. RUN SETTINGS PANEL
   - Input: GSC Property URL
   - Input: Account ID (number, default 1)
   - Input: Batch Limit (number, default 50)
   - Button: "▶ Start Run" → POST /run
   - Show "Run started for N URLs" message inline

5. URL TABLE
   - Columns: URL (truncated with title tooltip), State (colored badge), Submitted At, Retries, Notes
   - Filter dropdown: All / Queued / Submitted / Indexed / Already Indexed / Failed
   - Refresh button
   - Data from GET /urls/list, auto-refreshes every 30s
   - State badge colors: queued=gray, submitted=blue, indexed=green, already_indexed=emerald, failed=red

State badge color map as a JS const object.
All fetch calls handle errors and show inline error messages.
After implementing, verify the page loads correctly at /gsc-indexer/ with real data.
```

---

## PROMPT 08 — n8n Scheduling + Navigation Link

```
You are finalizing the GSC Indexer module integration in the WLA project.

Task 1: Add GSC Indexer to the main navigation
- Find where other module links are defined (likely base.html or a nav partial)
- Add: "GSC Indexer" linking to /gsc-indexer/
- Use the same style/icon pattern as existing nav items
- Icon suggestion: 🔍 or a search/index icon

Task 2: Add a /gsc-indexer/run-scheduled endpoint for n8n/cron triggering
- POST /run-scheduled
- Accepts: { "property_url": "...", "account_id": 1, "limit": 50, "secret": "..." }
- Validate secret against env var GSC_SCHEDULER_SECRET (from .env)
- If valid: trigger run_batch() as background task (same logic as /run)
- Return: { "triggered": true, "queued_count": N }
- If secret invalid: 403

Task 3: Create an n8n workflow JSON file at n8n_workflows/gsc_indexer_daily.json
Structure a workflow that:
- Triggers: Schedule node, every day at 06:00
- Step 1: HTTP Request node → POST https://app.nrankai.com/gsc-indexer/run-scheduled
  Body: { "property_url": "https://pagani.ro/", "account_id": 1, "limit": 50, "secret": "{{$env.GSC_SCHEDULER_SECRET}}" }
- Step 2: IF node — check response.triggered == true
- Step 3a (true): Send notification (placeholder Slack/email node with "GSC run triggered for N URLs")
- Step 3b (false): Error notification node

Task 4: Add GSC_SCHEDULER_SECRET to .env.example with a placeholder value

Task 5: Final smoke test
- Start the WLA app
- Confirm /gsc-indexer/ loads with correct stats
- Confirm POST /gsc-indexer/run-scheduled returns 403 with wrong secret
- Confirm POST /gsc-indexer/run-scheduled returns 200 with correct secret and triggers a background run
- Confirm the nav link appears on all pages

Report any issues found.
```
