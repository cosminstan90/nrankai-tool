# ContentIQ — Claude Code Build Prompts
# Sequential Claude Code sessions for building the ContentIQ module into WLA (app.nrankai.com)
# Test domain throughout: pagani.ro
# GitHub: github.com/cosminstan90/nrankai-tool
# Run prompts in order. Each is a standalone Claude Code session.

---

## PROMPT 01 — Scaffold + MariaDB Schema

```
You are building a new module called "ContentIQ" inside the existing WLA FastAPI project.

The project uses:
- FastAPI + Python 3.11
- MariaDB via aiomysql (pool at app.state.db_pool)
- Jinja2 templates (base.html exists, extend it)
- Existing module pattern: modules/<name>/router.py + templates/ + __init__.py

Task 1 — Create folder structure:

modules/content_iq/
├── __init__.py
├── router.py              (stub: APIRouter, one GET "/" returning {"status":"ok"})
├── crawler.py             (empty stub)
├── ahrefs.py              (empty stub)
├── gsc.py                 (empty stub)
├── engines/
│   ├── __init__.py
│   ├── freshness.py       (empty stub)
│   ├── geo.py             (empty stub)
│   ├── eeat.py            (empty stub)
│   └── seo_health.py      (empty stub)
├── verdict.py             (empty stub)
├── brief.py               (empty stub)
├── export.py              (empty stub)
└── templates/
    ├── contentiq.html     (empty stub extending base.html)
    └── contentiq_demo.html (empty stub extending base.html)

Task 2 — Run this SQL against MariaDB:

CREATE TABLE IF NOT EXISTS ciq_audits (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    client_id       INT DEFAULT NULL,
    label           VARCHAR(255) NOT NULL,
    domain          VARCHAR(512) NOT NULL,
    sitemap_url     VARCHAR(512) DEFAULT NULL,
    status          ENUM('pending','crawling','scoring','done','failed') DEFAULT 'pending',
    total_urls      INT DEFAULT 0,
    scored_urls     INT DEFAULT 0,
    triggered_by    ENUM('manual','scheduled','api') DEFAULT 'manual',
    notes           TEXT DEFAULT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at     DATETIME DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ciq_pages (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    audit_id            INT NOT NULL,
    url                 VARCHAR(2048) NOT NULL,
    title               VARCHAR(512) DEFAULT NULL,
    word_count          INT DEFAULT NULL,
    last_modified       DATE DEFAULT NULL,
    -- Ahrefs metrics
    ahrefs_traffic      INT DEFAULT NULL,
    ahrefs_keywords     INT DEFAULT NULL,
    ahrefs_backlinks    INT DEFAULT NULL,
    ahrefs_dr           TINYINT DEFAULT NULL,
    -- GSC metrics
    gsc_clicks          INT DEFAULT NULL,
    gsc_impressions     INT DEFAULT NULL,
    gsc_ctr             DECIMAL(5,2) DEFAULT NULL,
    gsc_position        DECIMAL(5,2) DEFAULT NULL,
    -- Scores (0-100)
    score_freshness     TINYINT DEFAULT NULL,
    score_geo           TINYINT DEFAULT NULL,
    score_eeat          TINYINT DEFAULT NULL,
    score_seo_health    TINYINT DEFAULT NULL,
    score_total         TINYINT DEFAULT NULL,
    -- Verdict
    verdict             ENUM('KEEP','UPDATE','CONSOLIDATE','DELETE') DEFAULT NULL,
    verdict_reason      TEXT DEFAULT NULL,
    -- Brief
    brief_generated     TINYINT(1) DEFAULT 0,
    brief_content       MEDIUMTEXT DEFAULT NULL,
    -- Meta
    competitor_gap      TINYINT(1) DEFAULT 0,
    crawled_at          DATETIME DEFAULT NULL,
    scored_at           DATETIME DEFAULT NULL,
    UNIQUE KEY uq_audit_url (audit_id, url(768)),
    FOREIGN KEY (audit_id) REFERENCES ciq_audits(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ciq_competitors (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    audit_id    INT NOT NULL,
    domain      VARCHAR(512) NOT NULL,
    label       VARCHAR(255) DEFAULT NULL,
    added_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (audit_id) REFERENCES ciq_audits(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

Task 3 — Mount router in main.py:
from modules.content_iq.router import router as ciq_router
app.include_router(ciq_router, prefix="/content-iq", tags=["ContentIQ"])

Task 4 — Add to .env.example:
AHREFS_API_KEY=your_ahrefs_v3_key_here
GSC_CLIENT_ID=your_gsc_oauth_client_id
GSC_CLIENT_SECRET=your_gsc_oauth_client_secret
GSC_REDIRECT_URI=https://app.nrankai.com/content-iq/gsc/callback
CONTENTIQ_DEMO_SECRET=set_a_random_secret_here

Task 5 — Verify app starts without errors.
Scaffold only — no logic yet.
```

