# WLA — AI Query Fan-Out Analyzer
## Claude Code Prompts (copy-paste ready)

**Module concept:** Reverse-engineer which search queries ChatGPT (and other AI engines) fire when answering a user prompt. This reveals the "retrieval surface" — the exact keywords and sources you need to rank for to appear in AI-generated answers.

**Inspired by:** QueryFan.com by Mark Williams-Cook (Candour)

**Repos:**
- `nrankai-tool` — CLI batch analyzer (WLA core)
- `nrankai-cloud` — FastAPI web service (UI + API)

**Execution order:** 1 → 2 → 3 → 4 → 5 → 6

---

## PROMPT 1 — OpenAI Responses API Fan-Out Extractor (Core Engine)

```
Repo: nrankai-tool

Creează fișierul `fanout_analyzer.py` în root-ul proiectului.

Acest modul folosește OpenAI Responses API cu web_search tool pentru a extrage query-urile de fan-out pe care ChatGPT le generează intern când răspunde la un prompt.

Funcționalitate:

1. Clasa `FanoutAnalyzer` cu:
   - __init__(self, api_key: str = None, model: str = "gpt-4o")
     - api_key din parametru sau os.getenv("OPENAI_API_KEY")
     - Folosește openai.OpenAI client
   
   - async def analyze_prompt(self, prompt: str, user_location: str = None) -> FanoutResult:
     - Apelează OpenAI Responses API:
       response = client.responses.create(
           model=self.model,
           input=prompt,
           tools=[{"type": "web_search"}]
       )
     - Parsează response.output pentru a extrage:
       a) web_search_call items → extrage query-urile de search (din câmpul "action" sau similar)
       b) message items cu annotations de tip url_citation → extrage URL-urile sursă, title-urile
     - Dacă user_location e specificat, adaugă context la prompt: f"{prompt} (location: {user_location})"
   
   - async def analyze_batch(self, prompts: List[str], user_location: str = None) -> List[FanoutResult]:
     - Procesează mai multe prompturi secvențial (cu delay de 1s între ele pentru rate limiting)

2. Dataclass `FanoutResult`:
   - prompt: str (prompt-ul original)
   - model: str
   - fanout_queries: List[str] (query-urile de search extrase)
   - sources: List[dict] (url, title, snippet pentru fiecare sursă citată)
   - search_call_count: int
   - total_sources: int
   - total_fanout_queries: int
   - timestamp: datetime
   - raw_response: dict (response-ul complet pentru debugging)

3. Funcție helper `extract_search_queries(response_output: list) -> List[str]`:
   - Iterează prin response.output
   - Pentru items cu type="web_search_call", extrage query-ul din action.query sau search.query
   - Returnează lista de query-uri unice

4. Funcție helper `extract_sources(response_output: list) -> List[dict]`:
   - Iterează prin items cu type="message"
   - Din content[].annotations unde type="url_citation", extrage url și title
   - De-duplică pe URL
   - Returnează lista de sources

Dependențe: openai>=1.40.0, python-dotenv
Adaugă în requirements.txt dacă nu există deja.

NU folosi async httpx direct — folosește SDK-ul oficial openai.
Adaugă logging cu logger = logging.getLogger("fanout_analyzer").
Adaugă error handling pentru: API key missing, rate limit (retry cu exponential backoff, max 3 retries), invalid response format.
Adaugă un __main__ block pentru testare rapidă:
  python fanout_analyzer.py "best seo agency romania"
```

---

## PROMPT 2 — MariaDB Schema + Storage Layer

