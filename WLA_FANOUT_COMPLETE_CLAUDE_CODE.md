# WLA Fan-Out Analyzer — MASTER CLAUDE CODE PROMPTS
## Toate fazele 1-5 — Copy-paste ready pentru Claude Code

**Repo principal:** cosminstan90/nrankai-tool (github.com)
**Ultima actualizare:** Aprilie 2026

---

## ORDINE DE IMPLEMENTARE

```
PHASE 1:  1 → 7 → 2 → 3 → 8 → 5 → 4 → 6
PHASE 2:  30 → 29 → 9 → 10 → 11 → 12 → 13 → 14
PHASE 3:  15 → 16 → 17 → 18 → 19 → 20 → 21
PHASE 4:  25 → 22 → 23 → 24 → 31 → 27 → 28
PHASE 5:  32 → 33 → 34 → 35 → 36
```

## NOTE CRITICE

```
MODELE CORECTE (Aprilie 2026):
- Fan-out default:   gpt-4.1
- Fan-out bulk:      gpt-4.1-mini
- Gemini:            gemini-2.5-flash
- Perplexity:        sonar-pro
- Claude intern:     claude-haiku-4-5-20251001
- Claude calibrare:  claude-sonnet-4-6

ARHITECTURĂ REPO — IMPORTANT:
- FanoutAnalyzer + FastAPI endpoints + templates = TOATE în nrankai-tool (api/)
- Structura reală: api/workers/fanout_analyzer.py, api/routes/fanout.py
- Import direct în routes: from api.workers.fanout_analyzer import FanoutAnalyzer, ...
- nrankai-cloud (outreach) NU importă niciodată din nrankai-tool

SINGURA EXCEPȚIE — Prompt 6 (prospect enrichment din nrankai-cloud):
- nrankai-cloud apelează nrankai-tool prin HTTP la POST /api/fanout/analyze
- Autentificat cu X-API-Key (nu import direct)
- .env în nrankai-cloud: NRANKAI_TOOL_URL=https://app.nrankai.com + NRANKAI_TOOL_API_KEY=nrk_...
- Folosește httpx.AsyncClient, timeout=60s

FIȘIERE EXISTENTE ÎN REPO — CITEȘTE ÎNAINTE DE:
- Prompt 9:  perplexity_researcher.py
- Prompt 11: history_tracker.py
- Prompt 18: cross_reference_analyzer.py
- Prompt 24: api/routes/benchmarks.py
- Prompt 30: HARDENING_NOTES_MASTER_V1.md

ELIMINATE (există deja):
- PDF Report → există în v2.1.0 (WHITE_LABEL_PDF)
- Schema Generator → există în v2.2.0
- Benchmarks WLA → există în v1.3.0 (Prompt 24 adaugă GEO benchmarks separat)
```

---

# ═══════════════════════════════════════
# PHASE 1 — CORE FAN-OUT ANALYZER
# ═══════════════════════════════════════

## PROMPT 1 — OpenAI Responses API Fan-Out Extractor

```
Repo: nrankai-tool

Creează fișierul `fanout_analyzer.py` în root-ul proiectului.

Acest modul folosește OpenAI Responses API cu web_search tool pentru a extrage
query-urile de fan-out pe care ChatGPT le generează intern când răspunde la un prompt.

1. Dataclass `FanoutResult`:
   - prompt: str
   - engine: str = "chatgpt"
   - model: str = "gpt-4.1"
   - fanout_queries: List[str]
   - sources: List[dict]          # url, title, domain, position
   - search_call_count: int
   - total_sources: int
   - total_fanout_queries: int
   - run_cost_usd: float = 0.0
   - locale: str = "ro-RO"
   - query_origin: str = "actual"   # actual | inferred | generated
   - source_origin: str = "citation"
   - prompt_cluster: str = None
   - from_cache: bool = False
   - timestamp: datetime
   - raw_response: dict

2. Clasa `FanoutAnalyzer` (ChatGPT):
   __init__(self, api_key=None, model="gpt-4.1")
   api_key din parametru sau os.getenv("OPENAI_API_KEY")
   
   async def analyze_prompt(self, prompt: str, locale: str = "ro-RO") -> FanoutResult:
     response = client.responses.create(
         model=self.model,
         input=prompt,
         tools=[{"type": "web_search_20250305"}]
     )
     Parsează response.output:
     - type="web_search_call" → query din action.query → fanout_queries
     - type="message" → annotations url_citation → sources (url, title)
   
   async def analyze_batch(self, prompts, locale="ro-RO") -> List[FanoutResult]:
     Secvențial cu delay 1s între calls pentru rate limiting.

3. Funcție `classify_prompt_cluster(prompt: str) -> str`:
   Clasificare locală, fără API call:
   CLUSTERS = {
     "branded":         ["review", "complaint", "vs", "alternative", "is X good"],
     "best_of":         ["best", "top", "leading", "highest rated", "#1"],
     "pricing":         ["cost", "price", "how much", "cheap", "affordable"],
     "comparison":      ["versus", "compare", "difference", "better than"],
     "local":           ["near me", "nearby", "local", "closest"],
     "alternatives":    ["alternative", "instead of", "similar to"],
     "problem_solution":["how to", "how do i", "fix", "solve", "guide"],
   }
   Prioritate: branded > pricing > comparison > local > best_of > alternatives > problem_solution
   Fallback: "generic"

4. Funcție `extract_search_queries(response_output) -> List[str]`:
   Iterează response.output, extrage din type="web_search_call".

5. Funcție `extract_sources(response_output) -> List[dict]`:
   Iterează type="message", extrage url_citation annotations, de-duplică pe URL.

6. Cost calculation:
   COST_PER_1K_TOKENS = {
     "gpt-4.1":      {"input": 0.002, "output": 0.008},
     "gpt-4.1-mini": {"input": 0.0001, "output": 0.0004},
   }

7. __main__ block:
   python fanout_analyzer.py "best seo agency romania"
   python fanout_analyzer.py "best seo agency romania" --model gpt-4.1-mini --locale ro-RO

Dependențe: openai>=1.40.0, python-dotenv
Error handling: API key missing, rate limit retry exponential backoff max 3x, invalid response.
Logging cu logger = logging.getLogger("fanout_analyzer").
```

---

## PROMPT 2 — MariaDB Schema + Storage Layer

```
Repo: nrankai-cloud

Creează `models/fanout_models.py` și migrația SQL.

1. Tabel `fanout_sessions`:
   CREATE TABLE fanout_sessions (
     id VARCHAR(36) PRIMARY KEY,
     prompt TEXT NOT NULL,
     model VARCHAR(100) NOT NULL DEFAULT 'gpt-4.1',
     engine VARCHAR(50) DEFAULT 'chatgpt',
     user_location VARCHAR(200),
     locale VARCHAR(20) DEFAULT 'ro-RO',
     language VARCHAR(10) DEFAULT 'ro',
     total_fanout_queries INT DEFAULT 0,
     total_sources INT DEFAULT 0,
     total_search_calls INT DEFAULT 0,
     target_url VARCHAR(500),
     target_found_in_sources BOOLEAN DEFAULT FALSE,
     target_source_position INT,
     query_origin ENUM('actual','inferred','generated') DEFAULT 'actual',
     source_origin ENUM('citation','grounding','extracted') DEFAULT 'citation',
     prompt_cluster VARCHAR(50) DEFAULT NULL,
     run_cost_usd DECIMAL(8,6) DEFAULT 0.000000,
     confidence_score FLOAT DEFAULT NULL,
     from_cache BOOLEAN DEFAULT FALSE,
     project_id VARCHAR(36) DEFAULT NULL,
     campaign_id VARCHAR(50),
     audit_id VARCHAR(36),
     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
     INDEX idx_campaign (campaign_id),
     INDEX idx_target (target_url),
     INDEX idx_created (created_at),
     INDEX idx_cluster (prompt_cluster),
     INDEX idx_engine (engine),
     INDEX idx_project (project_id)
   );

2. Tabel `fanout_queries`:
   CREATE TABLE fanout_queries (
     id INT AUTO_INCREMENT PRIMARY KEY,
     session_id VARCHAR(36) NOT NULL,
     query_text TEXT NOT NULL,
     query_position INT,
     query_origin ENUM('actual','inferred','generated') DEFAULT 'actual',
     confidence_score FLOAT DEFAULT 1.0,
     FOREIGN KEY (session_id) REFERENCES fanout_sessions(id) ON DELETE CASCADE,
     INDEX idx_session (session_id)
   );

3. Tabel `fanout_sources`:
   CREATE TABLE fanout_sources (
     id INT AUTO_INCREMENT PRIMARY KEY,
     session_id VARCHAR(36) NOT NULL,
     url VARCHAR(2000) NOT NULL,
     title VARCHAR(500),
     domain VARCHAR(500),
     position INT,
     is_target BOOLEAN DEFAULT FALSE,
     citation_count INT DEFAULT 1,
     FOREIGN KEY (session_id) REFERENCES fanout_sessions(id) ON DELETE CASCADE,
     INDEX idx_session (session_id),
     INDEX idx_domain (domain)
   );

4. SQLAlchemy models pentru cele 3 tabele (pattern existent din proiect).

5. Pydantic schemas:
   FanoutSessionCreate, FanoutSessionResponse, FanoutQueryResponse, FanoutSourceResponse

6. Helper `save_fanout_result(db, fanout_result, target_url=None) -> str`:
   - Salvează session + queries + sources
   - Dacă target_url: verifică prezența în sources, setează target_found + position
   - Extrage domain din URL cu urllib.parse.urlparse
   - Calculează prompt_cluster via classify_prompt_cluster()
   - Returnează session_id

Migrație: `migrations/007_fanout_tables.sql`
Pattern conexiune DB: folosește ce există în proiect (verifică models/ sau database.py).
```

---

## PROMPT 3 — FastAPI Endpoints

```
Repo: nrankai-tool  ← CORECTAT (nu nrankai-cloud)

ARHITECTURĂ: FanoutAnalyzer și endpoints-urile FastAPI sunt în ACELAȘI repo.
Import direct: from api.workers.fanout_analyzer import FanoutAnalyzer, MultiEngineFanoutAnalyzer
Fișier: api/routes/fanout.py (există deja — citește-l înainte)

Creează `api/routes/fanout.py`.

1. POST /api/fanout/analyze
   Body: {prompt, model, target_url, user_location, locale, project_id, campaign_id}
   Response: {session_id, prompt, fanout_queries, sources, stats}
   - Apelează FanoutAnalyzer din nrankai-tool
   - Salvează în DB via save_fanout_result()

2. POST /api/fanout/analyze-batch
   Body: {prompts (max 10), model, target_url, user_location, locale, project_id}
   Response: {job_id, total_prompts, status: "processing"}
   - Background task (BackgroundTasks FastAPI)

3. GET /api/fanout/sessions
   Query: campaign_id, target_url, project_id, cluster, engine, locale,
          query_origin, limit (default 20), offset
   Response: lista paginată + aggregation:
   {
     sessions: [...],
     aggregation: {
       by_cluster: {pricing: 12, best_of: 8},
       by_engine: {chatgpt: 20, gemini: 15},
       total_cost_usd: float,
       avg_mention_rate: float
     }
   }

4. GET /api/fanout/sessions/{session_id}
   Response: sesiunea completă cu queries și sources

5. GET /api/fanout/sessions/{session_id}/coverage
   Response: {
     target_url, target_found, target_position,
     coverage_score,         # % queries unde target apare
     missing_queries,
     competing_domains: [{domain, appearances}]
   }

6. GET /api/fanout/sessions/{session_id}/composite-score
   → Calculează și returnează CompositeScoreBreakdown (din Prompt 22)

7. DELETE /api/fanout/sessions/{session_id}

Înregistrează router în main.py cu prefix-ul existent.
Rate limit: max 5 analyze requests/minut (in-memory counter simplu).
```