---

## PROMPT 02 — Sitemap Crawler + Page Meta Extraction

```
You are implementing modules/content_iq/crawler.py in the WLA project.

Dependencies to install if missing: httpx, lxml, beautifulsoup4, python-dateutil

The crawler has two responsibilities:
1. Parse the sitemap to get the URL list (reuse logic from modules/gsc_indexer/sitemap.py if it exists,
   otherwise implement inline)
2. For each URL, fetch the page and extract metadata

Implement:

async def crawl_sitemap(sitemap_url: str, max_urls: int = 2000) -> list[str]
- Fetch + parse sitemap XML (handle sitemap index → children recursively)
- Return deduplicated URL list capped at max_urls
- Use httpx.AsyncClient with timeout=20, User-Agent="nrankai-contentiq/1.0"

async def extract_page_meta(url: str, client: httpx.AsyncClient) -> dict
- GET the URL (follow redirects, timeout=15)
- Parse HTML with BeautifulSoup (lxml parser)
- Extract:
    title: <title> tag text (strip whitespace)
    word_count: count words in <main>, <article>, or <body> (in that priority)
               strip script/style tags before counting
    last_modified: try in this order:
        1. <meta name="last-modified"> or <meta property="article:modified_time">
        2. <time datetime="..."> tag
        3. HTTP Last-Modified response header
        → parse to date with dateutil.parser.parse(), return as ISO date string or None
    canonical: <link rel="canonical" href="..."> href value or None
    meta_description: <meta name="description" content="..."> or None
    h1: first <h1> text or None
    status_code: int
- Return dict with all fields; on any error return dict with url, status_code=0, error=str(e)

async def crawl_audit(audit_id: int, sitemap_url: str, db_pool, max_urls=2000, concurrency=5)
- Fetch URL list from sitemap
- Update ciq_audits SET total_urls=N, status='crawling' WHERE id=audit_id
- Use asyncio.Semaphore(concurrency) to limit concurrent fetches
- For each URL: call extract_page_meta(), then INSERT into ciq_pages
  (url, audit_id, title, word_count, last_modified, crawled_at=NOW())
  ON DUPLICATE KEY UPDATE title, word_count, last_modified, crawled_at
- After all pages done: UPDATE ciq_audits SET status='scoring' WHERE id=audit_id
- Log progress every 25 URLs: "[Crawler] 50/200 pages crawled"

After implementing, test with:
  python -c "
  import asyncio
  from modules.content_iq.crawler import crawl_sitemap, extract_page_meta
  import httpx

  async def test():
      urls = await crawl_sitemap('https://pagani.ro/sitemap.xml')
      print(f'Found {len(urls)} URLs')
      async with httpx.AsyncClient() as c:
          meta = await extract_page_meta(urls[0], c)
          print(meta)
  asyncio.run(test())
  "

Confirm URL count and that meta dict has title, word_count, last_modified populated.
```

---

## PROMPT 03 — Ahrefs v3 API Integration

```
You are implementing modules/content_iq/ahrefs.py in the WLA project.

Ahrefs API v3 docs: https://developers.ahrefs.com/api/v3
API key is in env var AHREFS_API_KEY.
Base URL: https://api.ahrefs.com/v4  (v3 uses v4 endpoint, check docs)

Dependencies: httpx, tenacity (for retry)

Implement an AhrefsClient class:

class AhrefsClient:
    def __init__(self, api_key: str)
    - Store api_key
    - Create httpx.AsyncClient with:
        base_url = "https://api.ahrefs.com/v4"
        headers = {"Authorization": f"Bearer {api_key}"}
        timeout = 30

    async def get_url_metrics(self, url: str) -> dict
    - Call GET /site-explorer/metrics
      params: {"select": "org_traffic,org_keywords,backlinks,domain_rating", "target": url, "mode": "exact"}
    - Return dict: {traffic, keywords, backlinks, dr} with int values (0 if missing)
    - On 429: raise RateLimitError
    - On any other error: log warning, return zeros dict

    async def get_top_pages(self, domain: str, limit: int = 200) -> list[dict]
    - Call GET /site-explorer/top-pages-by-traffic
      params: {"select": "url,traffic,keywords", "target": domain, "mode": "domain", "limit": limit}
    - Return list of {url, traffic, keywords}

    async def get_keywords_for_url(self, url: str, limit: int = 10) -> list[dict]
    - Call GET /site-explorer/organic-keywords
      params: {"select": "keyword,position,traffic,volume", "target": url, "mode": "exact", "limit": limit}
    - Return list of {keyword, position, traffic, volume}

    async def batch_url_metrics(self, urls: list[str], concurrency: int = 3) -> dict[str, dict]
    - For each URL call get_url_metrics() with asyncio.Semaphore(concurrency)
    - Return dict mapping url → metrics
    - Respect rate limits: catch RateLimitError, sleep 10s, retry once

Add retry decorator using tenacity on get_url_metrics:
  @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))

Also implement a standalone test:
  python ahrefs.py
  → fetches metrics for https://pagani.ro/ and prints result

After implementing, run the test and confirm you get numeric traffic/keywords values.
If the API structure differs from docs, adjust field names accordingly and note what changed.
```