```
Repo: nrankai-cloud

Creează fișierul `models/fanout_models.py` și adaugă migrația SQL.

1. Tabel MariaDB `fanout_sessions`:
   CREATE TABLE fanout_sessions (
     id VARCHAR(36) PRIMARY KEY,          -- UUID
     prompt TEXT NOT NULL,
     model VARCHAR(50) NOT NULL DEFAULT 'gpt-4o',
     user_location VARCHAR(200),
     total_fanout_queries INT DEFAULT 0,
     total_sources INT DEFAULT 0,
     total_search_calls INT DEFAULT 0,
     target_url VARCHAR(500),             -- site-ul clientului (opțional, pentru cross-reference)
     target_found_in_sources BOOLEAN DEFAULT FALSE,
     target_source_position INT,          -- la a câta sursă apare site-ul clientului
     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
     campaign_id VARCHAR(50),             -- legătură cu campaniile de prospects
     audit_id VARCHAR(36),               -- legătură cu auditul WLA dacă e cazul
     INDEX idx_campaign (campaign_id),
     INDEX idx_target (target_url),
     INDEX idx_created (created_at)
   );

2. Tabel MariaDB `fanout_queries`:
   CREATE TABLE fanout_queries (
     id INT AUTO_INCREMENT PRIMARY KEY,
     session_id VARCHAR(36) NOT NULL,
     query_text TEXT NOT NULL,
     query_position INT,                  -- ordinea în care a apărut (Q1, Q2, etc.)
     FOREIGN KEY (session_id) REFERENCES fanout_sessions(id) ON DELETE CASCADE,
     INDEX idx_session (session_id)
   );

3. Tabel MariaDB `fanout_sources`:
   CREATE TABLE fanout_sources (
     id INT AUTO_INCREMENT PRIMARY KEY,
     session_id VARCHAR(36) NOT NULL,
     url VARCHAR(2000) NOT NULL,
     title VARCHAR(500),
     domain VARCHAR(500),                 -- extras automat din URL
     is_target BOOLEAN DEFAULT FALSE,     -- True dacă e site-ul clientului
     citation_count INT DEFAULT 1,        -- de câte ori a fost citat în response
     FOREIGN KEY (session_id) REFERENCES fanout_sessions(id) ON DELETE CASCADE,
     INDEX idx_session (session_id),
     INDEX idx_domain (domain)
   );

4. În `models/fanout_models.py` creează:
   - SQLAlchemy models pentru cele 3 tabele (folosește pattern-ul existent din proiect)
   - Pydantic schemas: FanoutSessionCreate, FanoutSessionResponse, FanoutQueryResponse, FanoutSourceResponse
   - Helper: save_fanout_result(db_session, fanout_result: FanoutResult, target_url: str = None) -> str
     - Salvează session + queries + sources
     - Dacă target_url e dat, verifică dacă apare în sources și setează target_found_in_sources + position
     - Extrage domain din fiecare source URL cu urllib.parse.urlparse
     - Returnează session_id

5. Adaugă migrația SQL într-un fișier `migrations/007_fanout_tables.sql` (sau următorul număr disponibil).

Folosește pattern-ul de conexiune DB existent din proiect (verifică cum se face în models/ sau database.py).
```

---

## PROMPT 3 — FastAPI Endpoints

```
Repo: nrankai-cloud

Creează fișierul `routes/fanout.py` cu endpoint-urile pentru Fan-Out Analyzer.

1. POST /api/fanout/analyze
   Body: {
     "prompt": "best seo agency romania",
     "model": "gpt-4o",                    // opțional, default gpt-4o
     "target_url": "https://example.com",   // opțional — site-ul clientului
     "user_location": "Bucharest, Romania", // opțional
     "campaign_id": "campaign_123"          // opțional
   }
   Response: {
     "session_id": "uuid",
     "prompt": "...",
     "fanout_queries": ["query1", "query2", ...],
     "sources": [{"url": "...", "title": "...", "domain": "..."}],
     "stats": {
       "total_queries": 22,
       "total_sources": 145,
       "search_calls": 8,
       "target_found": true,
       "target_position": 12
     }
   }
   - Apelează FanoutAnalyzer din nrankai-tool (importă direct sau via subprocess)
   - Salvează în DB
   - Returnează rezultatul complet

2. POST /api/fanout/analyze-batch
   Body: {
     "prompts": ["prompt1", "prompt2", ...],  // max 10
     "model": "gpt-4o",
     "target_url": "https://example.com",
     "user_location": "Romania",
     "campaign_id": "campaign_123"
   }
   Response: {
     "job_id": "uuid",
     "total_prompts": 5,
     "status": "processing"
   }
   - Rulează async ca background task (BackgroundTasks FastAPI)
   - Fiecare prompt devine un fanout_session separat

3. GET /api/fanout/sessions
   Query params: campaign_id, target_url, limit (default 20), offset
   Response: lista paginată de sesiuni cu stats

4. GET /api/fanout/sessions/{session_id}
   Response: sesiunea completă cu queries și sources

5. GET /api/fanout/sessions/{session_id}/coverage
   Response: {
     "target_url": "https://example.com",
     "target_found": true,
     "target_position": 12,
     "coverage_score": 45,                    // % din fanout queries unde target ar putea ranki
     "missing_queries": ["query3", "query7"],  // queries unde target NU apare
     "competing_domains": [                    // top domenii din sources
       {"domain": "competitor.com", "appearances": 8},
       {"domain": "review-site.com", "appearances": 5}
     ]
   }
   - Coverage score = (queries unde target domain apare în top 10 SERP) / total queries * 100
   - Missing queries = queries unde target domain NU e în sources
   - Competing domains = group by domain, count, sort desc

6. DELETE /api/fanout/sessions/{session_id}

Înregistrează router-ul în main.py cu prefix-ul existent.
Adaugă autentificare dacă există deja un pattern de auth în proiect (verifică middleware).
Rate limit: max 5 analyze requests per minut (simplu in-memory counter cu time check).
```