---

## PROMPT 4 — Cross-Reference cu WLA Modules Existente

```
Repo: nrankai-cloud

Creează `workers/fanout_cross_reference.py`.

IMPORTANT: Verifică tabelele existente din DB (audit_results, prospects, citations)
și adaptează query-urile SQL. Dacă un tabel nu există, skip și returnează null.

1. Funcție `cross_reference_with_citations(session_id, db) -> dict`:
   - Compară sources din fanout_sources cu citation_tracker results
   - Returnează: cited_and_in_fanout, in_fanout_not_cited, cited_not_in_fanout, overlap_score

2. Funcție `generate_content_gap_from_fanout(session_id, target_url, db) -> List[dict]`:
   - Pentru fiecare fanout_query, verifică dacă target_url are conținut relevant
   - Keyword matching simplu pe titluri/URL-uri din audit results
   - Returnează content gaps cu: fanout_query, has_content, suggested_content_type,
     priority (high dacă query în primele 5 fan-out positions), competing_urls

3. Funcție `calculate_retrieval_coverage(session_id, target_domain, db) -> dict`:
   - total_fanout_queries, queries_where_domain_appears, retrieval_coverage_pct
   - top_competing_domains: [{domain, coverage_pct, appearances}]
   - improvement_potential: high (<20%) | medium (20-50%) | low (>50%)

4. Funcție `generate_fanout_action_cards(session_id, target_url, db) -> List[dict]`:
   a) "Low Retrieval Coverage" dacă coverage < 20%: priority critical
   b) "Competitor Dominance" dacă competitor > 50% din sources: priority high
   c) "Quick Win Queries" — target prezent dar nu în top 3: priority medium
   d) "Missing Content Types" — tipuri de content lipsă: priority medium
   
   Format action card:
   {id, type, priority, title, description, action_items: [...], data: {}}

5. Dacă tabelul prospects există:
   Funcție `enrich_prospect_with_fanout(prospect_id, prompts, db) -> dict`:
   - Rulează fan-out pe prompturi relevante
   - Actualizează prospect cu geo_visibility_score + top_issues JSON

6. Endpoint GET /api/fanout/sessions/{session_id}/cross-reference
   → Apelează toate funcțiile, returnează JSON combinat

7. Endpoint POST /api/fanout/enrich-prospect
   Body: {prospect_id, prompts, model: "gpt-4.1-mini"}
   → Background task, returnează job_id

8. Endpoint POST /api/fanout/enrich-batch
   Body: {campaign_id, prompts_template, model, limit: 50}
   → Rate limit: 1 prospect per 5 secunde
```

---

## PROMPT 5 — UI Page (Jinja2 Template)

```
Repo: nrankai-cloud

Creează `templates/fanout.html`.

Folosește AlpineJS + Tailwind CSS, consistent cu celelalte template-uri din proiect.

1. SECȚIUNEA "Analyze":
   - Input text mare: prompt
   - Select model: gpt-4.1, gpt-4.1-mini
   - Select engines: ChatGPT ☑, Gemini ☐, Perplexity ☐
   - Input opțional: Target URL, User Location, Locale
   - Buton "Analyze Fan-Out" → POST /api/fanout/analyze
   - Loading spinner: "Analyzing fan-out queries... 10-30 seconds"

2. SECȚIUNEA "Results":
   - Stats bar: "22 fan-out queries • 145 sources • 8 search calls"
   - Target coverage badge: verde >50% | galben 20-50% | roșu <20%
   - Cost badge: "$0.024 used"

3. Tab "Fan-Out Queries":
   - Tabel: #, Query, Cluster badge, Origin badge (actual/inferred), Copy button
   - Queries verde dacă target domain apare în sources pentru acel query

4. Tab "Sources":
   - Tabel: #, Domain, URL (link), Title, Is Target badge
   - Filter: "Show only target domain"
   - Sortabil pe domain

5. Tab "Domain Analysis":
   - Bar chart (Chart.js): top 10 domenii după apariții
   - Target domain cu culoare diferită
   - Tabel: domain, appearances, % din total

6. Tab "Content Gaps":
   - Tabel: Fan-out Query, Has Content, Priority, Suggested Type, Competing URLs
   - Badge-uri: High=roșu, Medium=galben, Low=verde
   - Buton "Generate Content Brief" per rând

7. Tab "Multi-Engine" (dacă multiple engines selectate):
   - Venn diagram SVG simplu cu source overlap
   - Tabel: Source | ChatGPT ✓ | Gemini ✓ | Perplexity ✗
   - Engine Agreement Score gauge

8. Tab "Prompt Discovery":
   - Dropdown: Category (seo_agency, beauty_clinic, dental, saas, generic...)
   - Input: Target Domain, Target Brand, Location
   - Slider: prompts to test 5-50 cu cost estimate live
   - Results: mention rate bar, tabel split, competitor dominance chart

9. Tab "Timeline":
   - Dropdown: tracking config
   - Line chart: mention_rate + composite_score over time
   - Trend badge: Improving ↑ | Declining ↓ | Stable →
   - Linie referință = benchmark median
   - Buton [+ Create Tracking] → modal
   - Banner dacă model drift detectat

10. Tab "Competitive":
    - Textarea: competitor domains (max 5)
    - Cost estimate
    - Ranking table + head-to-head matrix

11. SECȚIUNEA "History":
    - Tabel ultimele 20 sesiuni cu Prompt, Date, Queries, Sources, Coverage, Cost

Toate fetch-uri cu relative paths (niciodată hardcodat localhost).
Adaugă "Fan-Out Analyzer" în navigația din base.html după "GEO Monitor".
```

---

## PROMPT 6 — Action Cards Integration + Prospect Enrichment

```
Repo: nrankai-cloud  ← ACESTA e singurul prompt din Phase 1 în nrankai-cloud

ARHITECTURĂ INTER-REPO: nrankai-cloud apelează nrankai-tool prin HTTP (nu import direct).
Prospect enrichment se face via:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{settings.NRANKAI_TOOL_URL}/api/fanout/analyze",
            json={"prompt": prompt, "target_url": prospect.website},
            headers={"X-API-Key": settings.NRANKAI_TOOL_API_KEY},
            timeout=60.0
        )
.env în nrankai-cloud: NRANKAI_TOOL_URL=https://app.nrankai.com + NRANKAI_TOOL_API_KEY=nrk_...

(Conținut inclus în Prompt 4 — implementat împreună.)
Verifică că action cards se afișează în fanout.html și că
endpoint-urile de enrichment funcționează cu tabelul prospects existent.

Adaugă în UI (fanout.html, results section):
- Tab "Action Cards": lista card-urilor generate cu priority badges
- Buton "Enrich Prospect" dacă session are target_url dintr-un prospect

Buton "Generate All Action Cards" → POST /api/fanout/sessions/{id}/action-cards
```

---

## PROMPT 7 — Deployment Config

```
Repo: nrankai-cloud

1. Actualizează `.env.example`:
   APP_ENV=development
   APP_HOST=0.0.0.0
   APP_PORT=8000
   APP_BASE_URL=http://localhost:8000
   APP_SECRET_KEY=change-me-in-production
   DB_HOST=localhost
   DB_PORT=3306
   DB_NAME=nrankai_cloud
   DB_USER=nrankai
   DB_PASSWORD=change-me
   OPENAI_API_KEY=
   ANTHROPIC_API_KEY=
   PERPLEXITY_API_KEY=
   GOOGLE_API_KEY=
   SERPER_API_KEY=
   GOOGLE_API_KEY=
   N8N_WEBHOOK_URL=
   GOOGLE_CLIENT_ID=
   GOOGLE_CLIENT_SECRET=
   GOOGLE_REDIRECT_URI=https://app.nrankai.com/api/gsc/callback
   VELOCITYCMS_API_URL=
   VELOCITYCMS_API_KEY=
   DISABLE_AUTH=false
   CORS_ORIGINS=http://localhost:3000,http://localhost:8000
   ALLOWED_HOSTS=app.nrankai.com,localhost

2. În main.py:
   development: auto-reload, CORS permisiv, logging DEBUG
   production: CORS strict, trust proxy headers, cookie secure=True

3. Creează `scripts/run_local.sh`:
   #!/bin/bash
   set -e
   if [ ! -f .env ]; then cp .env.example .env; fi
   if [ -d "venv" ]; then source venv/bin/activate; fi
   pip install -r requirements.txt --quiet
   python manage_db.py migrate 2>/dev/null || echo "Skip migrations"
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   chmod +x scripts/run_local.sh

4. Creează `scripts/deploy_vps.sh`:
   rsync -avz --delete --exclude 'venv/' --exclude '.env' --exclude '__pycache__/' \
     ./ root@VPS_IP:/opt/nrankai-cloud/
   ssh root@VPS_IP "cd /opt/nrankai-cloud && source venv/bin/activate && \
     pip install -r requirements.txt --quiet && \
     python manage_db.py migrate 2>/dev/null || true && \
     sudo systemctl restart nrankai-cloud"

5. Fișier systemd `scripts/nrankai-cloud.service`:
   [Unit]
   Description=nrankai-cloud FastAPI
   After=network.target mariadb.service
   [Service]
   Type=simple
   User=www-data
   WorkingDirectory=/opt/nrankai-cloud
   Environment="PATH=/opt/nrankai-cloud/venv/bin"
   ExecStart=/opt/nrankai-cloud/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
   Restart=always
   RestartSec=5
   [Install]
   WantedBy=multi-user.target

6. `scripts/cloudpanel_nginx.conf`:
   location / {
     proxy_pass http://127.0.0.1:8000;
     proxy_set_header Host $host;
     proxy_set_header X-Real-IP $remote_addr;
     proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
     proxy_set_header X-Forwarded-Proto $scheme;
     proxy_read_timeout 300s;
   }
```

---

## PROMPT 8 — Environment-Aware Templates + Navigation