---

## PROMPT 04 — GSC OAuth + Metrics Fetcher

```
You are implementing modules/content_iq/gsc.py in the WLA project.

This module handles:
1. Google OAuth 2.0 flow for Search Console API access
2. Fetching per-URL performance metrics from GSC

Dependencies: google-auth, google-auth-oauthlib, google-auth-httplib2, google-api-python-client

Env vars: GSC_CLIENT_ID, GSC_CLIENT_SECRET, GSC_REDIRECT_URI

Implement:

--- OAuth flow ---

def get_oauth_url(state: str) -> str
- Build Google OAuth URL with scopes:
    https://www.googleapis.com/auth/webmasters.readonly
  params: client_id, redirect_uri, response_type=code, access_type=offline, state, prompt=consent
- Return the URL string

async def exchange_code(code: str) -> dict
- POST to https://oauth2.googleapis.com/token
  body: code, client_id, client_secret, redirect_uri, grant_type=authorization_code
- Return {access_token, refresh_token, expires_in}

async def refresh_access_token(refresh_token: str) -> str
- POST to https://oauth2.googleapis.com/token to refresh
- Return new access_token string

--- GSC Data ---

async def get_page_metrics(
    access_token: str,
    property_url: str,
    urls: list[str],
    date_range_days: int = 90
) -> dict[str, dict]
- Use Google Search Console API v1 (searchanalytics.query)
- Endpoint: POST https://www.googleapis.com/webmasters/v3/sites/{property}/searchAnalytics/query
- Request body:
    startDate: today - date_range_days
    endDate: today
    dimensions: ["page"]
    dimensionFilterGroups: filter to only the given urls
    rowLimit: 25000
- Returns dict mapping url → {clicks, impressions, ctr, position}
- Handle pagination if needed (startRow)
- For URLs not in response: return {clicks:0, impressions:0, ctr:0.0, position:0.0}
- On auth error: raise GSCAuthError

Store tokens: implement save_tokens(audit_id, tokens, db_pool) and load_tokens(audit_id, db_pool)
Add a ciq_gsc_tokens table migration:
CREATE TABLE IF NOT EXISTS ciq_gsc_tokens (
    audit_id        INT PRIMARY KEY,
    access_token    TEXT,
    refresh_token   TEXT,
    expires_at      DATETIME,
    property_url    VARCHAR(512),
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (audit_id) REFERENCES ciq_audits(id) ON DELETE CASCADE
);

After implementing, add OAuth endpoints to the router (they'll be wired in Prompt 09):
- GET /gsc/auth?audit_id=N → redirect to OAuth URL
- GET /gsc/callback?code=...&state=... → exchange code, save tokens, redirect to /content-iq/{audit_id}

Do not test live yet — GSC OAuth will be tested end-to-end in Prompt 09.
```

---

## PROMPT 05 — Freshness Scoring Engine

```
You are implementing modules/content_iq/engines/freshness.py in the WLA project.

The Freshness engine scores a page on how fresh/updated its content is.
Score range: 0–100 (integer).

Inputs (all from ciq_pages row):
- last_modified: ISO date string or None
- word_count: int or None
- gsc_clicks: int (last 90 days)
- ahrefs_traffic: int (monthly organic)

Scoring logic — implement as score_freshness(page: dict) -> tuple[int, str]:

Step 1 — Date freshness (0–60 points):
  If last_modified is None:
    date_score = 20  (unknown = penalty, might be evergreen)
  Else:
    days_old = (today - last_modified).days
    if days_old <= 30:   date_score = 60
    elif days_old <= 90: date_score = 50
    elif days_old <= 180: date_score = 40
    elif days_old <= 365: date_score = 30
    elif days_old <= 730: date_score = 15
    else:                date_score = 0

Step 2 — Traffic signal bonus (0–25 points):
  combined = (gsc_clicks or 0) + (ahrefs_traffic or 0)
  if combined > 500:   traffic_bonus = 25
  elif combined > 100: traffic_bonus = 15
  elif combined > 10:  traffic_bonus = 8
  else:                traffic_bonus = 0

Step 3 — Content depth bonus (0–15 points):
  wc = word_count or 0
  if wc > 1500:   depth_bonus = 15
  elif wc > 800:  depth_bonus = 10
  elif wc > 300:  depth_bonus = 5
  else:           depth_bonus = 0

Final score = min(100, date_score + traffic_bonus + depth_bonus)

Reason string: human-readable explanation, e.g.:
  "Last updated 45 days ago. Good traffic signal (320 combined). Thin content (210 words)."

Return: (score: int, reason: str)

Also implement: batch_score(pages: list[dict]) -> list[dict]
- For each page dict, add score_freshness and freshness_reason keys
- Return updated list

Write pytest tests at the bottom (if __name__ == "__main__"):
  Test cases:
  1. page with last_modified=today, traffic=600, word_count=2000 → expect score >= 90
  2. page with last_modified=2 years ago, traffic=0, word_count=100 → expect score <= 15
  3. page with last_modified=None, traffic=50, word_count=500 → expect score ~33

Run tests and confirm all pass before finishing.
```