---

## PROMPT 4 — Cross-Reference cu WLA Modules Existente

```
Repo: nrankai-cloud

Creează fișierul `workers/fanout_cross_reference.py`.

Acest modul face cross-reference între rezultatele fan-out și modulele existente din WLA.

1. Funcție `cross_reference_with_citations(session_id: str, db) -> dict`:
   - Ia sources din fanout_sources pentru session_id
   - Compară cu citation_tracker results (dacă există tabel citations sau similar)
   - Returnează:
     {
       "cited_and_in_fanout": ["url1", "url2"],       // apare ȘI în citations ȘI în fanout
       "in_fanout_not_cited": ["url3"],                // apare în fanout dar NU e citat
       "cited_not_in_fanout": ["url4"],                // e citat dar NU apare în fanout
       "overlap_score": 65                             // % overlap
     }

2. Funcție `generate_content_gap_from_fanout(session_id: str, target_url: str, db) -> List[dict]`:
   - Ia fanout_queries din sesiune
   - Pentru fiecare query, verifică dacă target_url are conținut relevant (simplu: caută query keywords în titlurile/URL-urile existente din audit results)
   - Returnează lista de "content gaps":
     [
       {
         "fanout_query": "best SEO tools 2026",
         "has_content": false,
         "suggested_content_type": "listicle",          // bazat pe pattern-ul query-ului
         "priority": "high",                            // high dacă query e în primele 5 fan-out positions
         "competing_urls": ["competitor.com/page"]       // cine apare acum pentru acest query
       }
     ]

3. Funcție `calculate_retrieval_coverage(session_id: str, target_domain: str, db) -> dict`:
   - Calculează Retrieval Coverage Score:
     {
       "domain": "example.com",
       "total_fanout_queries": 22,
       "queries_where_domain_appears": 3,
       "retrieval_coverage_pct": 13.6,
       "top_competing_domains": [
         {"domain": "competitor.com", "coverage_pct": 36.4, "appearances": 8}
       ],
       "improvement_potential": "high"                  // high dacă <20%, medium 20-50%, low >50%
     }

4. Endpoint: GET /api/fanout/sessions/{session_id}/cross-reference
   - Apelează toate cele 3 funcții
   - Returnează JSON combinat cu secțiunile: citations_overlap, content_gaps, retrieval_coverage

Verifică tabelele existente din DB (prospects, audit results, citations) și adaptează query-urile SQL.
Dacă un tabel nu există, skip acel modul și returnează null pentru secțiunea respectivă.
```

---

## PROMPT 5 — UI Page (Jinja2 Template)