```
Repo: nrankai-cloud

1. În main.py, la inițializare Jinja2, adaugă globals:
   templates.env.globals.update({
     "APP_ENV": os.getenv("APP_ENV", "development"),
     "APP_BASE_URL": os.getenv("APP_BASE_URL", "http://localhost:8000"),
     "APP_NAME": "nrankai GEO Tools",
     "APP_VERSION": "3.0.0",
     "NAV_ITEMS": [
       {"name": "Dashboard",         "url": "/",              "icon": "home"},
       {"name": "Projects",          "url": "/projects",      "icon": "folder"},
       {"name": "Audits",            "url": "/audits",        "icon": "search"},
       {"name": "GEO Monitor",       "url": "/geo-monitor",   "icon": "globe"},
       {"name": "Fan-Out Analyzer",  "url": "/fanout",        "icon": "zap"},
       {"name": "Prompt Library",    "url": "/fanout/prompt-library", "icon": "book"},
       {"name": "Prospects",         "url": "/prospects",     "icon": "users"},
       {"name": "Content Gaps",      "url": "/content-gaps",  "icon": "file-text"},
     ]
   })

2. Actualizează `templates/base.html`:
   - Navigație generată dinamic din NAV_ITEMS
   - Active state: request.url.path == item.url
   - Footer: APP_NAME v APP_VERSION + ENV badge (DEV galben / PROD verde)
   - Meta robots: noindex, nofollow (tool intern)

3. Banner dev în fanout.html:
   {% if APP_ENV == "development" %}
   <div class="bg-yellow-900/50 text-yellow-200 text-center text-xs py-1">
     Development Mode — {{ APP_BASE_URL }}
   </div>
   {% endif %}

4. Verifică toate template-urile existente:
   - Relative paths pentru toate API calls
   - Consistency navigație
   - "Fan-Out Analyzer" și "Projects" în nav

NU rescrie template-urile de la zero — actualizează ce există.
```

---

# ═══════════════════════════════════════
# PHASE 2 — MULTI-ENGINE + DISCOVERY + TRACKING
# ═══════════════════════════════════════

## PROMPT 30 — Basic Auth: API Keys (rulează primul)

```
Repo: nrankai-cloud

IMPORTANT: Citește HARDENING_NOTES_MASTER_V1.md înainte.
Dacă auth e parțial implementat, extinde-l. Nu duplica ce există.

1. Tabel `api_keys`:
   CREATE TABLE api_keys (
     id VARCHAR(36) PRIMARY KEY,
     name VARCHAR(200) NOT NULL,
     key_hash VARCHAR(64) NOT NULL UNIQUE,   -- SHA256, niciodată plaintext
     key_prefix VARCHAR(8) NOT NULL,          -- primele 8 chars pentru identificare UI
     permissions JSON DEFAULT '["read","write"]',
     is_active BOOLEAN DEFAULT TRUE,
     last_used_at TIMESTAMP DEFAULT NULL,
     expires_at TIMESTAMP DEFAULT NULL,
     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
     INDEX idx_hash (key_hash),
     INDEX idx_active (is_active)
   );

2. Format cheie: nrk_{secrets.token_urlsafe(32)}
   Exemplu: nrk_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6
   Stocat: DOAR SHA256(full_key) + primele 8 chars
   Full key returnată O SINGURĂ DATĂ la creare.

3. Middleware `middleware/auth.py`:
   EXCLUDED_PATHS = ["/health", "/login", "/auth/login", "/docs", "/openapi.json"]
   
   Acceptă key din: header X-API-Key SAU cookie nrk_session (7 zile, HttpOnly Secure)
   Browser fără key → redirect /login
   API fără key → 401 JSON
   
   Validare: SHA256(key) → lookup WHERE key_hash=? AND is_active=True
             AND (expires_at IS NULL OR expires_at > NOW())
   last_used_at: asyncio.create_task (fire-and-forget, nu bloca request-ul)
   request.state.permissions = db_key.permissions

4. Pagina /login: form simplu → POST /auth/login → cookie → redirect /fanout

5. Endpoints admin (permissions=["admin"]):
   POST /api/admin/keys → returnează full_key O SINGURĂ DATĂ
   GET  /api/admin/keys → prefix + metadata (NICIODATĂ full_key sau hash)
   DELETE /api/admin/keys/{id} → is_active=False

6. Seed la primul start (api_keys gol):
   Generează admin key, loghează cu:
   logger.warning("=" * 60)
   logger.warning("FIRST RUN — Admin API key:")
   logger.warning(f"  {full_key}")
   logger.warning("Save this — NOT shown again!")
   logger.warning("=" * 60)

7. .env: DISABLE_AUTH=false (true pentru dev local)
   Production: dezactivează /docs și /redoc

Migrație: migrations/015_api_keys.sql
```

---

## PROMPT 29 — Dead Letter Queue + Retry Logic

```
Repo: nrankai-cloud

1. Extinde `fanout_tracking_runs` (dacă nu există deja din Prompt 11):
   ALTER TABLE fanout_tracking_runs
     ADD COLUMN retry_count INT DEFAULT 0,
     ADD COLUMN max_retries INT DEFAULT 3,
     ADD COLUMN next_retry_at TIMESTAMP DEFAULT NULL,
     ADD COLUMN failure_reason VARCHAR(500) DEFAULT NULL,
     ADD COLUMN is_dead_letter BOOLEAN DEFAULT FALSE,
     ADD INDEX idx_retry (status, next_retry_at, is_dead_letter);

2. `utils/retry_policy.py`:
   RETRYABLE = ["rate_limit", "timeout", "connection_error", "502", "503", "504"]
   NON_RETRYABLE = ["invalid_api_key", "insufficient_quota", "400", "model_not_found"]
   
   is_retryable(error) -> bool
   next_retry_delay(retry_count) -> int (minute):
     base = [30, 120, 480]  # retry 1: 30min | retry 2: 2h | retry 3: 8h
     jitter = random.randint(-5, 5)
     return base[min(retry_count, 2)] + jitter

3. Wrapper în tracking worker:
   try: run → status=completed
   except retryable AND count < max_retries:
     status=failed, retry_count++, next_retry_at=NOW()+delay
   except non-retryable OR max_retries:
     status=failed, is_dead_letter=True
     trigger webhook "tracking_run_failed"

4. check_and_run_due_trackings() selectează:
   - normale: status='pending' AND next_run_at <= NOW() AND is_dead_letter=False
   - retry: status='failed' AND next_retry_at <= NOW() AND retry_count < max_retries

5. Endpoints:
   GET  /api/fanout/tracking/dead-letters
   POST /api/fanout/tracking/dead-letters/{run_id}/retry   → resetează pentru retry manual
   POST /api/fanout/tracking/dead-letters/{run_id}/dismiss

6. Banner în orice pagină UI dacă is_dead_letter > 0:
   "⚠️ {N} tracking runs failed permanently. View Dead Letters →"

Migrație: migrations/016_dead_letter_queue.sql
```

---

## PROMPT 9 — Multi-Engine Fan-Out: Gemini + Perplexity Wrapper

```
Repo: nrankai-tool

IMPORTANT: Citește `perplexity_researcher.py` existent ÎNAINTE de a scrie cod.
- Identifică exact ce face: API calls, parametri, output format
- Nu rescrie logica existentă — wrap-o și extinde-o

1. Extinde `fanout_analyzer.py` cu arhitectura multi-engine:

   Clasa de bază `BaseFanoutAnalyzer`:
   - async def analyze_prompt(self, prompt: str, locale: str = "ro-RO") -> FanoutResult
   - property: engine_name -> str
   - property: cost_per_query -> float

   Redenumește/extinde clasa existentă `FanoutAnalyzer` → `ChatGPTFanoutAnalyzer(BaseFanoutAnalyzer)`
   model: gpt-4.1 (default), query_origin="actual", source_origin="citation"

2. Clasa `PerplexityFanoutAnalyzer(BaseFanoutAnalyzer)`:
   IMPORTANT: Wrapper peste logica din perplexity_researcher.py existent.
   Adaptează outputul la FanoutResult format.
   Perplexity NU expune queries interne → query_origin="inferred"
   Extrage queries inferate din response text (entități și topicuri menționate).
   source_origin="citation" (Perplexity returnează citations explicit)
   Model: sonar-pro, Cost: $0.015/1K output tokens

3. Clasa `GeminiFanoutAnalyzer(BaseFanoutAnalyzer)`:
   google-generativeai SDK, model: gemini-2.5-flash
   
   from google import generativeai as genai
   model = genai.GenerativeModel("gemini-2.5-flash")
   response = model.generate_content(
       prompt,
       tools=[{"google_search": {}}]
   )
   
   Extrage din grounding_metadata:
   - web_search_queries → fanout_queries (query_origin="actual" — Gemini le expune)
   - grounding_chunks → sources (source_origin="grounding")
   Cost: ~$0.0001/query

4. Clasa `MultiEngineFanoutAnalyzer`:
   engines: List[str] = ["chatgpt", "gemini"]
   Rulează în paralel cu asyncio.gather
   Engine cu API key lipsă → skip cu warning, nu crash
   
   Returnează MultiEngineResult:
   {
     prompt, engines: {chatgpt: FanoutResult, gemini: FanoutResult},
     combined_sources,    # deduped
     combined_queries,    # deduped
     source_overlap: {
       all_engines: [...],
       unique_per_engine: {chatgpt: [...], gemini: [...]}
     },
     engine_agreement_score: float,   # 0-100
     total_cost_usd: float
   }

5. __main__:
   python fanout_analyzer.py "best seo agency romania" --engines chatgpt,gemini
   python fanout_analyzer.py "best seo agency romania" --engines chatgpt,gemini,perplexity --locale ro-RO

Dependențe noi: google-generativeai
.env.example: GOOGLE_API_KEY=
```

---

## PROMPT 10 — Prompt Discovery Module