---

## PROMPT 06 — GEO, E-E-A-T, and SEO Health Scoring Engines

```
You are implementing three scoring engines in the WLA ContentIQ module.
Each follows the same pattern as freshness.py: score_X(page: dict) -> tuple[int, str]
Score range: 0–100.

--- engines/geo.py — GEO Visibility Score ---

Measures how well the page is positioned for AI/LLM answer engines.
Inputs: word_count, title, h1, meta_description, ahrefs_keywords, gsc_position, url

Scoring:
  Structure score (0–30):
    +10 if word_count >= 800 (enough for featured snippet)
    +10 if meta_description is not None and len > 80
    +10 if h1 is not None and 20 < len(h1) < 100

  Question/intent signals (0–25):
    +15 if title or h1 contains question words: ce, cum, când, de ce, care, what, how, when, why, which
    +10 if word_count >= 1200 (long-form = FAQ potential)

  SERP position bonus (0–25):
    gsc_position = page.get("gsc_position") or 99
    if position <= 3:  +25
    elif position <= 10: +15
    elif position <= 20: +8
    else: 0

  Keyword breadth (0–20):
    kw = ahrefs_keywords or 0
    if kw >= 50: +20
    elif kw >= 20: +12
    elif kw >= 5: +6
    else: 0

Reason: "Ranks at position X. Answers question intent. N keywords."

--- engines/eeat.py — E-E-A-T Score ---

Measures signals of Experience, Expertise, Authoritativeness, Trustworthiness.
Inputs: word_count, ahrefs_backlinks, ahrefs_dr, url, title, h1, last_modified

Scoring:
  Authority (0–35):
    bl = ahrefs_backlinks or 0
    if bl >= 50: +35
    elif bl >= 20: +25
    elif bl >= 5: +15
    elif bl >= 1: +8
    else: 0

  Domain Rating proxy (0–25):
    dr = ahrefs_dr or 0
    if dr >= 50: +25
    elif dr >= 30: +18
    elif dr >= 20: +12
    elif dr >= 10: +6
    else: 0

  Content depth (0–25):
    wc = word_count or 0
    if wc >= 2000: +25
    elif wc >= 1000: +18
    elif wc >= 500: +10
    else: 0

  Freshness signal (0–15):
    Same logic as freshness date_score but scaled to 15 max
    (reuse date score calculation, divide by 4)

Reason: "N backlinks, DR=X. Content depth: Y words."

--- engines/seo_health.py — SEO Health Score ---

Measures on-page and crawlability health signals.
Inputs: title, h1, meta_description, word_count, canonical, url, status_code

Scoring:
  Title (0–25):
    +15 if title is not None
    +10 if title and 40 <= len(title) <= 65

  Meta description (0–20):
    +10 if meta_description is not None
    +10 if meta_description and 100 <= len(meta_description) <= 160

  H1 (0–20):
    +10 if h1 is not None
    +10 if h1 and len(h1) >= 10

  Canonical (0–10):
    +10 if canonical and canonical == url (self-referencing canonical)
    +5 if canonical is None (ambiguous — neither good nor bad)
    0 if canonical points elsewhere

  Content length (0–15):
    wc = word_count or 0
    if wc >= 600: +15
    elif wc >= 300: +10
    elif wc >= 100: +5
    else: 0

  Status code (0–10):
    +10 if status_code == 200
    0 otherwise

Reason: "Title OK. Meta desc missing. H1 present. Thin content (N words)."

--- After implementing all three ---

In engines/__init__.py, export:
  from .freshness import score_freshness
  from .geo import score_geo
  from .eeat import score_eeat
  from .seo_health import score_seo_health

Write a combined test at the bottom of each file (if __name__ == "__main__")
testing with a sample pagani.ro-style page dict and printing the score + reason.

Confirm all engines return (int, str) tuples and scores are within 0–100.
```