```
Repo: nrankai-cloud

Creează template-ul `templates/fanout.html` pentru pagina Fan-Out Analyzer.

Pagina trebuie să aibă:

1. SECȚIUNEA "Analyze" (sus):
   - Input text mare: "Enter a prompt to analyze" (placeholder: "What are the best SEO tools in 2026?")
   - Select model: dropdown cu opțiuni gpt-4o, gpt-4o-mini, gpt-5
   - Input opțional: Target URL (site-ul clientului pentru coverage check)
   - Input opțional: User Location
   - Buton "Analyze Fan-Out" → POST /api/fanout/analyze
   - Loading spinner cu text "Analyzing fan-out queries... This takes 10-30 seconds"

2. SECȚIUNEA "Results" (apare după analiză):
   - Stats bar: "22 fan-out queries • 145 sources • 8 search calls" (stilizat ca în QueryFan)
   - Target coverage badge: "Your site found in 3/22 queries (13.6%)" — verde >50%, galben 20-50%, roșu <20%

3. Tab "Fan-Out Queries":
   - Tabel cu coloanele: #, Query, Copy button
   - Fiecare rând are un buton copy (clipboard API)
   - Queries numerotate Q1, Q2, ... Qn
   - Highlight cu verde pe queries unde target domain apare în sources

4. Tab "Sources":
   - Tabel: #, Domain, URL (link), Title, Is Target (badge verde/gri)
   - Sortabil pe domain
   - Filter: "Show only target domain"

5. Tab "Domain Analysis":
   - Bar chart (Chart.js sau Recharts): top 10 domenii după număr de apariții
   - Evidențiază target domain cu culoare diferită
   - Sub chart: tabel cu domain, appearances, % din total

6. Tab "Content Gaps" (dacă target_url e specificat):
   - Tabel: Fan-out Query, Has Content, Priority, Suggested Type, Competing URLs
   - Badge-uri colorate pentru priority (High=roșu, Medium=galben, Low=verde)
   - Buton "Generate Content Brief" pe fiecare rând (link către content brief generator dacă există)

7. SECȚIUNEA "History" (jos):
   - Tabel cu ultimele 20 sesiuni: Prompt (truncat), Date, Queries count, Sources count, Coverage %
   - Click pe rând → deschide results-ul complet

Folosește AlpineJS + Tailwind CSS, consistent cu celelalte template-uri din proiect.
Adaugă link "Fan-Out Analyzer" în navigația principală din base.html, după "GEO Monitor".
Fetch-urile către API se fac cu Alpine.js x-data + fetch().
Adaugă x-cloak pe secțiunile care trebuie ascunse inițial.
```

---

## PROMPT 6 — Action Cards Integration + Prospect Enrichment

```
Repo: nrankai-cloud

Integrează Fan-Out Analyzer cu sistemul de Action Cards și cu Prospect scoring.

1. În `workers/fanout_cross_reference.py` adaugă funcția:
   def generate_fanout_action_cards(session_id: str, target_url: str, db) -> List[dict]:
   - Bazat pe rezultatele cross-reference, generează action cards:
     a) "Low Retrieval Coverage" (dacă coverage <20%):
        - priority: "critical"
        - action: "Create content targeting these {N} fan-out queries where you're not visible"
        - details: lista de missing queries
     b) "Competitor Dominance" (dacă un competitor apare în >50% din sources):
        - priority: "high"  
        - action: "Competitor {domain} dominates AI retrieval for this topic"
        - details: competitor domain + appearance count
     c) "Quick Win Queries" (dacă target apare în sources dar nu în top 3 positions):
        - priority: "medium"
        - action: "Optimize existing content to improve position for these queries"
        - details: queries unde target e prezent dar nu dominant
     d) "Missing Content Types" (dacă anumite tipuri de content lipsesc):
        - priority: "medium"
        - action: "Create {type} content to fill gap"
        - details: suggested content types din content_gaps

   Fiecare action card are structura:
   {
     "id": "uuid",
     "type": "fanout_coverage|competitor_dominance|quick_win|content_gap",
     "priority": "critical|high|medium|low",
     "title": "...",
     "description": "...",
     "action_items": ["item1", "item2"],
     "data": {}  // date raw pentru UI
   }

2. Dacă tabelul `prospects` există, adaugă funcția:
   def enrich_prospect_with_fanout(prospect_id: int, prompts: List[str], db) -> dict:
   - Pentru un prospect, rulează fan-out analysis pe prompturile relevante
   - Actualizează prospect-ul cu:
     - geo_visibility_score bazat pe retrieval_coverage_pct (media din toate prompturile)
     - top_issues JSON cu action cards generate
   - Returnează dict cu scores actualizate

3. Endpoint: POST /api/fanout/enrich-prospect
   Body: {
     "prospect_id": 123,
     "prompts": ["best {category} {city}", "top {category} near me"],
     "model": "gpt-4o-mini"   // mini e mai ieftin pentru bulk
   }
   - Înlocuiește {category} și {city} cu datele prospect-ului din DB
   - Rulează ca background task
   - Returnează: {"job_id": "uuid", "status": "processing"}

4. Endpoint: POST /api/fanout/enrich-batch
   Body: {
     "campaign_id": "campaign_123",
     "prompts_template": ["best {business_category} {location_city}"],
     "model": "gpt-4o-mini",
     "limit": 50                // max 50 prospects per batch
   }
   - Selectează primii N prospects din campanie cu status 'scored' sau 'pending'
   - Rulează enrich_prospect_with_fanout pentru fiecare
   - Rate limit: 1 prospect per 5 secunde (OpenAI rate limits)

Verifică structura tabelului prospects existent și adaptează field names.
Dacă tabelul prospects nu există, skip partea de prospect enrichment și implementează doar action cards.
```