```
Repo: nrankai-tool

Creează `prompt_discovery.py`.

Concept: descoperi CARE prompturi trigger-uiesc menționarea brandului/site-ului.

1. Clasa `PromptDiscovery`:
   __init__(self, target_domain, target_brand, category, location=None)

   def generate_candidate_prompts(self, count=50) -> List[str]:
     Template-uri per categorie (reutilizează din PromptLibrary dacă există):
     TEMPLATES = {
       "seo_agency": [
         "best seo agency in {city}", "top seo companies {country} {year}",
         "how much does seo cost {city}", "seo agency vs in-house seo",
         "best seo agency reviews {city}", "is {brand} a good seo agency",
         "alternatives to {brand} seo", "seo agency pricing {country}",
         "best seo tools {year}", "how to choose an seo agency",
       ],
       "beauty_clinic": [
         "best botox clinic in {city}", "laser hair removal cost {city}",
         "top rated med spa near me", "botox vs fillers which is better",
         "best aesthetic clinic reviews {city}", "how much does coolsculpting cost {city}",
         "is {brand} good for skin treatments", "med spa vs dermatologist",
       ],
       "dental_clinic": [
         "best dentist in {city}", "dental implants cost {city}",
         "teeth whitening clinic {city}", "emergency dentist near me",
       ],
       "restaurant": [
         "best restaurants in {city} {year}", "romantic dinner {city}",
         "best {cuisine} restaurant {city}", "fine dining {city}",
       ],
       "saas": [
         "best {category} software {year}", "{brand} vs {competitor}",
         "{brand} pricing and plans", "alternatives to {brand}",
         "{brand} reviews and complaints",
       ],
       "law_firm": [
         "best {practice_area} lawyer {city}", "top law firms {city}",
         "how much does a {practice_area} attorney cost",
       ],
       "real_estate": [
         "best real estate agent {city}", "apartments for rent {city} {year}",
         "houses for sale {city}", "real estate agency reviews {city}",
       ],
       "generic": [
         "best {category} in {city}", "top {category} companies {country}",
         "{brand} reviews", "how much does {service} cost",
         "{brand} alternatives", "is {brand} worth it",
         "{category} near me", "{brand} vs {competitor}",
       ]
     }
     Înlocuiește: {city}, {country}, {brand}, {year}, {category}, {service}
     {year} = anul curent, {competitor} = sinonime ale categoriei

   async def discover(self, engines=["chatgpt"], max_prompts=20) -> DiscoveryResult:
     Rulează MultiEngineFanoutAnalyzer pe fiecare prompt.
     Verifică dacă target_domain apare în sources.
   
   async def quick_discover(self, engines=["chatgpt"], count=5) -> DiscoveryResult:
     Ia top 5 prompturi: best_of + pricing + comparison.
     Ideal pentru prospect scoring rapid.

2. DiscoveryResult:
   {
     target_domain, target_brand, prompts_tested, prompts_with_mention,
     mention_rate: float,
     mentioned_in: [{prompt, engines, position_per_engine, total_sources}],
     not_mentioned_in: [{prompt, engines_tested, top_competitors_instead}],
     strongest_prompts: [...],
     weakest_prompts: [...],
     competitor_dominance: {domain: {appearances, avg_position}},
     total_cost_usd: float
   }

3. Cost estimator:
   print(f"≈ ${cost:.2f} for {max_prompts} prompts × {len(engines)} engines. Continue? [y/N]")

4. __main__:
   python prompt_discovery.py --domain example.com --brand "Example" \
     --category seo_agency --location "Bucharest, Romania" --engines chatgpt,gemini
   python prompt_discovery.py --domain example.com --brand "Example" \
     --category beauty_clinic --location "Miami, FL" --quick
```

---

## PROMPT 11 — Historical Tracking: Fan-Out Timeline

```
Repo: nrankai-cloud

IMPORTANT: Citește `history_tracker.py` din root-ul nrankai-tool ÎNAINTE.
- Identifică ce face: ce urmărește, cum stochează (JSON/SQLite/DB)
- Dacă face deja tracking pe DB: adaugă tabele noi, nu rescrie
- Dacă e file-based: creează tracking nou în DB, separat

1. Tabel `fanout_tracking_configs`:
   CREATE TABLE fanout_tracking_configs (
     id VARCHAR(36) PRIMARY KEY,
     name VARCHAR(200) NOT NULL,
     target_domain VARCHAR(500),
     target_brand VARCHAR(200),
     prompts JSON NOT NULL,
     engines JSON DEFAULT '["chatgpt"]',
     schedule VARCHAR(20) DEFAULT 'weekly',
     is_active BOOLEAN DEFAULT TRUE,
     last_run_at TIMESTAMP DEFAULT NULL,
     next_run_at TIMESTAMP DEFAULT NULL,
     project_id VARCHAR(36) DEFAULT NULL,
     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
     INDEX idx_active_schedule (is_active, next_run_at)
   );

2. Tabel `fanout_tracking_runs`:
   CREATE TABLE fanout_tracking_runs (
     id VARCHAR(36) PRIMARY KEY,
     config_id VARCHAR(36) NOT NULL,
     run_date DATE NOT NULL,
     total_prompts INT,
     mention_rate FLOAT,
     avg_source_position FLOAT,
     total_unique_sources INT,
     composite_score FLOAT DEFAULT NULL,
     score_breakdown JSON DEFAULT NULL,
     sentiment_breakdown JSON DEFAULT NULL,
     top_competitors JSON,
     model_version VARCHAR(50) DEFAULT NULL,
     baseline_mention_rate FLOAT DEFAULT NULL,
     cost_usd FLOAT DEFAULT 0,
     retry_count INT DEFAULT 0,
     max_retries INT DEFAULT 3,
     next_retry_at TIMESTAMP DEFAULT NULL,
     failure_reason VARCHAR(500) DEFAULT NULL,
     is_dead_letter BOOLEAN DEFAULT FALSE,
     status ENUM('pending','running','completed','failed') DEFAULT 'pending',
     error_message TEXT,
     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
     FOREIGN KEY (config_id) REFERENCES fanout_tracking_configs(id),
     INDEX idx_config_date (config_id, run_date),
     UNIQUE KEY uk_config_date (config_id, run_date)
   );

3. Tabel `fanout_tracking_details`:
   CREATE TABLE fanout_tracking_details (
     id INT AUTO_INCREMENT PRIMARY KEY,
     run_id VARCHAR(36) NOT NULL,
     prompt TEXT NOT NULL,
     prompt_cluster VARCHAR(50) DEFAULT NULL,
     engine VARCHAR(50),
     query_origin VARCHAR(20) DEFAULT 'actual',
     target_found BOOLEAN DEFAULT FALSE,
     source_position INT,
     fanout_query_count INT,
     source_count INT,
     session_id VARCHAR(36),
     FOREIGN KEY (run_id) REFERENCES fanout_tracking_runs(id) ON DELETE CASCADE,
     INDEX idx_run (run_id)
   );

4. Worker `workers/fanout_tracker_worker.py`:
   async def run_tracking_with_retry(config_id, db):
     Folosește analyze_prompt_cached (din Prompt 16) cu ttl_mode=config.schedule.
     La success: calculează composite_score, salvează în runs + details.
     La failure: aplică retry logic din Prompt 29.
   
   next_run_at: daily=+1DAY | weekly=+7DAY | monthly=+1MONTH
   model_drift_detected: True dacă model_version diferă față de run precedent.
   
   async def check_and_run_due_trackings(db):
     normale: status='pending' AND next_run_at<=NOW() AND is_dead_letter=False
     retry: status='failed' AND next_retry_at<=NOW() AND retry_count<max_retries

5. Endpoints:
   POST /api/fanout/tracking              → creare config
   GET  /api/fanout/tracking              → lista configs
   GET  /api/fanout/tracking/{id}/timeline?period=30d
     Response: {config, timeline, trend, change_vs_first, model_drift_detected}
   POST /api/fanout/tracking/{id}/run-now
   GET  /api/fanout/tracking/dead-letters
   POST /api/fanout/tracking/dead-letters/{id}/retry
   POST /api/fanout/tracking/dead-letters/{id}/dismiss

6. APScheduler în main.py: verificare la fiecare 15 minute.

Migrație: migrations/008_fanout_tracking.sql
```

---

## PROMPT 12 — Competitive Fan-Out Comparison

```
Repo: nrankai-cloud

Creează `workers/fanout_competitive.py`.

1. Funcție `compare_competitors(prompts, competitors, engines, db) -> CompetitiveReport`:
   competitors = max 5 domenii inclusiv target
   
   CompetitiveReport:
   {
     prompts_analyzed, engines_used,
     competitors: {
       "mysite.com": {
         mention_rate, avg_position,
         appeared_in_prompts, missing_from_prompts,
         best_prompt, worst_gap
       }
     },
     head_to_head: [
       {prompt, cluster, results: {domain: {found, position}}, winner}
     ],
     overall_ranking: [{domain, score, rank}],
     recommendations: [str],
     total_cost_usd: float
   }

2. Funcție `generate_competitive_recommendations(report) -> List[str]`:
   - Competitor > 70% dominance: alert
   - Gap pe cluster tip (price, review, comparison): content gap
   - Bun pe branded, slab pe generic: authority gap
   - Competitor apare doar pe un engine: engine opportunity

3. Tabel `fanout_competitive_reports`:
   id, target_domain, project_id, competitors JSON, report JSON, created_at

4. Endpoints:
   POST /api/fanout/competitive
   Body: {prompts, competitors (max 5), engines, target_domain, project_id}
   Afișează cost estimate: "prompts × engines × {N competitors} ≈ $X"
   
   GET /api/fanout/competitive/{report_id}

Migrație: inclus în 008 sau migrations/008b_competitive.sql
```

---

## PROMPT 13 — Enhanced UI: Timeline + Competitive + Discovery

```
Repo: nrankai-cloud

Actualizează `templates/fanout.html` — tab-urile noi sunt deja definite în Prompt 5.
Verifică ce există deja și completează ce lipsește.

Asigură-te că toate tab-urile funcționează:
- Multi-Engine: Venn SVG + tabel overlap + agreement gauge
- Prompt Discovery: mention rate bar + tabel split + competitor dominance chart + export
- Timeline: Chart.js line chart + trend badge + benchmark linie + modal create tracking
- Competitive: textarea competitors + ranking table + head-to-head matrix

Toate charturile: Chart.js (deja inclus).
Loading states cu spinners pe fiecare tab.
Export buttons: window.location redirect (nu fetch).
```

---

## PROMPT 14 — Smart Prompt Templates + Auto-Discovery

```
Repo: nrankai-tool

Extinde `prompt_discovery.py` cu LLM-powered prompt generation.

1. Funcție `generate_smart_prompts(target_domain, target_brand, category, location, count=30) -> List[str]`:
   
   a) Scrape homepage cu httpx + BeautifulSoup:
      title, meta description, H1, H2-uri, servicii menționate, USP-uri
   
   b) Claude API pentru generare:
      Model: claude-haiku-4-5-20251001 (~$0.001 per call)
      
      System: "Generate {count} realistic prompts real users would type into
               ChatGPT/Perplexity/Google AI about this business.
               Mix: informational + commercial + navigational intents.
               Include: branded, non-branded, comparison, local, pricing, review, problem-solution.
               Include negative prompts ('complaints', 'is X worth it').
               Short (3-5 words) AND conversational (full sentences).
               Return ONLY a JSON array of strings."
      
      User: "Brand: {brand} | Domain: {domain} | Category: {category}
             Location: {location} | Services: {extracted_services}
             Title: {title} | USPs: {usps}
             Generate {count} prompts."
      
      Parse JSON, strip markdown fences dacă există.
   
   c) Merge cu generate_candidate_prompts() output, deduplicare.

2. Funcție `auto_discover_category(url) -> str`:
   Scrape → keyword matching:
   "beauty|salon|spa|botox" → "beauty_clinic"
   "dental|dentist|teeth"   → "dental_clinic"
   "seo|marketing|agency"   → "seo_agency"
   "restaurant|food|menu"   → "restaurant"
   "law|attorney|legal"     → "law_firm"
   "real estate|property"   → "real_estate"
   "software|saas|platform" → "saas"
   Fallback: "generic"

3. Funcție `extract_competitors_from_fanout(fanout_results, target_domain) -> List[str]`:
   Top 10 domenii frecvente din sources.
   Exclude: target, wikipedia, youtube, reddit, yelp, google, facebook,
            linkedin, twitter, instagram, amazon, tripadvisor

4. __main__:
   python prompt_discovery.py --domain example.com --brand "Example" --smart --count 30

Dependențe noi: beautifulsoup4, httpx (dacă nu există)
```

