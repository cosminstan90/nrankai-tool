# Faza 2 — Rute și endpoint-uri

## Rezumat

| ID | Sev | Titlu | Fișier:linie | Verificat prin |
|----|-----|-------|---------------|-----------------|
| F2-01 | **P0** | `NameError: name '_CLIENT_CONFIG' is not defined` — **`/api/gsc/oauth/authorize` crapă 100%** — flow-ul de conectare GSC e complet blocat de la primul pas | `api/routes/gsc/oauth_sync.py:81` | Smoke test live: GET → 500, traceback complet în log |
| F2-02 | **P0** | `TypeError: can't subtract offset-naive and offset-aware datetimes` în `detect_alerts_for_website` — rupe **atât** `/api/portfolio/alerts` **cât și** `/api/portfolio/overview` | `api/routes/portfolio.py:344` (apelat din liniile 365, 402, 654) | Smoke test live: ambele endpoint-uri → 500, traceback identic |
| F2-03 | **P0** | `sqlite3.OperationalError: no such column: content_briefs.current_score` — **`/api/briefs` (lista de content briefs) e complet nefuncțională** | `api/routes/content_briefs.py:648` | Smoke test live → 500; confirmat prin `PRAGMA table_info` pe DB-ul real: coloana lipsește |
| F2-04 | **P0** | `sqlite3.OperationalError: no such column: fanout_sessions.query_origin` — **`/api/fanout/sessions` complet nefuncțional** — lipsesc **10 coloane** din tabelă, nu doar una | `api/routes/fanout.py:540` | Smoke test live → 500; `PRAGMA table_info(fanout_sessions)` confirmă 13 coloane prezente din 23 așteptate de model |
| F2-05 | **P0 — cauză rădăcină pentru F2-03 și F2-04** | La pornire se apelează `init_db()` (sincron), care **NU** conține auto-migrarea pentru `fanout_sessions` — acea logică există doar în `init_db_async()`, funcție **niciodată apelată** din tot codebase-ul | `api/main.py:71` (apelează `init_db`) vs. `api/models/database.py:193` (`init_db_async`, dead code) | `grep -rn "init_db_async"` → doar definiție + re-export, zero apeluri |
| F2-06 | ✅ verificat, fără problemă | Toate cele 46 de routere definite în cod sunt corect importate în `api/routes/__init__.py` **și** înregistrate în `api/main.py` — 46/46 potrivire exactă, fără orfani | diff automat între `import ... as X_router` și `app.include_router(X_router)` |
| F2-07 | ✅ verificat, fără problemă | Rate limiting: toate endpoint-urile cu `@limiter.limit(...)` au parametrul `request: Request` corect prezent și tipat, în întreaga semnătură (inclusiv multi-linie) | verificare AST-like pe toate cele 18 decoratoare găsite în `api/routes` + `app/modules` |
| F2-08 | ✅ verificat, fără problemă | Toate cele 192 de apeluri `fetch()` găsite în template-uri corespund unui path existent în OpenAPI — niciun buton „mort" detectat static | comparație automată `fetch()` URLs (192) vs. `openapi.json` paths (315) |
| F2-09 | P3 | `/api/gsc/oauth/callback` → 307 redirect (fără cod de autorizare în query, normal la GET direct); `/api/gsc-fanout/callback` → 400; `/api/gsc/oauth/sites` → 401 — comportament așteptat, nu bug | curl direct, fără parametri OAuth reali |

---

## Metodă

### 1. Rute definite vs. înregistrate (F2-06)

Am extras programatic:
- toate numele `X_router` importate în `api/routes/__init__.py` (46)
- toate numele date la `app.include_router(X_router)` din `api/main.py` (46)

```bash
comm -23 imported.txt included.txt   # → gol
comm -13 imported.txt included.txt   # → gol
```

**Potrivire perfectă, 46/46, în ambele direcții.** Toate cele 62 de fișiere care
definesc `router = APIRouter(...)` (42 top-level + 13 din `pages/` + 3 din
`gsc/` + 2 din `app/modules/`) sunt corect agregate prin cele două module-pachet
(`pages/__init__.py`, `gsc/__init__.py`) și incluse în aplicație. Sistemul de
înregistrare a rutelor e solid — nu există routere „uitate".