---

## NOTE TEHNICE

### Costuri estimate per analiză:
- **gpt-4o** cu web_search: ~$0.02-0.05 per prompt (depinde de fan-out depth)
- **gpt-4o-mini** cu web_search: ~$0.005-0.01 per prompt (recomandat pentru bulk)
- **gpt-5**: ~$0.05-0.15 per prompt (cel mai detaliat fan-out)

### Limitări OpenAI Responses API:
- Web search tool returnează query-urile de search în response.output ca items cu type `web_search_call`
- Sources vin ca `url_citation` annotations în message content
- Rate limit: 500 RPM pentru Tier 1, verifică tier-ul contului
- Response-ul poate varia ca structură — extractor-ul trebuie să fie robust

### Ordine de implementare recomandată:
1. **Prompt 1** (core engine) — testează standalone cu `python fanout_analyzer.py`
2. **Prompt 2** (DB schema) — rulează migrația, verifică tabelele
3. **Prompt 3** (API endpoints) — testează cu curl/Postman
4. **Prompt 5** (UI) — verifică vizual în browser
5. **Prompt 4** (cross-reference) — necesită date existente în DB
6. **Prompt 6** (action cards + prospects) — ultimul, depinde de toate celelalte

### Ce model Claude Code să folosești:
- **Prompt 1, 2, 3, 7**: Sonnet 4.6 (mecanice, clar definite)
- **Prompt 4, 6**: Opus 4.6 (logică complexă de cross-reference)
- **Prompt 5, 8**: Sonnet 4.6 (template UI, pattern existent)

---

## PROMPT 7 — Deployment Config: app.nrankai.com + Local Dev