---

# ═══════════════════════════════════════
# PHASE 3 — ENRICHMENT + INTEGRĂRI
# ═══════════════════════════════════════

## PROMPT 15 — Schema Enrichment

```
Repo: nrankai-cloud

1. Migrație `migrations/009_schema_enrichment.sql`:
   (fanout_sessions deja are câmpurile noi din Prompt 2 — verifică ce lipsește și adaugă)
   Adaugă dacă nu există:
   ALTER TABLE fanout_sessions
     ADD COLUMN IF NOT EXISTS query_origin ENUM('actual','inferred','generated') DEFAULT 'actual',
     ADD COLUMN IF NOT EXISTS source_origin ENUM('citation','grounding','extracted') DEFAULT 'citation',
     ADD COLUMN IF NOT EXISTS prompt_cluster VARCHAR(50) DEFAULT NULL,
     ADD COLUMN IF NOT EXISTS run_cost_usd DECIMAL(8,6) DEFAULT 0.000000,
     ADD COLUMN IF NOT EXISTS locale VARCHAR(20) DEFAULT 'ro-RO',
     ADD COLUMN IF NOT EXISTS language VARCHAR(10) DEFAULT 'ro',
     ADD COLUMN IF NOT EXISTS confidence_score FLOAT DEFAULT NULL,
     ADD COLUMN IF NOT EXISTS engine VARCHAR(50) DEFAULT 'chatgpt',
     ADD COLUMN IF NOT EXISTS model_version VARCHAR(50) DEFAULT 'gpt-4.1';

2. Cost calculation în save_fanout_result():
   COST_PER_1K_TOKENS = {
     "gpt-4.1":          {"input": 0.002,    "output": 0.008},
     "gpt-4.1-mini":     {"input": 0.0001,   "output": 0.0004},
     "gemini-2.5-flash": {"input": 0.000075, "output": 0.0003},
     "sonar-pro":        {"input": 0.003,    "output": 0.015},
   }

3. classify_prompt_cluster() importat din fanout_analyzer.py, rulat la save.

4. GET /api/fanout/sessions acceptă filtre: ?cluster=&engine=&locale=&query_origin=
   Response include aggregation: by_cluster, by_engine, total_cost_usd, avg_mention_rate

Sesiunile vechi fără câmpuri noi: DEFAULT values acoperă, fără erori.
```

---

## PROMPT 16 — Caching Layer

```
Repo: nrankai-cloud

1. Tabel `fanout_cache`:
   CREATE TABLE fanout_cache (
     id INT AUTO_INCREMENT PRIMARY KEY,
     cache_key VARCHAR(64) NOT NULL UNIQUE,
     prompt_hash VARCHAR(64) NOT NULL,
     engine VARCHAR(50) NOT NULL,
     model VARCHAR(100) NOT NULL,
     locale VARCHAR(20) DEFAULT 'ro-RO',
     result_json MEDIUMTEXT NOT NULL,
     hit_count INT DEFAULT 0,
     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
     expires_at TIMESTAMP NOT NULL,
     INDEX idx_expires (expires_at),
     INDEX idx_lookup (prompt_hash, engine, model, locale)
   );

2. `utils/fanout_cache.py` — clasa FanoutCache:
   TTL: adhoc=4h | daily=20h | weekly=160h | monthly=700h
   cache_key = SHA256(f"{prompt.lower().strip()}|{engine}|{model}|{locale}")
   
   async def get(db, cache_key) -> FanoutResult | None:
     WHERE cache_key=? AND expires_at > NOW() → hit_count++, deserialize, from_cache=True
   
   async def set(db, ..., ttl_mode):
     INSERT ON DUPLICATE KEY UPDATE result_json=..., expires_at=..., hit_count=hit_count+1
   
   async def get_stats(db):
     total_entries, active, total_hits, estimated_cost_saved_usd

3. Wrapper `analyze_prompt_cached(prompt, locale, ttl_mode, db)` în BaseFanoutAnalyzer.
4. Tracking runs folosesc cache cu ttl_mode=config.schedule.
5. GET /api/fanout/cache/stats
6. DELETE /api/fanout/cache?engine=chatgpt (sau fără params → cleanup expired)

Cleanup job zilnic.
Migrație: migrations/010_fanout_cache.sql
```

---

## PROMPT 17 — Export JSON/CSV + Client Report Text

```
Repo: nrankai-cloud

1. `utils/fanout_export.py` — clasa FanoutExporter:
   
   session_to_csv(session) -> str:
     3 secțiuni: Session info | Fan-out queries | Sources
     UTF-8 BOM: output.write('\ufeff')
   
   session_to_json(session) -> dict:
     Complet + export_meta: {generated_at, tool, version}
   
   tracking_timeline_to_csv(config, timeline) -> str:
     Date, Mention Rate, Composite Score, Avg Position, Sources, Cost, Trend
   
   competitive_report_to_csv(report) -> str:
     Ranking + head-to-head matrix
   
   discovery_result_to_csv(result) -> str:
     Prompt, Mentioned Y/N, Engines, Position, Top Competitor
   
   generate_client_report_text(brand, domain, discovery, timeline, competitive) -> str:
     Plain text structurat gata de email:
     Executive Summary | Top Performing Prompts | Gaps | Recommendations

2. Endpoints StreamingResponse:
   GET /api/fanout/sessions/{id}/export?format=json|csv
   GET /api/fanout/tracking/{id}/export?format=csv&period=90d
   GET /api/fanout/competitive/{id}/export?format=csv
   POST /api/fanout/export/client-report → text/plain
   Content-Disposition: attachment; filename="fanout_{id}_{date}.{ext}"

3. UI: butoane [↓ JSON] [↓ CSV] [↓ Client Report] pe rezultate.
   Butoanele = window.location redirect.

DOAR stdlib: csv, json, io. Niciun pandas/openpyxl.
```

---

## PROMPT 18 — WLA Cross-Reference Real

```
Repo: nrankai-cloud

IMPORTANT: Citește `cross_reference_analyzer.py` din nrankai-tool ÎNAINTE.
Identifică: ce date compară, output format, schema tabelelor WLA existente.
Extinde logica existentă, nu o rescrie.

Creează `workers/fanout_wla_crossref.py`:

async def analyze(fanout_session_id, wla_audit_id, target_domain, db) -> CrossRefResult:

a) Extrage fan-out queries din fanout_queries WHERE session_id=?

b) Extrage din audit WLA: URL, title, meta_description, H1 ale paginilor crawlate.
   Verifică exact câmpurile din schema WLA existentă.

c) Clasificare per query:
   tokenizează → set termeni (fără stop words ro+en)
   STOP_WORDS = ["the","a","an","and","or","for","in","on","at","to","is","are",
                 "de","la","în","și","sau","pe","cu","că","o","un"]
   
   overlap = len(query_tokens ∩ page_tokens) / len(query_tokens)
   COVERED:  overlap >= 0.6 cu title/H1/URL
   PARTIAL:  overlap >= 0.3 cu orice câmp
   GAP:      sub 0.3

d) CrossRefResult:
   {
     coverage_score, retrieval_readiness_score,
     covered_queries, partial_queries,
     gap_queries: [{
       query, status, suggested_content_type,
       priority: high|medium|low, competitor_who_covers
     }],
     quick_wins, action_cards: [{
       type, priority, title, rationale, queries_covered, estimated_impact
     }]
   }
   
   Priority: HIGH dacă query în >3 sesiuni SAU competitor dominant
              MEDIUM dacă în 2 sesiuni | LOW: o singură dată

e) Dacă SERP validation există pentru sesiune:
   gap + serp_found → "Optimize for AI retrieval" (pagina există, GEO gap)
   gap + serp_not_found → "Create new content" (lipsești din ambele)

Tabel `fanout_crossref_results`:
id, session_id, audit_id, project_id, target_domain, result_json, created_at

Endpoints:
POST /api/fanout/crossref
GET  /api/fanout/crossref/{id}

UI: buton "🔗 Cross-Reference with WLA Audit" în session detail.
Modal: dropdown audit selection → results 3 coloane + Action Cards.

Migrație: migrations/011_fanout_crossref.sql
Token overlap simplu, NU embeddings.
```

---

## PROMPT 19 — SERP Validation via Serper.dev

```
Repo: nrankai-cloud

1. `utils/serp_validator.py` — clasa SERPValidator:
   SERPER_ENDPOINT = "https://google.serper.dev/search"
   
   async def check_ranking(query, target_domain, gl="ro", hl="ro", num=10) -> SERPResult:
     POST cu X-API-KEY: {SERPER_API_KEY}
     Body: {"q": query, "gl": gl, "hl": hl, "num": num, "autocorrect": false}
     Extrage: organic positions, featured snippet, people_also_ask
     Returnează: target_found, target_position, featured_snippet_domain,
                 people_also_ask, top_10_domains
   
   async def validate_batch(queries, target_domain, gl, hl, max_queries=20):
     asyncio.sleep(0.2) între calls (max 5/s Serper.dev)

2. Tabel `fanout_serp_validation`:
   session_id, query_text, target_domain, target_found, target_position,
   has_featured_snippet, featured_snippet_domain, top_10_domains JSON,
   people_also_ask JSON, gl, hl, cost_usd=0.001, validated_at

3. POST /api/fanout/sessions/{id}/validate-serp:
   Body: {target_domain, gl, hl, max_queries}
   Response cu 4 categorii:
   SYNCED:     ai_found=T + serp_found=T → ideal
   AI_GAP:     ai_found=F + serp_found=T → GEO optimization needed (PRIORITATE)
   AI_ONLY:    ai_found=T + serp_found=F → SEO opportunity
   DOUBLE_GAP: ai_found=F + serp_found=F → create content
   
   Metrică: traffic_at_risk = sum clicks pentru AI_GAP queries
   Bonus: people_also_ask colectat din toate queries

Cost estimator: "$0.001 × {n} queries ≈ ${total}. Continue?"
Dacă SERPER_API_KEY lipsă: 422 cu mesaj clar.
Migrație: migrations/012_fanout_serp.sql
```

---

## PROMPT 20 — n8n Integration: Webhook System