### 2. Smoke test GET-uri

Am extras din `openapi.json` toate cele **102 path-uri GET fără parametri de
rută** (path-urile cu `{id}` etc. au fost excluse — necesită date reale, vezi
mai jos) și le-am chemat pe serverul local pornit în Faza 0:

| Cod | Nr. | Interpretare |
|---|---|---|
| 200 | 83 | OK |
| 422 | 10 | Query params obligatorii lipsă la apelul „gol" — **așteptat**, nu bug (ex. `/api/compare` are nevoie de `?ids=`) |
| **500** | **5** | **Rupt — vezi detaliere completă mai jos** |
| 401 | 1 | `/api/gsc/oauth/sites` fără sesiune — comportament corect |
| 400 | 1 | `/api/gsc-fanout/callback` fără parametri OAuth — comportament corect |
| 307 | 1 | `/api/gsc/oauth/callback` redirect — comportament corect la GET direct fără `code` |

Cele 119 path-uri cu parametri de rută (`{id}`, `{website_id}` etc.) nu au fost
apelate — ar necesita date reale din DB și depășesc scopul unui smoke test
generic. Recomandare pentru o trecere viitoare: alegeți 2-3 ID-uri reale din
`api/data/analyzer.db` și repetați testul țintit pe acestea.

---

## Cele 4 endpoint-uri 500 — root cause complet

### F2-01 — GSC OAuth authorize: variabilă nedefinită

```python
# api/routes/gsc/oauth_sync.py:81
flow = Flow.from_client_config(_CLIENT_CONFIG, scopes=_GSC_SCOPES)
                               ^^^^^^^^^^^^^^
NameError: name '_CLIENT_CONFIG' is not defined
```

Cel mai simplu bug din tot auditul: o variabilă referită dar niciodată
definită în acest fișier. Efect: **niciun utilizator nu poate iniția conexiunea
GSC** — primul pas al flow-ului OAuth crapă instant. Notă: `api/routes/gsc/_shared.py`
are o funcție `_client_config()` echivalentă (linia 54, verificată în Faza 4) —
foarte probabil `oauth_sync.py` trebuia să o importe de acolo și cineva a uitat,
sau a fost redenumită variabila într-un refactor și acest apel n-a fost updatat.
Fix probabil: `from ._shared import _client_config` + apel, în loc de referință
directă la un nume inexistent.

### F2-02 — Portfolio: comparație datetime naive/aware

```python
# api/routes/portfolio.py:344
days_since = (datetime.now(timezone.utc) - last_audit).days
              ~~~~~~~~~~~~~~~~~~~~~~~~~~~^~~~~~~~~~~~
TypeError: can't subtract offset-naive and offset-aware datetimes
```

Exact clasa de bug pe care CLAUDE.md o semnalează explicit (regula 6:
`datetime.now(timezone.utc)` da, `datetime.utcnow()` nu) — aici efectul opus:
codul folosește corect varianta aware, dar `last_audit` (probabil citit din
coloana `Audit.created_at`) e naiv — fie fiindcă a fost scris cu
`datetime.utcnow()` undeva în trecut, fie SQLite/SQLAlchemy returnează
timestamp-ul fără tzinfo. **Rupe simultan 2 endpoint-uri** (`/api/portfolio/alerts`
direct, și `/api/portfolio/overview` care cheamă aceeași funcție intern la
linia 365 → 402). Funcția `detect_alerts_for_website` (linia 344) e complet
inutilizabilă azi — orice website cu cel puțin un audit în DB declanșează
crash-ul (nu doar un caz-limită).

### F2-03 + F2-04 + F2-05 — Drift schemă DB: migrări/model niciodată sincronizate cu baza reală

Acesta e cel mai important lanț de findings al Fazei 2. Root cause complet,
verificat prin execuție directă + inspecție schema DB:

**Pasul 1 — simptom:** `/api/briefs` și `/api/fanout/sessions` dau 500 cu
`sqlite3.OperationalError: no such column: ...`.