```
Repo: nrankai-cloud

Configurează proiectul pentru a rula în două medii: local (development) și pe subdomeniul app.nrankai.com (production pe VPS cu CloudPanel).

1. Creează/actualizează fișierul `.env.example` cu toate variabilele necesare:
   
   # === APP ===
   APP_ENV=development                          # development | production
   APP_HOST=0.0.0.0
   APP_PORT=8000
   APP_BASE_URL=http://localhost:8000           # local
   # APP_BASE_URL=https://app.nrankai.com       # production
   APP_SECRET_KEY=change-me-in-production
   
   # === DATABASE (MariaDB) ===
   DB_HOST=localhost
   DB_PORT=3306
   DB_NAME=nrankai_cloud
   DB_USER=nrankai
   DB_PASSWORD=change-me
   
   # === API KEYS ===
   OPENAI_API_KEY=
   ANTHROPIC_API_KEY=
   PERPLEXITY_API_KEY=
   PAGESPEED_API_KEY=
   
   # === NRANKAI-TOOL PATH ===
   NRANKAI_TOOL_PATH=../nrankai-tool            # calea relativă către repo-ul nrankai-tool
   
   # === CORS (pentru dev local cu frontend separat) ===
   CORS_ORIGINS=http://localhost:3000,http://localhost:8000

2. În `main.py` (FastAPI app), adaugă/actualizează:
   - Încarcă APP_ENV din .env
   - Dacă APP_ENV == "development":
     - Activează auto-reload
     - CORS permisiv (localhost:*)
     - Logging level DEBUG
     - Disable HTTPS redirect
   - Dacă APP_ENV == "production":
     - CORS doar pentru APP_BASE_URL
     - Logging level INFO
     - Trust proxy headers (X-Forwarded-For, X-Forwarded-Proto) pentru CloudPanel reverse proxy
     - Setează cookie secure=True, samesite="lax"
   
   - Adaugă middleware TrustedHostMiddleware cu allowed_hosts din .env:
     ALLOWED_HOSTS=app.nrankai.com,localhost
   
   - Asigură-te că toate URL-urile generate intern (redirects, links în templates) folosesc APP_BASE_URL

3. Creează fișierul `scripts/run_local.sh`:
   #!/bin/bash
   # Pornește nrankai-cloud local pentru development
   set -e
   
   # Verifică .env
   if [ ! -f .env ]; then
     echo "Copiez .env.example → .env"
     cp .env.example .env
     echo "EDITEAZĂ .env cu API keys și DB credentials înainte de restart!"
   fi
   
   # Verifică MariaDB local
   if ! mysqladmin ping -h localhost --silent 2>/dev/null; then
     echo "⚠ MariaDB nu e pornit local. Pornește-l sau configurează DB_HOST în .env"
   fi
   
   # Activează virtualenv dacă există
   if [ -d "venv" ]; then
     source venv/bin/activate
   fi
   
   # Rulează migrațiile
   python manage_db.py migrate 2>/dev/null || echo "⚠ Migrații skip (manage_db.py nu există încă)"
   
   # Pornește serverul
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   
   chmod +x scripts/run_local.sh

4. Creează fișierul `scripts/deploy_vps.sh`:
   #!/bin/bash
   # Deploy pe VPS la app.nrankai.com
   set -e
   
   VPS_USER="root"
   VPS_HOST="YOUR_VPS_IP"
   DEPLOY_DIR="/opt/nrankai-cloud"
   
   echo "=== Deploy nrankai-cloud → app.nrankai.com ==="
   
   # Sync fișierele (exclude venv, __pycache__, .env)
   rsync -avz --delete \
     --exclude 'venv/' \
     --exclude '__pycache__/' \
     --exclude '.env' \
     --exclude '*.pyc' \
     --exclude '.git/' \
     ./ ${VPS_USER}@${VPS_HOST}:${DEPLOY_DIR}/
   
   # Restart serviciul pe VPS
   ssh ${VPS_USER}@${VPS_HOST} << 'REMOTE'
     cd /opt/nrankai-cloud
     source venv/bin/activate
     pip install -r requirements.txt --quiet
     
     # Rulează migrații
     python manage_db.py migrate 2>/dev/null || true
     
     # Restart systemd service
     sudo systemctl restart nrankai-cloud
     
     echo "✓ Deploy complet. Verifică: https://app.nrankai.com"
   REMOTE

5. Creează fișierul systemd `scripts/nrankai-cloud.service`:
   [Unit]
   Description=nrankai-cloud FastAPI
   After=network.target mariadb.service
   
   [Service]
   Type=simple
   User=www-data
   Group=www-data
   WorkingDirectory=/opt/nrankai-cloud
   Environment="PATH=/opt/nrankai-cloud/venv/bin"
   ExecStart=/opt/nrankai-cloud/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
   Restart=always
   RestartSec=5
   
   [Install]
   WantedBy=multi-user.target

6. Creează fișierul `scripts/cloudpanel_nginx.conf` (instrucțiuni pentru reverse proxy):
   # CloudPanel → Sites → Add Reverse Proxy
   # Domain: app.nrankai.com
   # Reverse Proxy URL: http://127.0.0.1:8000
   # Enable Let's Encrypt SSL
   #
   # Sau manual, adaugă în Nginx:
   
   location / {
       proxy_pass http://127.0.0.1:8000;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_http_version 1.1;
       proxy_set_header Upgrade $http_upgrade;
       proxy_set_header Connection "upgrade";
       proxy_read_timeout 300s;                  # timeout mare pentru analize lungi
       proxy_connect_timeout 75s;
   }

7. Actualizează `README.md` cu secțiune nouă:
   ## Deployment
   
   ### Local Development
   ```bash
   cp .env.example .env
   # Editează .env cu API keys
   ./scripts/run_local.sh
   # Deschide http://localhost:8000
   ```
   
   ### Production (app.nrankai.com)
   ```bash
   # Prima dată pe VPS:
   sudo cp scripts/nrankai-cloud.service /etc/systemd/system/
   sudo systemctl enable nrankai-cloud
   
   # Deploy:
   ./scripts/deploy_vps.sh
   ```

NU modifica logica existentă din routes sau templates.
Doar adaugă configuration layer-ul și scripturile de deployment.
Testează că `python -c "from main import app; print('OK')"` funcționează fără erori.
```

---

## PROMPT 8 — Environment-Aware Templates + Navigation Update