```
Repo: nrankai-cloud

1. Tabel `fanout_webhooks`:
   id, name, webhook_url, events JSON, is_active, secret_key, created_at

2. Tabel `fanout_webhook_logs`:
   id, webhook_id, event_type, status (success|failure), error VARCHAR(500),
   payload_size INT, sent_at, response_code INT

3. `utils/webhook_sender.py`:
   async def send(webhook_url, event_type, payload, secret_key=None):
     body = {event, timestamp ISO, tool: "nrankai-fanout-analyzer", data: payload}
     HMAC: headers["X-Webhook-Signature"] = f"sha256={hmac_sha256(secret_key, json(body))}"
     httpx timeout=10s, loghează în webhook_logs indiferent de rezultat
   
   async def send_to_all(db, event_type, payload):
     SELECT WHERE is_active=True AND JSON_CONTAINS(events, '"event_type"')
     try send per webhook, except → log, NU bloca flow-ul principal

4. Events și triggers:
   "tracking_run_completed" → mention_rate, composite_score, trend, report_url
   "mention_rate_drop"      → dacă rate < previous * 0.85: drop_pct, alert_level="warning"
   "mention_rate_spike"     → dacă rate > previous * 1.20
   "new_competitor_detected" → domeniu nou în top_competitors vs run precedent
   "tracking_run_failed"    → dead letter: retry_count, failure_reason
   "discovery_completed"    → mention_rate, strongest_prompt, competitor_dominance
   "entity_check_completed" → entity_authority_score, missing_sources

5. Endpoints:
   POST   /api/fanout/webhooks
   GET    /api/fanout/webhooks
   DELETE /api/fanout/webhooks/{id}   → is_active=False
   POST   /api/fanout/webhooks/{id}/test → payload de test

6. .env: N8N_WEBHOOK_URL=http://proxmox-ip:5678/webhook/fanout
   Dacă setat, creează webhook default la startup cu toate events active.

Migrație: migrations/013_fanout_webhooks.sql
```

---

## PROMPT 21 — Prompt Library

```
Repo: nrankai-cloud

1. Tabel `fanout_prompt_library`:
   id, prompt_text, prompt_hash VARCHAR(64) UNIQUE (SHA256),
   vertical, cluster, language, locale, tags JSON,
   is_template BOOLEAN, template_vars JSON,
   times_used INT, avg_fanout_queries FLOAT, avg_mention_rate FLOAT,
   avg_source_count FLOAT,
   performance_tier ENUM('high','medium','low','untested') DEFAULT 'untested',
   created_at, last_used_at,
   INDEX idx_vertical, idx_cluster, idx_performance

2. Seed în migrație (INSERT): 60+ prompturi din toate verticalele:
   seo_agency (12), beauty_clinic (12), dental_clinic (6),
   restaurant (6), saas (8), real_estate (6), law_firm (5), generic (10+)

3. `utils/prompt_library.py` — clasa PromptLibrary:
   get_for_discovery(db, vertical, brand=None, city=None, count=20) -> List[str]:
     Selectează, înlocuiește placeholders {brand} {city} {year}
     Prioritizează: high > medium > untested > low
   
   record_usage(db, prompt_hash, fanout_result, mention_rate=None):
     Running average: avg = (avg * n + new) / (n + 1)
     UPDATE performance_tier: >=60% high | >=30% medium | <30% low
   
   suggest_gaps(db, vertical, existing_prompts) -> List[str]:
     Prompts din library care lipsesc din existing_prompts
   
   add_from_discovery(db, discovery_result, vertical):
     Adaugă prompts cu mention_rate > 0, check pe hash

4. Integrare cu PromptDiscovery (Prompt 10):
   generate_candidate_prompts() → PromptLibrary.get_for_discovery() ca sursă primară
   Templates hardcodate = fallback dacă library e goală

5. Endpoints:
   GET  /api/fanout/prompt-library?vertical=&cluster=&performance=&limit=
   GET  /api/fanout/prompt-library/suggest?vertical=&existing=
   POST /api/fanout/prompt-library
   GET  /api/fanout/prompt-library/stats

6. UI tab simplu: filtere + tabel cu [+ Add to Discovery] + [+ Add Custom]

Migrație: migrations/014_prompt_library.sql (include seed INSERT-uri)
```

---

# ═══════════════════════════════════════
# PHASE 4 — ANALYTICS + INTEGRĂRI AVANSATE
# ═══════════════════════════════════════

## PROMPT 25 — Client/Project Management

```
Repo: nrankai-cloud

1. Tabel `projects`:
   CREATE TABLE projects (
     id VARCHAR(36) PRIMARY KEY,
     name VARCHAR(200) NOT NULL,
     client_name VARCHAR(200) DEFAULT NULL,
     target_domain VARCHAR(500) NOT NULL,
     target_brand VARCHAR(200) NOT NULL,
     vertical VARCHAR(100) DEFAULT 'generic',
     locale VARCHAR(20) DEFAULT 'ro-RO',
     language VARCHAR(10) DEFAULT 'ro',
     gl VARCHAR(5) DEFAULT 'ro',
     color VARCHAR(7) DEFAULT '#6366f1',
     notes TEXT DEFAULT NULL,
     is_active BOOLEAN DEFAULT TRUE,
     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
     updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
   );

2. ALTER TABLE pe toate tabelele existente (adaugă project_id):
   fanout_sessions, fanout_tracking_configs, fanout_crossref_results,
   fanout_serp_validation, fanout_competitive_reports
   FOREIGN KEY → projects(id), INDEX pe fiecare.
   DEFAULT NULL — sesiunile vechi rămân valide.

3. Endpoints `routes/projects.py`:
   POST   /api/projects
   GET    /api/projects?is_active=&vertical=         → cu ProjectStats per proiect
   GET    /api/projects/{id}                         → complet cu stats
   GET    /api/projects/{id}/dashboard               → KPIs + recent + timeline + gaps
   PUT    /api/projects/{id}
   DELETE /api/projects/{id}                         → soft delete
   POST   /api/projects/{id}/quick-analyze           → fan-out cu project settings

4. ProjectStats per proiect:
   total_sessions, active_tracking_configs, last_tracking_run,
   latest_mention_rate, latest_composite_score, trend

5. UI `templates/projects.html`:
   /projects — grid cards:
   Color avatar + brand + domain + vertical badge + mention rate + composite score + trend arrow
   Buton [+ New Project]
   
   /projects/{id} — project dashboard:
   Header: brand, domain, vertical, culoare
   4 KPI cards: Mention Rate | Avg Position | Composite Score | Sessions Count
   Mini timeline Chart.js — ultimele 30 zile
   Tabel recent sessions + tracking configs cu [Run Now]
   Top 3 content gaps cu priority
   Butoane: [New Analysis] [New Tracking] [Export Client Report] [Entity Audit] [Bot Audit]

6. Actualizează navigație base.html: "Projects" după Dashboard.

7. POST /api/fanout/analyze, /tracking, /competitive:
   Acceptă project_id → preia locale/language/gl din project.

Migrație: migrations/017_projects.sql
```

---

## PROMPT 22 — GEO Composite Score

```
Repo: nrankai-cloud

1. `utils/geo_composite_score.py`:
   
   Formula:
   score = (
     mention_rate_pct        * 0.30 +
     position_score          * 0.25 +
     engine_coverage_score   * 0.20 +
     cluster_diversity_score * 0.15 +
     trend_score             * 0.10
   )
   
   position_score = max(0, 100 - (avg_position - 1) * 10)
   engine_coverage = engines_with_mention / total_engines * 100
   cluster_diversity = clusters_with_mention / clusters_tested * 100
   trend: no_data=50 | improving(>5%)=100 | stable(±5%)=50 | declining(>5%)=0
   
   Grades: 90-100 Excellent | 70-89 Good | 50-69 Fair | 30-49 Weak | 0-29 Poor
   
   @dataclass CompositeScoreBreakdown:
     total_score, grade, all components, trend, previous_score, score_change

2. Salvare în tracking_runs:
   ALTER TABLE fanout_tracking_runs
     ADD COLUMN composite_score FLOAT DEFAULT NULL,
     ADD COLUMN score_breakdown JSON DEFAULT NULL;
   Calculează și salvează la finalul fiecărui run.

3. Endpoints:
   GET /api/fanout/sessions/{id}/composite-score
   GET /api/fanout/tracking/{id}/score-history

4. UI:
   Gauge SVG semicircle colorat (culoare = grade)
   Breakdown componente sub gauge
   Project dashboard: KPI card cu score + trend arrow
   Tracking chart: linie composite_score over time

Migrație: migrations/018_composite_score.sql
```

---

## PROMPT 23 — Sentiment Analysis

```
Repo: nrankai-cloud

1. `utils/sentiment_analyzer.py`:
   Extrage propozițiile din răspunsul AI cu brandul menționat.
   0 mențiuni → sentiment="not_mentioned"
   
   Claude API: claude-haiku-4-5-20251001 (~$0.002/sesiune)
   
   System: "Analyze brand mentions in AI response. Return ONLY JSON:
   {sentiment: positive|neutral|negative|mixed, confidence: 0-1,
    brand_mentions: [{text, sentiment, context_type: recommendation|comparison|warning|neutral_mention|complaint}],
    summary: str}
   POSITIVE=recommended | NEUTRAL=factual | NEGATIVE=criticized | MIXED=both"

2. Tabel `fanout_sentiment`:
   session_id UNIQUE, overall_sentiment ENUM, confidence,
   brand_mention_count, mentions_json, summary, analyzed_at

3. Tracking runs: rulează automat pentru sessions cu target_found=True.
   ALTER TABLE fanout_tracking_runs:
     ADD sentiment_breakdown JSON, ADD dominant_sentiment VARCHAR(20)

4. Endpoints:
   POST /api/fanout/sessions/{id}/analyze-sentiment
   GET  /api/fanout/sessions/{id}/sentiment
   GET  /api/fanout/tracking/{id}/sentiment-trend

5. UI:
   Pills: 🟢 Positive | ⚪ Neutral | 🔴 Negative | 🟡 Mixed
   Stacked bar chart per tracking run
   Alert dacă negative > 20%

Manual pe sesiuni ad-hoc, AUTOMAT pe tracking runs.
Migrație: migrations/019_sentiment.sql
```

---

## PROMPT 24 — GEO Benchmark per Vertical

```
Repo: nrankai-cloud

IMPORTANT: Citește `api/routes/benchmarks.py` și schema DB existentă.
Creează tabel NOU `geo_benchmarks` — separat de benchmarks WLA existente.
Nu suprascrie ce există.

1. Tabel `geo_benchmarks`:
   vertical, locale, period_month VARCHAR(7) (ex: "2026-04"),
   sample_size INT, avg_mention_rate, median_mention_rate,
   p25_mention_rate, p75_mention_rate, avg_composite_score,
   UNIQUE KEY (vertical, locale, period_month)

2. `workers/benchmark_calculator.py`:
   calculate_geo_benchmarks(db, period_month=None):
   Grupează proiecte pe (vertical, locale), minim 3 pentru anonimizare.
   Ultimul tracking run completat per proiect din luna curentă.
   Calculează cu stdlib statistics: mean, median + percentile custom.
   INSERT ON DUPLICATE KEY UPDATE.

3. Rulează zilnic via APScheduler.

4. Endpoints:
   GET /api/benchmarks/geo/{vertical}/{locale}
   GET /api/projects/{id}/benchmark-comparison
   POST /api/admin/benchmarks/geo/recalculate
   
   comparison response: percentile_rank, grade, gap_to_top, message
   sample_size < 3 → {"available": false, "reason": "insufficient_data"}

5. UI project dashboard:
   "Better than {percentile}% of {vertical} in {locale}"
   Bar: ----[p25]----[median]----★----[p75]----
   Tracking chart: linie referință = benchmark median

Migrație: migrations/020_geo_benchmarks.sql
```

---