---

## PROMPT 07 — KUCD Verdict Engine

```
You are implementing modules/content_iq/verdict.py in the WLA project.
All four scoring engines are already implemented in engines/.

The verdict engine combines the four scores into a KEEP/UPDATE/CONSOLIDATE/DELETE decision.

Implement: assign_verdict(page: dict) -> tuple[str, str]
Returns: (verdict: str, reason: str)

Inputs from page dict:
- score_freshness (0–100)
- score_geo (0–100)
- score_eeat (0–100)
- score_seo_health (0–100)
- score_total (weighted average, computed here)
- gsc_clicks (int)
- ahrefs_traffic (int)
- ahrefs_backlinks (int)
- word_count (int)

Step 1 — Compute weighted total score:
  score_total = round(
      score_freshness * 0.30 +
      score_geo       * 0.25 +
      score_eeat      * 0.25 +
      score_seo_health * 0.20
  )

Step 2 — Traffic signals:
  has_traffic = (gsc_clicks or 0) + (ahrefs_traffic or 0) > 50
  has_links = (ahrefs_backlinks or 0) >= 3

Step 3 — Verdict rules (evaluate in order, first match wins):

  DELETE:
    score_total < 20
    AND NOT has_traffic
    AND NOT has_links
    AND (word_count or 0) < 150
    Reason: "Very low scores, no traffic, no backlinks, thin content."

  CONSOLIDATE:
    score_total < 35
    AND NOT has_traffic
    Reason: "Low scores and no meaningful traffic. Merge with stronger related content."

  CONSOLIDATE (alternative):
    score_total < 40
    AND score_seo_health < 30
    AND NOT has_links
    Reason: "Poor SEO health, no authority signals. Consolidation candidate."

  UPDATE:
    score_total >= 35 AND score_total < 65
    OR (score_total >= 65 AND score_freshness < 25)
    Reason: "Decent authority but needs freshness update." or "Good scores but content is stale."

  UPDATE (traffic present, low scores):
    has_traffic AND score_total < 50
    Reason: "Has traffic but underperforming scores — update to protect rankings."

  KEEP:
    score_total >= 65
    AND score_freshness >= 40
    Reason: "Strong scores across all dimensions. Performing well."

  Default fallback → UPDATE
    Reason: "Mixed signals — review manually."

Also implement:

def score_and_verdict(page: dict) -> dict
- Calls all four score_X(page) functions
- Adds score_freshness, score_geo, score_eeat, score_seo_health to page dict
- Computes score_total
- Calls assign_verdict()
- Adds verdict, verdict_reason, score_total to page dict
- Returns updated page dict

def batch_score_and_verdict(pages: list[dict]) -> list[dict]
- Calls score_and_verdict() on each page
- Returns updated list

Write a test at the bottom (if __name__ == "__main__") with 4 synthetic pages:
1. High scores, fresh → expect KEEP
2. Some traffic, stale → expect UPDATE
3. No traffic, low scores, thin → expect DELETE
4. Moderate scores, no traffic → expect CONSOLIDATE

Print verdict + reason for each. Confirm logic works before finishing.
```

---

## PROMPT 08 — Claude API Brief Generator