```
Repo: nrankai-cloud

Actualizează template-urile Jinja2 și navigația pentru a funcționa corect atât local cât și pe app.nrankai.com.

1. În `main.py`, injectează variabile globale în Jinja2 context:
   
   from starlette.config import Config
   
   @app.middleware("http")
   async def add_template_globals(request, call_next):
       # Sau folosește app.state / Jinja2 env globals
       response = await call_next(request)
       return response
   
   # La inițializarea Jinja2 templates, adaugă globals:
   templates.env.globals.update({
       "APP_ENV": os.getenv("APP_ENV", "development"),
       "APP_BASE_URL": os.getenv("APP_BASE_URL", "http://localhost:8000"),
       "APP_NAME": "nrankai GEO Tools",
       "APP_VERSION": "1.0.0",
       "NAV_ITEMS": [
           {"name": "Dashboard", "url": "/", "icon": "home"},
           {"name": "Audits", "url": "/audits", "icon": "search"},
           {"name": "GEO Monitor", "url": "/geo-monitor", "icon": "globe"},
           {"name": "Fan-Out Analyzer", "url": "/fanout", "icon": "zap"},
           {"name": "Prospects", "url": "/prospects", "icon": "users"},
           {"name": "Content Gaps", "url": "/content-gaps", "icon": "file-text"},
       ]
   })

2. Actualizează `templates/base.html`:
   - În <head>:
     <title>{% block title %}{{ APP_NAME }}{% endblock %}</title>
     <meta name="robots" content="noindex, nofollow">  {# tool intern, nu indexa #}
   
   - Navigația principală — generează dinamic din NAV_ITEMS:
     {% for item in NAV_ITEMS %}
       <a href="{{ item.url }}" 
          class="nav-link {% if request.url.path == item.url %}active{% endif %}">
         {{ item.name }}
       </a>
     {% endfor %}
   
   - Footer:
     <span class="text-gray-500 text-xs">
       {{ APP_NAME }} v{{ APP_VERSION }} • 
       {% if APP_ENV == "development" %}
         <span class="text-yellow-400">DEV</span>
       {% else %}
         <span class="text-green-400">PROD</span>
       {% endif %}
     </span>

3. Actualizează `templates/fanout.html` (creat în Prompt 5):
   - Toate fetch()-urile către API trebuie să folosească relative paths (nu hardcoda localhost):
     ✓ fetch('/api/fanout/analyze', ...)
     ✗ fetch('http://localhost:8000/api/fanout/analyze', ...)
   
   - Adaugă un banner vizibil doar în dev:
     {% if APP_ENV == "development" %}
       <div class="bg-yellow-900/50 text-yellow-200 text-center text-xs py-1">
         🔧 Development Mode — {{ APP_BASE_URL }}
       </div>
     {% endif %}

4. Creează `templates/fanout.html` route în main.py sau routes (dacă nu există deja):
   
   @app.get("/fanout", response_class=HTMLResponse)
   async def fanout_page(request: Request):
       return templates.TemplateResponse("fanout.html", {
           "request": request,
       })

5. Verifică toate celelalte template-uri existente (index.html, audit_detail.html, etc.):
   - Asigură-te că folosesc relative paths pentru API calls
   - Asigură-te că link-urile de navigație sunt consistente
   - Adaugă "Fan-Out Analyzer" în nav dacă nu e deja acolo

NU rescrie template-urile de la zero — doar actualizează navigația, adaugă globals, și corectează path-urile.
Păstrează stilul AlpineJS + Tailwind existent.
```

---

## ACTUALIZARE: Ordine completă de implementare

1. **Prompt 1** (core engine) — testează: `python fanout_analyzer.py "best seo agency romania"`
2. **Prompt 7** (deployment config) — setup .env, scripts, systemd
3. **Prompt 2** (DB schema) — rulează migrația
4. **Prompt 3** (API endpoints) — testează cu curl local
5. **Prompt 8** (templates env-aware + nav) — verifică în browser local
6. **Prompt 5** (Fan-Out UI complet) — verifică vizual
7. **Prompt 4** (cross-reference) — necesită date în DB
8. **Prompt 6** (action cards + prospects) — ultimul

### Deploy flow:
```
Local dev: ./scripts/run_local.sh → http://localhost:8000
Production: ./scripts/deploy_vps.sh → https://app.nrankai.com
```

### DNS + CloudPanel setup (manual, one-time):
1. DNS: A record `app.nrankai.com` → VPS IP
2. CloudPanel: Reverse Proxy → `http://127.0.0.1:8000`
3. Let's Encrypt SSL: enable
4. Systemd: `sudo systemctl enable nrankai-cloud`