## PROMPT 31 — Entity Presence Checker

```
Repo: nrankai-cloud

1. `utils/entity_checker.py` — toate checks în paralel (asyncio.gather), timeout 10s:

   check_wikipedia(brand) → EntityCheck:
   GET wikipedia.org/api/rest_v1/page/summary/{brand} (EN + RO variant)
   found, url, description, quality_score
   
   check_wikidata(brand, domain) → EntityCheck:
   SPARQL pe query.wikidata.org:
   SELECT ?item WHERE { ?item wdt:P856 <https://domain.com> }
   found, wikidata_id, properties_count
   
   check_schema_markup(domain) → EntityCheck:
   httpx GET homepage, BeautifulSoup
   Caută <script type="application/ld+json">
   has_schema, schema_types, has_same_as, same_as_urls
   schema_quality: good (Organization+sameAs) | basic (Organization) | missing
   
   check_crunchbase(brand, domain) → EntityCheck:
   Serper.dev: "site:crunchbase.com {brand}" → found, url
   Skip dacă SERPER_API_KEY lipsă
   
   check_google_knowledge_panel(brand) → EntityCheck:
   Serper.dev → knowledgeGraph în response
   has_knowledge_panel, panel_type, panel_title
   
   check_linkedin(brand) → EntityCheck:
   Serper.dev: "site:linkedin.com/company {brand}" → found, url

2. entity_authority_score (0-100):
   wikipedia_en: +25 | wikipedia_ro: +10 | wikidata: +20
   knowledge_panel: +20 | schema_good: +15 | schema_basic: +7
   crunchbase: +5 | linkedin: +5

3. Recomandări automate:
   wikipedia not found → "Create Wikipedia article — highest impact for AI entity recognition"
   wikidata not found  → "Add to Wikidata — LLMs use Wikidata extensively"
   schema missing      → "Add Organization schema markup to {domain}"
   schema no sameAs    → "Add sameAs links pointing to Wikipedia/Wikidata"
   no knowledge panel  → "Focus on Wikipedia + Wikidata first to trigger KP"

4. Tabel `entity_checks`:
   id, project_id, target_domain, target_brand, report_json, entity_authority_score, analyzed_at

5. Endpoints:
   POST /api/entity/check  (~15 secunde)
   GET  /api/entity/check/{project_id}/latest

6. UI tab "Entity Audit" în project dashboard:
   Grid 6 cards per sursă: ✅/⚠️/❌
   Entity Authority Score gauge 0-100
   Recommendations cu priority

Cost: $0.002 (2 Serper calls).
Migrație: migrations/021_entity_checks.sql
```

---

## PROMPT 27 — Google Search Console Integration

```
Repo: nrankai-cloud

1. `utils/gsc_client.py` — OAuth 2.0:
   Scopes: webmasters.readonly
   google-auth, google-auth-oauthlib, google-api-python-client
   
   get_query_data(project_id, queries, date_range_days=90, gl="ro"):
   POST searchconsole.googleapis.com/v1/sites/{property}/searchAnalytics/query
   Returnează: Dict[query → {clicks, impressions, ctr, position}]

2. Tabel `gsc_connections`:
   project_id UNIQUE, gsc_property, access_token TEXT, refresh_token TEXT, token_expiry

3. `workers/gsc_fanout_crossref.py`:
   4 categorii per query:
   SYNCED:     ai_found + serp_found → ideal
   AI_GAP:     not ai + serp_found  → GEO optimization needed
   AI_ONLY:    ai_found + not serp  → SEO opportunity
   DOUBLE_GAP: not ai + not serp    → create content
   traffic_at_risk = sum(clicks) pentru AI_GAP

4. Endpoints:
   GET    /api/gsc/connect/{project_id}  → OAuth redirect
   GET    /api/gsc/callback              → save tokens
   GET    /api/gsc/status/{project_id}
   POST   /api/gsc/crossref
   DELETE /api/gsc/disconnect/{project_id}

.env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
Dependențe: google-auth, google-auth-oauthlib, google-api-python-client
Migrație: migrations/021_gsc_connections.sql
```

---

## PROMPT 28 — VelocityCMS Bridge

```
Repo: nrankai-cloud

IMPORTANT: Verifică VelocityCMS repo ÎNAINTE:
- Cum se creează draft: REST API sau direct DB?
- Schema tabelului posts: câmpuri exacte
- Auth method pentru API

1. `utils/velocitycms_bridge.py`:
   create_draft_from_gap(gap, project, context=None) -> CMSDraftResult:
   
   a) Claude Haiku ($0.001): generează meta_description (155 chars), H1,
      outline HTML (H2+bullets), FAQ 3-5 întrebări
   
   b) Creează draft cu status="draft" + geo_source metadata:
      {fanout_query, cluster, priority, competitor_who_covers}
   
   Returnează: draft_id, draft_url, title, success

2. Endpoints:
   POST /api/fanout/crossref/{id}/gaps/{idx}/create-draft
   POST /api/fanout/crossref/{id}/gaps/create-all-drafts?priority=high

3. UI în crossref results:
   [→ Create Draft in CMS] per gap → loading → [✓ Open Draft →]
   [→ Create All High Priority Drafts]
   Disabled cu tooltip dacă VELOCITYCMS_API_URL nu e configurat.

.env: VELOCITYCMS_API_URL, VELOCITYCMS_API_KEY
Cost: ~$0.001/draft (Claude Haiku)
```

---

# ═══════════════════════════════════════
# PHASE 5 — OPTIMIZARE GEO REALĂ
# ═══════════════════════════════════════

## PROMPT 32 — Mention Seeding Tracker

```
Repo: nrankai-cloud

Scop: monitorizează prezența brandului pe sursele authoritative pentru LLM training/retrieval.

1. Tabel `mention_seeding_configs`:
   id, project_id, target_brand, target_domain, vertical,
   monitor_reddit, monitor_quora, monitor_review_sites, monitor_press BOOLEAN,
   keywords JSON, schedule, is_active, last_run_at

2. Tabel `mention_seeding_results`:
   id, config_id, run_date DATE, platform VARCHAR(50),
   mention_url, mention_title TEXT, mention_context TEXT,
   sentiment VARCHAR(20), is_new BOOLEAN, discovered_at

3. `workers/mention_seeder_worker.py`:
   async def run_mention_scan(config_id, db):
     brand = config.target_brand
     
     Reddit:      serper_search(f'site:reddit.com "{brand}"', num=10)
     Quora:       serper_search(f'site:quora.com "{brand}"', num=10)
     G2:          serper_search(f'site:g2.com "{brand}"', num=5)
     Capterra:    serper_search(f'site:capterra.com "{brand}"', num=5)
     Trustpilot:  serper_search(f'site:trustpilot.com "{brand}"', num=5)
     Press:       serper_search(f'"{brand}" -site:{domain}', num=15)
                  Exclude: youtube, facebook, twitter, instagram, linkedin, amazon
     
     is_new = URL nu apărea în run-ul precedent.

4. MentionSeedingReport:
   {total_mentions, new_this_run, by_platform, sentiment_breakdown,
    coverage_score, missing_platforms, recommendations}

5. Recomandări:
   reddit=0 → "Create/participate in relevant subreddit discussions"
   g2=0     → "Claim and optimize G2 profile"
   press<3  → "Target press coverage — reach relevant publications"
   negative>30% → "Address negative mentions — reputation risk"

6. Endpoints:
   POST /api/mention-seeding/configs
   GET  /api/mention-seeding/configs
   POST /api/mention-seeding/configs/{id}/run
   GET  /api/mention-seeding/configs/{id}/latest

7. Trigger webhook "mention_seeding_completed"

8. UI în project dashboard:
   Coverage Score gauge + timeline chart + grid platforme + tabel "New This Week"

Cost: ~$0.008-0.015/run (8-15 Serper calls)
Migrație: migrations/022_mention_seeding.sql
```

---

## PROMPT 33 — Bot Access Audit

```
Repo: nrankai-cloud

Scop: verifică dacă GPTBot, ClaudeBot, PerplexityBot sunt blocați în robots.txt.

1. `utils/bot_access_auditor.py`:
   BOT_CRAWLERS = {
     "GPTBot": "OpenAI ChatGPT",              # greutate 30
     "PerplexityBot": "Perplexity AI",        # greutate 25
     "ClaudeBot": "Anthropic Claude",          # greutate 20
     "Claude-Web": "Anthropic Claude (web)",
     "Googlebot-Extended": "Google SGE",      # greutate 15
     "Bingbot": "Microsoft Copilot",          # greutate 10
     "meta-externalagent": "Meta AI",
     "Applebot-Extended": "Apple AI",
     "YouBot": "You.com AI",
     "cohere-ai": "Cohere AI"
   }
   
   async def audit(target_domain) -> BotAccessReport:
     Fetch https://{domain}/robots.txt
     parse_robots_txt(content) → grupează reguli per User-agent
     Pentru fiecare bot: check exact match > wildcard *
     Blocked = "Disallow: /" sau "Disallow: /*"
     
     Fetch homepage → extrage meta robots, X-Robots-Tag header
     
     access_score: sum greutăți bots permișisi / sum total greutăți * 100

2. BotAccessReport:
   target_domain, robots_accessible, crawlers: Dict[bot → {status, blocked_paths}],
   meta_robots_homepage, x_robots_header,
   overall_status: open|partially_blocked|mostly_blocked|fully_blocked,
   access_score, blocked_crawlers, allowed_crawlers, recommendations,
   robots_txt_raw

3. Recomandări cu fix snippets copy-paste:
   GPTBot blocat → "User-agent: GPTBot\nAllow: /"
   ClaudeBot blocat → "User-agent: ClaudeBot\nAllow: /"
   * blocat + LLM bots nespecificați → "Explicitly whitelist LLM crawlers"

4. Tabel `bot_access_audits`:
   id, project_id, target_domain, report_json, access_score, audited_at

5. Endpoints:
   POST /api/bot-access/audit  → Body: {target_domain, project_id}
   GET  /api/bot-access/{project_id}/latest

6. UI în project dashboard:
   Grid crawlers cu ✅/❌/⚠️ + Access Score gauge
   robots.txt viewer + copy-paste fix snippets
   Alert dacă access_score < 50: "🚨 AI crawlers blocked — content cannot be cited"

7. Integrare: rulează automat la primul fan-out analysis per domeniu
   dacă nu există audit în ultimele 30 zile.

Migrație: migrations/023_bot_access.sql
Cost: $0 (httpx, fără API calls plătite)
```

---

## PROMPT 34 — Co-citation Map