```
You are implementing modules/content_iq/brief.py in the WLA project.

This module calls the Anthropic Claude API to generate content briefs
for pages that received UPDATE or CONSOLIDATE verdicts.

Dependencies: anthropic (pip install anthropic)
API key: ANTHROPIC_API_KEY from env (already in use by the WLA app)

Implement:

async def generate_brief(page: dict, audit_domain: str) -> str
Inputs from page dict:
  url, title, h1, word_count, last_modified, meta_description,
  score_total, score_freshness, score_geo, score_eeat, score_seo_health,
  verdict, verdict_reason,
  gsc_clicks, gsc_impressions, gsc_position,
  ahrefs_traffic, ahrefs_keywords, ahrefs_backlinks

Build this system prompt:
  "You are a senior SEO and GEO content strategist at nrankai.com.
  You produce precise, actionable content briefs for Romanian and international websites.
  Your briefs are structured, specific, and optimized for both traditional search and AI answer engines (GEO).
  Respond in the same language as the page title/URL — Romanian if the site is Romanian, English otherwise.
  Never include generic advice. Every recommendation must be specific to the given page."

Build this user prompt (use f-string with page data):
  "Generate a content brief for this page that needs a [{verdict}] action.

  URL: {url}
  Title: {title}
  H1: {h1}
  Word Count: {word_count}
  Last Modified: {last_modified}
  Meta Description: {meta_description}

  Performance scores (0-100):
  - Freshness: {score_freshness} — {freshness note from verdict_reason}
  - GEO Visibility: {score_geo}
  - E-E-A-T: {score_eeat}
  - SEO Health: {score_seo_health}
  - Total: {score_total}

  Traffic signals (last 90 days):
  - GSC Clicks: {gsc_clicks} | Impressions: {gsc_impressions} | Avg Position: {gsc_position}
  - Ahrefs Organic Traffic: {ahrefs_traffic} | Keywords: {ahrefs_keywords} | Backlinks: {ahrefs_backlinks}

  Verdict: {verdict} — {verdict_reason}

  Produce a brief with these sections:
  1. OBJECTIVE — Why this action, what outcome to achieve (2-3 sentences)
  2. TARGET KEYWORDS — 3–5 primary keywords to target or preserve, with intent
  3. CONTENT STRUCTURE — Recommended H1, H2 headings (as bullet list)
  4. CONTENT REQUIREMENTS — Word count target, key topics to cover, data/stats to include
  5. GEO OPTIMIZATION — How to structure content for AI answer engines (FAQ, definitions, direct answers)
  6. E-E-A-T SIGNALS — Specific additions: author bio, citations, trust elements
  7. INTERNAL LINKING — 2–3 specific internal link opportunities on {audit_domain}
  8. PRIORITY — High/Medium/Low with 1-line justification"

API call:
  model: claude-sonnet-4-20250514
  max_tokens: 1500
  temperature: 0.3

Return the text content of the response.

async def batch_generate_briefs(
    pages: list[dict],
    audit_domain: str,
    audit_id: int,
    db_pool,
    concurrency: int = 3
) -> int
- Filter pages where verdict in ('UPDATE', 'CONSOLIDATE') and brief_generated = 0
- Use asyncio.Semaphore(concurrency)
- For each page: generate_brief() → save to ciq_pages (brief_content, brief_generated=1, scored_at=NOW())
- Return count of briefs generated
- Log progress: "[Brief] 5/20 briefs generated"

After implementing, test with ONE page from pagani.ro (use a real page dict from ciq_pages):
  python -c "
  import asyncio, os
  from modules.content_iq.brief import generate_brief
  page = {
    'url': 'https://pagani.ro/vis-cu-apa/',
    'title': 'Vis cu apă — interpretare', 'h1': 'Ce înseamnă visul cu apă',
    'word_count': 450, 'last_modified': '2023-06-01', 'meta_description': None,
    'score_total': 42, 'score_freshness': 25, 'score_geo': 48, 'score_eeat': 38, 'score_seo_health': 55,
    'verdict': 'UPDATE', 'verdict_reason': 'Decent authority but stale content.',
    'gsc_clicks': 85, 'gsc_impressions': 2100, 'gsc_position': 14.2,
    'ahrefs_traffic': 60, 'ahrefs_keywords': 12, 'ahrefs_backlinks': 2
  }
  brief = asyncio.run(generate_brief(page, 'pagani.ro'))
  print(brief)
  "

Confirm the brief is structured with all 8 sections and is in Romanian.
```

---

## PROMPT 09 — FastAPI Router (Full)

```
You are implementing the complete modules/content_iq/router.py in the WLA project.
All engines, verdict.py, brief.py, crawler.py, ahrefs.py, gsc.py are implemented.

Implement all DB helpers first (async, aiomysql.DictCursor):

db_create_audit(conn, label, domain, sitemap_url, client_id, triggered_by) → audit_id
db_get_audit(conn, audit_id) → dict
db_list_audits(conn, client_id=None, limit=20) → list
db_update_audit_status(conn, audit_id, status, finished_at=None, scored_urls=None)
db_get_pages(conn, audit_id, verdict=None, limit=200, offset=0) → list
db_get_page(conn, page_id) → dict
db_get_stats(conn, audit_id) → dict with counts per verdict + avg scores
db_add_competitor(conn, audit_id, domain, label) → id
db_get_competitors(conn, audit_id) → list
db_save_page_scores(conn, page_id, scores: dict)

Pydantic models:
  CreateAuditRequest(label, domain, sitemap_url, client_id=None)
  AddCompetitorRequest(domain, label=None)

Endpoints:

GET  /                          → render contentiq.html with recent audits
GET  /{audit_id}                → render contentiq.html with audit detail + pages
POST /audits                    → create audit, trigger crawl_audit() as BackgroundTask
GET  /audits                    → list audits (JSON)
GET  /audits/{audit_id}         → audit detail + stats (JSON)
GET  /audits/{audit_id}/pages   → pages list, filter by ?verdict=KEEP|UPDATE|CONSOLIDATE|DELETE
GET  /audits/{audit_id}/pages/{page_id} → single page detail with brief
POST /audits/{audit_id}/score   → BackgroundTask: run batch_score_and_verdict() on all crawled pages,
                                  update ciq_pages, update audit status to 'done'
POST /audits/{audit_id}/briefs  → BackgroundTask: run batch_generate_briefs() for UPDATE+CONSOLIDATE pages
POST /audits/{audit_id}/competitors → add competitor domain
GET  /audits/{audit_id}/stats   → verdict distribution + avg scores (for charts)

GSC OAuth (from gsc.py):
GET  /gsc/auth?audit_id=N       → redirect to Google OAuth URL with state=audit_id
GET  /gsc/callback              → exchange code, save tokens, redirect to /content-iq/{audit_id}
POST /audits/{audit_id}/gsc-sync → BackgroundTask: fetch GSC metrics for all pages, update ciq_pages

Ahrefs:
POST /audits/{audit_id}/ahrefs-sync → BackgroundTask: batch_url_metrics() for all pages, update ciq_pages

All background tasks: update audit status at start, log progress, handle exceptions gracefully.
Verify all endpoints return correct HTTP status codes.
```