**Pasul 2 — schema reală vs. model:**
```
content_briefs (DB are 10 coloane) — modelul cere 12: lipsesc
   'current_score', 'executive_summary'

fanout_sessions (DB are 13 coloane) — modelul cere 23: lipsesc TOATE cele 10:
   'query_origin', 'source_origin', 'prompt_cluster', 'run_cost_usd',
   'locale', 'language', 'confidence_score', 'engine', 'model_version',
   'from_cache'
```
(confirmat cu `PRAGMA table_info()` direct pe `api/data/analyzer.db`)

**Pasul 3 — de ce lipsesc:**
- Pentru `fanout_sessions`: există deja cod scris pentru exact această
  reparație — `api/models/database.py:230-260`, funcția `init_db_async()`,
  conține lista completă a celor 10 coloane cu `ALTER TABLE ... ADD COLUMN`
  condiționat (rulează doar dacă lipsesc). **Dar `init_db_async()` nu e
  apelată de nicăieri în tot proiectul** (`grep -rn "init_db_async"` → doar
  definiția + re-exportul din `api/models/__init__.py`, zero invocări).
  `api/main.py:71` cheamă în schimb `init_db()` — versiunea **sincronă**
  (linia 134 din același fișier), care are propriile migrări ad-hoc pentru
  `benchmark_projects`, `gsc_properties`, `url_guides` — dar **nimic pentru
  `fanout_sessions`**.
- Pentru `content_briefs.current_score`/`executive_summary`: **nu există nicio
  migrare, nici Alembic, nici ad-hoc**, pentru aceste 2 coloane, în niciuna
  din cele două funcții. Au fost adăugate direct în modelul SQLAlchemy
  (`api/models/content.py:28`) și niciodată propagate la schema reală.
- Alembic separat: DB-ul e la revizia **`0005`**, head-ul e **`0008`**
  (`alembic current` vs `alembic heads`, verificat live) — dar niciuna din
  migrările `0006`-`0008` nu atinge `content_briefs` sau `fanout_sessions`
  (sunt exclusiv ClusterIQ/SerpIQ) — deci un simplu `alembic upgrade head`
  **nu ar repara aceste 2 endpoint-uri**.

**Concluzie:** proiectul are **trei mecanisme de schema-sync paralele și
nesincronizate** — Alembic (8 migrări, DB la 0005/8), `init_db()` sincron
(3 tabele cu auto-heal, activ, dar incomplet), și `init_db_async()` (10
coloane cu auto-heal, complet, dar **mort — niciodată executat**). Niciunul
dintre ele nu acoperă `content_briefs.current_score`/`executive_summary`.

**Recomandare (Faza 9, nu de implementat acum):**
1. Fix imediat, cu risc minim: în `api/main.py:71`, fie chemați
   `await init_db_async()` în loc de `init_db()` (dar verificați întâi
   diferențele exacte dintre cele două — au și cod duplicat pentru
   `benchmark_projects`/`gsc_properties`/`url_guides`, deci fuzionarea
   trebuie făcută atent, nu doar swap), fie adăugați manual 2 linii de
   auto-heal pentru `content_briefs` după modelul celor existente.
2. Pe termen mediu: consolidați totul sub Alembic și eliminați cele două
   funcții `init_db*` ad-hoc — trei surse de adevăr pentru schema DB e
   exact tipul de risc care a produs acest bug.
3. Rulați un audit complet coloană-cu-coloană model↔DB pentru **toate**
   tabelele, nu doar cele 2 descoperite accidental prin smoke test — e
   foarte probabil să mai existe drift similar în alte tabele neexersate de
   acest test (ex. tabele atinse doar de POST/PUT, nu de GET-urile testate
   aici). **Acesta e exact scopul Fazei 3** — recomand rularea ei imediat
   după acest raport, cu acest root-cause ca punct de plecare.

---

## Concluzie Faza 2

Sistemul de înregistrare a rutelor (routere, rate limiting, fetch↔endpoint) e
**foarte solid** — trei verificări separate, toate curate. În schimb, smoke
test-ul simplu pe GET-uri fără parametri a scos la iveală **4 endpoint-uri
100% rupte din 102 testate (~4%)**, dintre care 3 au cauze rădăcină complet
identificate și necesită fix-uri punctuale de câteva linii fiecare. Cel mai
grav dintre ele (F2-05) e un bug de proces — o funcție de migrare scrisă
corect dar niciodată conectată — care ar putea explica alte disfuncții
tăcute nedescoperite încă de acest audit.