```
Repo: nrankai-cloud

Scop: identifică ce branduri apar alături de client (sau competitori) în răspunsurile AI.

1. `workers/cocitation_analyzer.py`:
   
   async def build_cocitation_map(target_brand, target_domain, fanout_session_ids, db):
     
     Pentru fiecare sesiune:
     - source_domains = toate domeniile din sources
     - target_present = target_domain în sources
     
     Dacă target_present:
       others = source_domains - target
       pentru fiecare: brand_cooccurrences[domain]++
       brand_contexts[domain].append(session.prompt)
     
     Returnează CoCitationMap:
     {
       total_sessions_analyzed, sessions_with_target,
       frequent_co_citations: [
         {domain, co_occurrences, co_occurrence_rate,
          contexts: top 3 prompturi,
          relationship: competitor|review_site|directory|reference}
       ],
       missing_associations: [...],  # competitori fără co-apariție cu target
       association_gaps: [{
         domain, competitor_rate, your_rate, recommendation
       }],
       insights: [str]
     }
   
   def classify_relationship(domain) -> str:
     DIRECTORIES = ["yelp.com", "tripadvisor.com", "angi.com", "thumbtack.com"]
     REVIEW_SITES = ["g2.com", "capterra.com", "trustpilot.com", "clutch.co"]
     if domain in DIRECTORIES: return "directory"
     if domain in REVIEW_SITES: return "review_site"
     return "competitor"

2. Tabel `cocitation_maps`:
   id, project_id, target_domain, sessions_analyzed JSON, map_json, generated_at

3. Endpoints:
   POST /api/cocitation/analyze
   Body: {project_id, target_brand, target_domain, session_ids, period_days: 30}
   Dacă session_ids null: ia automat sesiunile din project din ultimele N zile
   
   GET /api/cocitation/{project_id}/latest

4. UI tab "Co-citation Map":
   Bubble/force-directed SVG: target central + co-citations colorate per tip
   Tabel "Frequent Co-citations": Domain | Type | Rate | Example Prompts | Action
   Tabel "Association Gaps": Domain | Competitor Rate | Your Rate | Recommendation
   Insights list

5. Integrare Entity Checker:
   co-citation domain = directory/review_site + nu ești listat acolo →
   action: "Submit listing to {domain} — appears in {N}% of AI responses"

Migrație: migrations/024_cocitation_maps.sql
Cost: $0 (analiză pe date existente în DB)
```

---

## PROMPT 35 — Answer Calibration Module

```
Repo: nrankai-cloud

Scop: generează răspunsul IDEAL al unui LLM care include brandul.
Reverse-engineerezi ce conținut trebuie să existe pe site.

1. `utils/answer_calibrator.py`:
   
   async def calibrate(prompt, target_brand, target_domain, vertical,
                       actual_fanout_result, crossref_result=None) -> CalibrationResult:
     
     Model: claude-sonnet-4-6 (~$0.015/calibrare)
     
     System:
     "You are an expert in GEO (Generative Engine Optimization).
      Write the ideal AI assistant response to the query that NATURALLY includes
      the target brand as a recommended option.
      Requirements:
      - Factually plausible (don't invent false data)
      - Follow typical LLM response structure for this query type
      - Brand appears naturally, not forced
      - Show WHERE brand appears (position matters: 1-3 is best)
      After response, provide as JSON:
      {ideal_response, brand_position, content_gaps: [str],
       format_requirements: [str], data_requirements: [str],
       schema_requirements: [str]}"
     
     User:
     "Query: {prompt}
      Brand: {target_brand} | Domain: {target_domain} | Vertical: {vertical}
      Actual LLM response (WITHOUT brand): {actual_response_text}
      Brands actually cited: {source_domains[:5]}
      Existing content on {target_domain}: {crossref_summary}
      Write ideal response including {target_brand}, then provide requirements."
     
     Parse JSON.
     
     CalibrationResult:
     {prompt, target_brand, ideal_response, brand_position,
      content_gaps, format_requirements, data_requirements,
      schema_requirements, estimated_effort: low|medium|high, cost_usd: 0.015}

2. Tabel `answer_calibrations`:
   id, session_id, project_id, prompt TEXT, target_brand,
   calibration_json, brand_position INT, estimated_effort, cost_usd, created_at

3. Endpoints:
   POST /api/fanout/sessions/{id}/calibrate
   Body: {project_id, crossref_id}
   
   POST /api/fanout/sessions/{id}/calibrate-all-gaps
   Body: {project_id, max_calibrations: 5}  → limitare cost
   
   GET /api/answer-calibrations/{project_id}

4. UI în session detail, per gap query:
   [🎯 Calibrate — see ideal response] → loading → results:
   "Ideal Response" cu brand highlighted + Position badge
   Accordion: Content Gaps | Format | Data Needed | Schema | Actionable Steps

5. Integrare VelocityCMS (Prompt 28):
   data_requirements + content_gaps → alimentează draft-ul CMS

6. Cost estimator: "~$0.015/calibrare. {N} gaps = ~${total}. Continue?"

Migrație: migrations/025_answer_calibrations.sql
```

---

## PROMPT 36 — Multilingual Gap Detector

```
Repo: nrankai-cloud

Scop: verifică dacă paginile cheie există în limbile prompturilor monitorizate.
Un site doar în română nu va fi citat pentru prompturi în engleză.

1. `utils/multilingual_gap_detector.py`:
   
   async def detect_gaps(target_domain, key_pages, prompt_languages, db):
     
     Detectează limbi existente pe site:
     a) hreflang tags pe homepage (<link rel="alternate" hreflang="en" href="...">)
     b) URL pattern: /{lang}/ sau {lang}.domain
     c) Try direct: https://domain/en/, https://en.domain/
     
     Pentru fiecare pagină din key_pages (max 20):
     Verifică ce limbi lipsesc față de prompt_languages.
     
     priority calculation:
     HIGH: homepage SAU pricing/services + "en" lipsă
     MEDIUM: altfel
     
     MultilingualGapReport:
     {
       detected_site_languages, prompt_languages, missing_languages,
       coverage_score,
       page_gaps: [{url, available_in, missing_in, priority}],
       summary: {pages_with_gaps, critical_missing, revenue_risk},
       recommendations: [str],
       hreflang_template: str  # generat automat
     }
   
   hreflang_template generat pentru implementare:
   <link rel="alternate" hreflang="ro" href="https://{domain}/ro/" />
   <link rel="alternate" hreflang="en" href="https://{domain}/en/" />
   <link rel="alternate" hreflang="x-default" href="https://{domain}/" />

2. Tabel `multilingual_gap_reports`:
   id, project_id, target_domain, report_json, coverage_score, analyzed_at

3. Endpoints:
   POST /api/multilingual/detect-gaps
   Body: {project_id, target_domain, key_pages: [...], prompt_languages: ["ro","en"]}
   
   GET /api/multilingual/{project_id}/latest

4. Integrare cu Tracking:
   La creare tracking config cu prompts în limbi multiple →
   rulează automat multilingual detection dacă nu există raport recent.
   
   Warning în tracking timeline dacă lipsesc limbi:
   "⚠️ {N} prompts in English — no English content detected on site"

5. UI tab "Language Coverage" în project dashboard:
   Grid limbi: RO ✅ | EN ❌ | DE ❌
   Coverage Score gauge
   Tabel pagini cu gaps + priority
   [Copy hreflang Code] button

Migrație: migrations/026_multilingual_gaps.sql
Cost: $0 (httpx, fără API calls plătite)
```

---

# ═══════════════════════════════════════
# MIGRAȚII — ORDINE COMPLETĂ
# ═══════════════════════════════════════

```
migrations/007_fanout_tables.sql          # Phase 1: sesiuni, queries, sources
migrations/008_fanout_tracking.sql        # Phase 2: tracking configs + runs + details
migrations/008b_competitive.sql           # Phase 2: competitive reports
migrations/009_schema_enrichment.sql      # Phase 3: verifică ce lipsește vs Prompt 2
migrations/010_fanout_cache.sql           # Phase 3: cache layer
migrations/011_fanout_crossref.sql        # Phase 3: cross-reference results
migrations/012_fanout_serp.sql            # Phase 3: SERP validation
migrations/013_fanout_webhooks.sql        # Phase 3: webhooks + logs
migrations/014_prompt_library.sql         # Phase 3: library + seed data (60+ prompturi)
migrations/015_api_keys.sql               # Phase 4: auth
migrations/016_dead_letter_queue.sql      # Phase 4: retry fields pe tracking_runs
migrations/017_projects.sql               # Phase 4: projects + ALTER pe toate tabelele
migrations/018_composite_score.sql        # Phase 4: composite_score pe tracking_runs
migrations/019_sentiment.sql              # Phase 4: sentiment per sesiune + tracking
migrations/020_geo_benchmarks.sql         # Phase 4: GEO benchmarks (separat de WLA)
migrations/021_entity_checks.sql          # Phase 4: entity checker
migrations/021_gsc_connections.sql        # Phase 4: GSC OAuth
migrations/022_mention_seeding.sql        # Phase 5: mention seeding
migrations/023_bot_access.sql             # Phase 5: bot access audits
migrations/024_cocitation_maps.sql        # Phase 5: co-citation maps
migrations/025_answer_calibrations.sql    # Phase 5: answer calibrations
migrations/026_multilingual_gaps.sql      # Phase 5: multilingual gaps
```

---

# ═══════════════════════════════════════
# .ENV COMPLET
# ═══════════════════════════════════════

```
# Core
APP_ENV=development
APP_HOST=0.0.0.0
APP_PORT=8000
APP_BASE_URL=http://localhost:8000
APP_SECRET_KEY=change-me-in-production
DISABLE_AUTH=false
CORS_ORIGINS=http://localhost:3000,http://localhost:8000
ALLOWED_HOSTS=app.nrankai.com,localhost

# Database
DB_HOST=localhost
DB_PORT=3306
DB_NAME=nrankai_cloud
DB_USER=nrankai
DB_PASSWORD=change-me

# LLM APIs
OPENAI_API_KEY=             # ChatGPT fan-out (obligatoriu)
ANTHROPIC_API_KEY=          # Claude sentiment/calibration/briefs
PERPLEXITY_API_KEY=         # Perplexity sources
GOOGLE_API_KEY=             # Gemini grounding

# External
SERPER_API_KEY=             # SERP + entity checks ($50/50k queries)
N8N_WEBHOOK_URL=            # n8n pe Proxmox

# Google Search Console OAuth
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=https://app.nrankai.com/api/gsc/callback

# VelocityCMS
VELOCITYCMS_API_URL=http://localhost:3000
VELOCITYCMS_API_KEY=

# nrankai-tool path
NRANKAI_TOOL_PATH=../nrankai-tool
```

---

# ═══════════════════════════════════════
# COST MATRIX
# ═══════════════════════════════════════

```
Fan-out ChatGPT gpt-4.1:         $0.02-0.04/query
Fan-out Gemini 2.5-flash:        ~$0.001/query
Fan-out Perplexity sonar-pro:    $0.015/query
20 prompt discovery (2 engines): ~$0.50
Tracking weekly (10 prompts):    ~$0.25/run
SERP validation 20 queries:      $0.02
Sentiment (Haiku):               $0.002/sesiune
Answer calibration (Sonnet):     $0.015/query
Mention seeding scan:            $0.008-0.015/run
Entity check:                    $0.002 (2 Serper)
Bot access audit:                $0
Co-citation analysis:            $0
Multilingual detection:          $0
VelocityCMS draft (Haiku):       $0.001/draft
```