---

## PROMPT 10 — Dashboard + Audit Detail UI

```
You are implementing the ContentIQ dashboard UI in the WLA project.
Two templates: contentiq.html and contentiq_detail.html (or a single template with conditional sections).
Extend base.html. Tailwind CSS only. Vanilla JS only.

--- contentiq.html (Audit List / Dashboard) ---

Header: "ContentIQ" + subtitle "Content Audit Engine — KUCD Verdicts"
Button: "+ New Audit" → opens a modal

New Audit Modal:
  - Input: Label (e.g. "Pagani.ro Q2 2026 Audit")
  - Input: Domain (e.g. pagani.ro)
  - Input: Sitemap URL (e.g. https://pagani.ro/sitemap.xml)
  - Button: "Start Audit" → POST /content-iq/audits, close modal, refresh list

Audit list table:
  Columns: Label, Domain, Status (badge), Total URLs, Scored, Created At, Actions
  Status badges: pending=gray, crawling=blue(animated pulse), scoring=yellow(pulse), done=green, failed=red
  Actions: "View" → /content-iq/{audit_id}
  Auto-refresh every 15s if any audit is in pending/crawling/scoring state

--- contentiq_detail.html (Audit Detail) ---

Top: Audit name, domain, status badge, created_at

Stats bar (5 cards):
  KEEP (green), UPDATE (blue), CONSOLIDATE (yellow), DELETE (red), Total Pages (gray)
  Load from GET /content-iq/audits/{audit_id}/stats

Donut chart (pure CSS or simple SVG — no chart library):
  Show verdict distribution visually

Actions row:
  "⬇ Sync GSC" → GET /content-iq/gsc/auth?audit_id=N
  "⬆ Sync Ahrefs" → POST /content-iq/audits/{audit_id}/ahrefs-sync
  "⚙ Run Scoring" → POST /content-iq/audits/{audit_id}/score
  "✍ Generate Briefs" → POST /content-iq/audits/{audit_id}/briefs
  "📥 Export CSV" → GET /content-iq/audits/{audit_id}/export (wired in Prompt 11)

Verdict filter tabs: All | KEEP | UPDATE | CONSOLIDATE | DELETE
  Click tab → load pages filtered by verdict

Pages table:
  Columns: URL (truncated + link), Title, Verdict (badge), Total Score,
           Freshness, GEO, E-E-A-T, SEO Health, GSC Clicks, Ahrefs Traffic, Brief (✅/—)
  Click row → expand inline to show brief content (if generated)
  Verdict badge colors: KEEP=green, UPDATE=blue, CONSOLIDATE=amber, DELETE=red

Score columns: show as colored number (>=70 green, 40-69 yellow, <40 red)

All data loaded via fetch() from JSON endpoints.
Show loading skeleton while fetching.
Auto-refresh stats every 20s if audit status is not 'done'.
```

---

## PROMPT 11 — Export Engine + Competitor Gap

```
You are implementing two features in the ContentIQ module.

--- Part 1: Export Engine (modules/content_iq/export.py) ---

Dependencies: openpyxl

Implement: async def export_audit_xlsx(audit_id: int, db_pool) -> bytes

Creates an Excel workbook with 3 sheets:

Sheet 1: "Summary"
  - Row 1: Audit label, domain, date
  - Stats table: verdict → count, % of total, avg total score
  - Avg scores per engine (Freshness, GEO, E-E-A-T, SEO Health)

Sheet 2: "All Pages"
  Columns: URL, Title, Verdict, Total Score, Freshness, GEO, E-E-A-T, SEO Health,
           Word Count, Last Modified, GSC Clicks, GSC Position, Ahrefs Traffic,
           Ahrefs Keywords, Ahrefs Backlinks, Notes
  Row colors: KEEP=light green, UPDATE=light blue, CONSOLIDATE=light yellow, DELETE=light red
  Freeze top row, auto-filter on all columns

Sheet 3: "Briefs"
  Only pages with brief_generated=1
  Columns: URL, Title, Verdict, Brief Content (full text, wrap text on)
  Wide column for brief content (width=120)

Add endpoint to router.py:
  GET /audits/{audit_id}/export
  → generate bytes, return as StreamingResponse with:
    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    Content-Disposition: attachment; filename="contentiq_{domain}_{date}.xlsx"

--- Part 2: Competitor Gap Analysis (add to router.py) ---

Implement: async def run_competitor_gap(audit_id: int, db_pool)
- Load competitor domains from ciq_competitors for this audit
- For each competitor: call ahrefs_client.get_top_pages(competitor_domain, limit=100)
- Get all client page URLs from ciq_pages for this audit
- Find competitor top URLs that have NO equivalent in client pages
  (match by URL path slug similarity — strip domain, compare path)
- For each gap URL: update ciq_pages SET competitor_gap=1 WHERE audit_id matches and url is similar
  OR insert new rows with state='gap' (add 'gap' to the ENUM if not present)
- Return: {gaps_found: int, competitors_checked: int}

Add endpoint:
  POST /audits/{audit_id}/competitor-gap → BackgroundTask: run_competitor_gap()

Add "Competitor Gap" column to the export Sheet 2 (✓ or blank).

After implementing:
1. Test export with: curl http://localhost:8000/content-iq/audits/1/export -o test_export.xlsx
   Open in LibreOffice and confirm 3 sheets + color coding
2. Add a competitor for pagani.ro (e.g. 123vise.ro) via POST /audits/1/competitors
   then POST /audits/1/competitor-gap and confirm gap count is returned
```

---

## PROMPT 12 — Public Demo Page + Navigation + Final Integration

```
You are finalizing the ContentIQ module integration in the WLA project.

Task 1: Public Demo Page

Create a public-facing demo at /content-iq/demo (no auth required).
Template: contentiq_demo.html (does NOT extend base.html — standalone page with nrankai branding).

The demo page shows a fictional but realistic audit of "pagani.ro" with hardcoded/seeded data:
- 5 example pages with all 4 scores and verdicts (one of each: KEEP, UPDATE, UPDATE, CONSOLIDATE, DELETE)
- Stats bar showing distribution
- A snippet of a generated brief (UPDATE page, first 3 sections only)
- CTA: "Run a full audit for your site" → links to /content-iq/ (auth required)
- nrankai logo, clean layout, trust signals ("Powered by Claude AI", "Ahrefs + GSC integrated")

Protect /content-iq/ (the main app) with existing WLA auth middleware.
/content-iq/demo is public — no auth check.

Task 2: Navigation

Add "ContentIQ" to the main WLA nav (base.html or nav partial).
Same style as other module links. Icon suggestion: 📊 or a bar chart icon.
Add a "NEW" badge next to it (small green pill).

Task 3: n8n Scheduled Audit Workflow

Create n8n_workflows/contentiq_scheduled_audit.json:
- Schedule trigger: every Monday 07:00
- Step 1: HTTP Request → POST /content-iq/audits
  Body: {label: "Weekly Audit — pagani.ro", domain: "pagani.ro",
         sitemap_url: "https://pagani.ro/sitemap.xml"}
- Step 2: Wait node (30 minutes — give crawl time to finish)
- Step 3: HTTP Request → POST /content-iq/audits/{{audit_id}}/score
- Step 4: Wait node (10 minutes)
- Step 5: HTTP Request → POST /content-iq/audits/{{audit_id}}/briefs
- Step 6: HTTP Request → GET /content-iq/audits/{{audit_id}}/export → save to Google Drive or send via email

Task 4: End-to-End Smoke Test

Run through this full flow against pagani.ro:
1. POST /content-iq/audits → audit created, crawl starts
2. Wait for status = 'scoring'
3. POST /content-iq/audits/{id}/score → scoring runs
4. GET /content-iq/audits/{id}/stats → confirm verdict counts are non-zero
5. POST /content-iq/audits/{id}/briefs → generates at least 1 brief
6. GET /content-iq/audits/{id}/export → downloads xlsx, open and verify
7. GET /content-iq/{id} → dashboard loads with correct data
8. GET /content-iq/demo → public page loads without auth

Report any failures with specific error messages and fix them.
When all 8 steps pass, the module is production-ready.
```
