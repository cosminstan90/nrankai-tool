# Plan de implementare — consolidare nrankai-tool

> **Pentru cine:** sesiuni Claude Sonnet care execută consolidarea, etapă cu etapă.
> **Obiectiv final:** un tool de audit **SEO / GEO / AEO** coerent — aceleași
> capabilități, într-o structură în care fiecare lucru se face într-un singur loc.
> **Precedent:** acest plan continuă auditul complet din `docs/CODEBASE_AUDIT_PLAN.md`
> (rapoarte în `docs/audit/`). Secțiunea 0 rezumă tot ce s-a găsit — nu e nevoie
> să recitești rapoartele ca să începi.

---

## 0. Ce s-a făcut anterior — rezumatul auditului

Pe 2026-09-01 s-a executat un audit complet în 9 faze (read-only, fiecare finding
verificat prin citire de cod, execuție directă sau apel live pe server).
Rapoartele complete: `docs/audit/00-baseline.md` … `08-frontend.md`, consolidat în
`docs/audit/FINDINGS.md`.

### Cifrele care contează

| Măsură | Valoare |
|---|---|
| Fișiere `.py` / LOC | 179 / 61.916 |
| Path-uri API / operații | 315 / 367 |
| Module de rute | 63 (+ 34 workers, 57 template-uri, 20 prompturi) |
| Tabele în DB | 81 — din care **59 complet goale** |
| Audituri rulate vreodată | 84 (62 reușite, 22 eșuate) |
| Distribuția lor | **75% GEO** (`SINGLE_GEO_AUDIT` singur = 57%) |
| Tipuri de audit folosite | 9 din 20 |
| Sesiuni Fan-Out rulate | **0** (modulul e rupt din construcție) |
| Ultimul commit | 2026-05-28 (95 de zile înainte de audit) |

### Cele 7 probleme P0 (rupte în producție)

| # | Problemă | Locație |
|---|---|---|
| 1 | `NameError: _CLIENT_CONFIG` — conectarea GSC imposibilă, 100% din timp | `api/routes/gsc/oauth_sync.py:81` |
| 2 | `TypeError` naive/aware datetime — rupe 3 endpoint-uri Portfolio | `api/routes/portfolio.py:344` **și** `:609` |
| 3 | `no such column: content_briefs.current_score` — lipsesc 2 coloane | model `api/models/content.py:28` vs. DB |
| 4 | `no such column: fanout_sessions.query_origin` — lipsesc **10 coloane**; blochează și citirea **și scrierea** (ORM la `fanout.py:184`) | idem |
| 5 | Cauza rădăcină a #3/#4: `init_db_async()` conține migrarea corectă dar **nu e apelată niciodată**; `main.py:71` cheamă `init_db()` (sincron, incomplet) | `api/models/database.py:193` |
| 6 | Blocul de auto-înregistrare webhook n8n **fără `try/except`** — poate pica tot serverul la pornire | `api/main.py:195-205` |
| 7 | Fallback-uri de model retrase: `claude-3-5-sonnet-20241022` (retras 2025-10-28) și `llama-3.1-sonar-large-128k-online` (schema veche Perplexity) | `api/routes/citation_tracker.py:228,230,260,262` |

Plus, din Faza 3: **starea Alembic e fictivă** — DB la revizia `0005`, head la `0008`,
dar toate tabelele există deja (create de `Base.metadata.create_all()`, nu de Alembic).
Un `alembic upgrade head` rulat naiv ar eșua cu „table already exists".

### Duplicarea găsită (motivul acestui plan)

**Infrastructură:**
- 3× `clean_json_response()` (`core/direct_analyzer.py`, `api/routes/summary.py`, `api/routes/schema_gen.py`) — cu robustețe inegală
- 3× flow OAuth Google (`gsc/_shared.py`, `gsc_fanout.py`, `workers/contentiq/gsc.py`) — doar primul tratează `invalid_grant`
- 2× validator SSRF (`api/utils/url_validator.py` **nu rezolvă DNS**; `workers/lead_audit_worker.py` **rezolvă corect**)
- 3× mecanism de migrare schemă (Alembic + `init_db()` + `init_db_async()`)
- 17× instanțiere directă de client LLM, deși `AsyncLLMClient` există și e folosit corect de 3 fișiere

**Features:**
- `citation_tracker.py` + `geo_monitor.py` = **același feature scris de două ori**
  (aceleași nume de funcții: `_query_provider`, `start_scan`, `get_scan`, același ciclu
  creează→generează queries→scanează→trend→alertă). `ai_visibility.py` există exclusiv
  ca să le agrege înapoi — dovada că trebuiau să fie unul singur.
- 11 module pentru „vizibilitate în AI" (~87 operații)
- 5 module care transformă rezultate de audit în recomandări (`action_cards`, `content_briefs`, `guide`, `insights`, `summary`)
- 6 module pentru gap/comparație (`content_gaps`, `compare`, `gap_analysis`, `cross_reference`, `benchmarks`, `multilingual`)
- 3 motoare de audit (`audits`, `content_iq` cu engine-uri proprii E-E-A-T/freshness, `draft_optimizer`)
- 3 implementări identice de upload CSV (`gsc`, `ga4`, `ads`)

**Cauza:** docstring-urile o spun — `projects.py` „(Prompt 25)", `entity.py` „(Prompt 31)",
`mention_seeding.py` „(Prompt 32)", `bot_access.py` „(Prompt 33)", `cocitation.py` „(Prompt 34)",
`answer_calibration.py` „(Prompt 35)", `multilingual.py` „(Prompt 36)". Fiecare idee nouă a
primit router + tabel + pagină proprii, niciodată pliate peste ce exista.
`answer_calibration.py` e cel mai elocvent: modul separat, dar endpoint-urile lui trăiesc
sub `/api/fanout/sessions/{id}/...`.

---

## 1. Principii — citește-le înainte de orice etapă

1. **Consolidăm, NU ștergem features.** Un feature nefolosit dar bine gândit devine
   un *mod* în domeniul lui, nu dispare. Singurele lucruri care se șterg sunt: cod
   mort dovedit (toolchain CLI legacy), duplicate exacte, și artefacte (worktrees,
   fișiere `.db` de 0 bytes).
2. **REGULA DE AUR: URL-urile publice nu se schimbă.** Există 192 de apeluri `fetch()`
   în template-uri, toate verificate ca funcționale (Faza 2 F2-08). Consolidarea
   schimbă **organizarea codului**, nu suprafața API. Un router mutat își păstrează
   `prefix`-ul. Dacă o rută chiar trebuie mutată, lași un alias către cea veche.
3. **O etapă per sesiune.** Fiecare etapă se termină cu smoke test verde + commit.
   Nu începe etapa N+1 în aceeași sesiune cu etapa N.
4. **Nu atinge `prompts/` și `api/prompts/`** decât în Etapa 6, care e explicit despre
   ele. (Regula din CLAUDE.md.)
5. **Verifică, nu presupune.** Fiecare etapă are o secțiune „Verificare" — rulează-o
   efectiv și pune output-ul în raport. Fără „ar trebui să meargă".
6. Nu porni sub-agenți. Nu face refactor oportunist în afara scopului etapei curente.

### Reguli de angajament pentru refactoring (diferite de cele de audit)

- **Înainte de fiecare etapă:** `git status` curat (sau stash), apoi branch nou
  `git checkout -b consolidare/etapa-N`.
- **Exclude mereu** `.claude/worktrees/` din orice scan (conține copii complete ale repo-ului).
- **După fiecare etapă:** rulează smoke test-ul (Etapa 0), compară cu baseline-ul,
  și explică orice diferență. Zero diferențe neexplicate = condiție de merge.
- **Nu comite** dacă smoke test-ul e mai roșu decât baseline-ul.
- Serverul se pornește cu `restart_server.bat` (setează `PYTHONUTF8=1`, necesar pe Windows).

---

## Etapa 0 — Plasă de siguranță

**De ce prima:** urmează să reorganizezi 63 de module și 315 endpoint-uri într-un
proiect cu **un singur fișier de test, care nici măcar nu rulează**. Fără o plasă de
regresie, orice consolidare e pe încredere.

### 0.1 Repară singurul test existent
`test_content_chunker.py` stă la rădăcină dar face `from content_chunker import ...`,
în timp ce modulul e la `core/content_chunker.py`. Nu există `conftest.py` sau config
pytest. Rulat cu `PYTHONPATH=core`, **33 din 34 de teste trec**.

Singurul eșec (`test_content_chunker.py:491`) așteaptă câmpul `ai_citation_likelihood`,
dar codul de producție folosește deja `citation_probability` — care e numele corect din
`prompts/geo_audit.yaml`. **Testul e învechit, codul e corect.**

- mută fișierul în `tests/` (sau adaugă `conftest.py` cu `pythonpath`)
- actualizează linia 491: `ai_citation_likelihood` → `citation_probability`
- adaugă `pytest` în `requirements.txt` (lipsește complet azi)

### 0.2 Construiește smoke test-ul de regresie
Ăsta e instrumentul principal de siguranță pentru tot restul planului. A fost deja
folosit în Faza 2 și a găsit toate cele 4 endpoint-uri rupte.

```bash
# 1. pornește serverul, apoi:
curl -s http://127.0.0.1:8000/openapi.json > tests/baseline/openapi.json

# 2. extrage GET-urile fără parametri de rută
python -c "
import json
d = json.load(open('tests/baseline/openapi.json', encoding='utf-8'))
paths = sorted(p for p, ops in d['paths'].items() if 'get' in ops and '{' not in p)
open('tests/baseline/get_paths.txt','w',encoding='utf-8',newline='\n').write('\n'.join(paths))
print(len(paths), 'paths')
"

# 3. rulează smoke test-ul  ⚠️ ATENȚIE: fișierul TREBUIE scris cu newline='\n'.
#    Cu CRLF (default pe Windows) curl primește URL-uri corupte și întoarce 000 la tot.
while IFS= read -r p; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "http://127.0.0.1:8000${p}")
  echo "$code  $p"
done < tests/baseline/get_paths.txt > tests/baseline/smoke_results.txt

awk '{print $1}' tests/baseline/smoke_results.txt | sort | uniq -c | sort -rn
```

**Baseline-ul cunoscut (2026-09-01), din 102 GET-uri:**

| Cod | Nr. | Interpretare |
|---|---|---|
| 200 | 83 | OK |
| 422 | 10 | query params obligatorii lipsă la apel gol — **așteptat** |
| 500 | 5 | rupt (4 cauze distincte — vezi P0 #1-#4) |
| 401 / 400 / 307 | 1 / 1 / 1 | comportament corect pe rute OAuth fără parametri |

Împachetează pașii 2-3 într-un script (`tests/smoke.sh` sau `.py`) care întoarce exit
code ≠ 0 dacă apare vreun 500 nou față de baseline.

### 0.3 Snapshot al suprafeței API
`tests/baseline/openapi.json` devine referința. După fiecare etapă de consolidare,
diff-ul de path-uri trebuie să fie **gol** (regula de aur #2). Un script mic de diff
face verificarea trivială.

**Definiție de gata:** `pytest` verde (34/34), smoke test rulabil cu o comandă,
baseline commis în `tests/baseline/`.

---

## Etapa 1 — Fix P0

Independente între ele, cu excepția #3+#4+#5+Alembic care sunt aceeași problemă.
Sunt toate fix-uri mici; scopul e ca smoke test-ul să ajungă la **0 × 500**.

### 1.1 GSC OAuth (P0 #1) — ~5 minute
`api/routes/gsc/oauth_sync.py:81` referă `_CLIENT_CONFIG`, care nu e definit nicăieri
în fișier. `api/routes/gsc/_shared.py:54` are deja funcția `_client_config()`, corectă
și folosită de restul flow-ului. Importă și apeleaz-o.

**Verificare:** `GET /api/gsc/oauth/authorize` întoarce un redirect 302/307 către
`accounts.google.com`, nu 500. În browser: butonul „Connect Google" din `/gsc` duce la
ecranul de consimțământ Google.

### 1.2 Timezone Portfolio (P0 #2) — ~10 minute
`api/routes/portfolio.py:344` **și `:609`** — scad un datetime naiv (citit din DB)
dintr-unul aware. Pattern-ul corect **există deja în proiect**, la `api/routes/audits.py:609-612`:

```python
if started.tzinfo is None:
    started = started.replace(tzinfo=timezone.utc)
```

Aplică-l în ambele locuri. Cauza structurală: toate cele **115** coloane `DateTime`
din `api/models/` sunt fără `timezone=True`, deci SQLite+SQLAlchemy întorc naiv orice
valoare la citire. **NU converti cele 115 coloane acum** — e o schimbare mare, separată;
notează-o ca datorie tehnică.

**Verificare:** `/api/portfolio/alerts`, `/api/portfolio/overview` și
`/api/portfolio/website/{domain}` (cu un domeniu real din DB) întorc 200. În browser,
`/portfolio` afișează conținut, nu pagină goală.

### 1.3 Drift de schemă + Alembic (P0 #3, #4, #5) — ~30 minute
Cele 12 coloane lipsă, confirmate prin diff exhaustiv model↔DB pe toate cele 80 de tabele
(doar astea două tabele au drift):

```
content_briefs:  current_score, executive_summary
fanout_sessions: query_origin, source_origin, prompt_cluster, run_cost_usd,
                 locale, language, confidence_score, engine, model_version, from_cache
```

Pași, în ordine:
1. `alembic stamp head` — sincronizează `alembic_version` la `0008` fără DDL
   (tabelele există deja; e corect aici, nu e un hack).
2. Scrie migrarea `0009_add_missing_columns.py` cu cele 12 `ADD COLUMN`.
   Valorile default sunt deja documentate în `api/models/database.py:238-249`
   (`query_origin` default `'actual'`, `source_origin` default `'citation'`, etc.).
3. `alembic upgrade head`.
4. **Rezolvă cauza rădăcină:** `api/main.py:71` cheamă `init_db()`, dar migrarea pentru
   `fanout_sessions` trăiește în `init_db_async()`, care nu e apelată de nimeni.
   Cele două funcții au și cod duplicat (pentru `benchmark_projects`, `gsc_properties`,
   `url_guides`). **Nu face swap naiv.** Recomandare: acum că Alembic e sursa de adevăr,
   scoate blocurile de auto-migrare ad-hoc din ambele funcții și lasă `init_db()` doar
   cu `create_all()` + seed-ul de template-uri. Șterge `init_db_async()`.

**Verificare:** `alembic check` trece. `GET /api/briefs` și `GET /api/fanout/sessions`
întorc 200. `PRAGMA table_info` confirmă coloanele. Rulează scriptul de diff model↔DB
din `docs/audit/03-data-layer.md` — trebuie să dea zero drift.

### 1.4 Robustețe startup (P0 #6) — ~2 minute
`api/main.py:195-205` — înfășoară blocul n8n în `try/except`, după modelul blocului
imediat următor din același fișier („Seed prompt library", linia 208+), care e deja protejat.

### 1.5 Model IDs (P0 #7) — ~15 minute
`api/routes/citation_tracker.py` își hardcodează propriile fallback-uri, în loc să
folosească `api/provider_registry.py`. Două sunt moarte:
- linia 228, 230: `claude-3-5-sonnet-20241022` — **retras oficial 2025-10-28**
- linia 260, 262: `llama-3.1-sonar-large-128k-online` — schema veche Perplexity

**Fix corect:** înlocuiește fallback-urile hardcodate cu
`from api.provider_registry import get_default_model` → `model or get_default_model(provider)`.
Asta previne și recurența.

> ⚠️ Înainte să scrii orice ID de model Anthropic, **încarcă skill-ul `claude-api`**.
> Nu scrie ID-uri din memorie.

**Definiție de gata pentru Etapa 1:** smoke test 0 × 500; diff de path-uri OpenAPI gol;
`pytest` verde.

---

## Etapa 2 — Infrastructură comună + modernizare LLM

Se face într-o singură trecere pentru că modernizarea **șterge** duplicarea, nu se
adaugă peste ea. Zero schimbări în suprafața API.

### 2.1 Un singur parser JSON — prin structured outputs
Azi există 3 implementări cu robustețe inegală:

| Locație | Reparare |
|---|---|
| `core/direct_analyzer.py` | fences → `json.loads` → regex → librăria `json_repair` |
| `api/routes/summary.py` | fences → `_repair_json()` local |
| `api/routes/schema_gen.py` | **doar** fences, nicio reparare |

În loc să le fuzionezi în una singură: folosește **structured outputs**
(`output_config: {format: {...}}`) pe apelurile către Anthropic/OpenAI. Modelul
întoarce JSON valid garantat față de schemă → dispar toate cele 3 implementări plus
dependența `json_repair`.

Prompturile au deja `output_schema` în YAML (verificat: 18/18 se potrivesc exact cu
cheile citite de `core/direct_analyzer.py::get_prefix_for_audit`) — deci schema pentru
structured outputs se poate genera din ce există.

> ⚠️ **Schimbare de comportament, nu doar de cod.** Validează pe un singur tip de audit
> întâi — **GEO** (75% din utilizare) — și compară output-ul cu un rezultat cunoscut
> bun din `audit_results` (6.352 rânduri disponibile). Abia apoi extinde.
> Încarcă skill-ul `claude-api` pentru sintaxa exactă.

### 2.2 Un singur client LLM
17 fișiere instanțiază direct `AsyncAnthropic`/`AsyncOpenAI`. `AsyncLLMClient` din
`core/direct_analyzer.py` există și e folosit corect de `audits.py`,
`lead_audit_worker.py`, `compare.py`.

**Migrează incremental, un fișier per commit**, nu big-bang. Ordinea recomandată
(de la cel mai folosit): `citation_tracker`, `geo_monitor`, `summary`, `schema_gen`,
`meta_generator`, `query_suggestions`, `answer_calibrator`, apoi workers.

Cu clientul unificat, adaugă **o singură dată**, în el:
- **prompt caching** pe system prompt (cele 20 de prompturi sunt ~11,5KB fiecare și
  perfect stabile; `god_mode` le trimite pe toate → economie directă; ai 5.034 rânduri
  în `cost_records`, deci costul contează)
- **modelele curente** (vezi 2.3)
- retry/timeout/error-handling consecvent (azi diferă de la fișier la fișier)

### 2.3 Migrare modele
`claude-sonnet-4-20250514` e modelul implicit al întregii aplicații — **17 fișiere,
38 apariții** — și e oficial *deprecated*. Migrarea către `claude-sonnet-5` e și mai
ieftină ($2/$10 vs $3/$15 per MTok) și mai bună.

Folosește `/claude-api migrate` cu scope explicit. Nu e doar swap de string: se schimbă
comportamentul de thinking/effort. Puncte de intrare: `api/provider_registry.py:67-91`
(sursa de adevăr pentru UI) și `core/config.py`.

### 2.4 Un singur validator SSRF
Două implementări; cea de pe calea principală (`api/utils/url_validator.py`, folosită de
`audits.py:142,462` și `gsc/optimizer.py:124`) **nu rezolvă DNS**, deci nu prinde un
domeniu care rezolvă către un IP privat. Cea din `api/workers/lead_audit_worker.py:36`
rezolvă corect și verifică față de rețelele blocate.

**Mută implementarea bună în `api/utils/url_validator.py`** și refolosește-o peste tot.

### 2.5 Un singur flow OAuth Google
Trei implementări: `gsc/_shared.py` (singura care tratează `invalid_grant`, din commit
`62b2155`), `gsc_fanout.py` (`except Exception` generic), `workers/contentiq/gsc.py`.
Extrage un `api/utils/google_oauth.py` comun, cu gestionarea `invalid_grant` din prima.

**Definiție de gata:** smoke test la nivelul baseline-ului; un audit GEO real rulat
end-to-end cu output valid; `cost_records` arată tokeni cache-uiți pe al doilea audit
identic.

---

## Etapa 3 — Primul domeniu: `visibility/`

Cea mai clară fuziune din proiect și proof-of-concept pentru tiparul restului.

### Ce se unește

| Modul | ops | Rânduri în DB |
|---|---|---|
| `citation_tracker.py` | 9 | `citation_trackers` 1, `citation_scans` 5 |
| `geo_monitor.py` | 8 | `geo_monitor_projects` 0, `geo_monitor_scans` 0 |
| `ai_visibility.py` | 2 | — (doar agregator) |

Sunt același ciclu de viață, cu vocabular diferit:

| citation_tracker | geo_monitor | rol comun |
|---|---|---|
| `create_tracker` / `list_trackers` / `delete_tracker` / `toggle_tracker` | `create_project` / `list_projects` / `delete_project` / `update_project` | CRUD țintă |
| `generate_citation_queries` | `generate_suggested_queries` | generare interogări |
| `_query_provider` | `_query_provider` | interogare LLM |
| `_run_citation_scan` | `_run_geo_scan` | rulare scan |
| `extract_cited_urls` + `check_brand_mention` | `_analyze_response` | analiză răspuns |
| `_check_and_alert_citation_drop` | `_check_and_alert_visibility_drop` | alertă la scădere |
| `get_tracker_trend` | `get_trend` | trend Chart.js |
| `check_and_run_citation_scans` (cron) | — | rulare programată |

### Model de date țintă

```
VisibilityTarget    (domeniu/brand + limbă + setări alertă + cron opțional)
  └── VisibilityQuery      (interogările urmărite)
        └── VisibilityScan       (o rulare, per provider)
              └── VisibilityHit  (mențiune | citare + URL + context + poziție)
```

**Costul migrării de date e aproape zero:** `geo_monitor_*` are 0 rânduri, iar
`citation_*` are 1 tracker + 5 scanări. Pornește de la tabelele `citation_*` (au datele),
adaugă câmpurile distinctive din `geo_monitor` ca noi coloane, migrează 0 rânduri din
`geo_monitor`. **Fă fuziunea acum, cât e ieftină** — cu cât aștepți, cu atât crește costul.

### Compatibilitate

Păstrează **toate** prefixele existente (`/api/citations/*`, `/api/geo-monitor/*`,
`/api/ai-visibility/*`) montate pe noul router. Template-urile
(`citation_tracker.html`, `geo_monitor.html`, `ai_visibility.html`) rămân neatinse în
această etapă — unificarea UI e o etapă separată, după ce backend-ul e stabil.

**Definiție de gata:** cele 19 operații răspund identic ca înainte; datele existente
(1 tracker, 5 scanări) vizibile în UI; diff de path-uri OpenAPI gol.

---

## Etapa 4 — Fan-Out reparat și integrat

`fanout` e cel mai mare modul din proiect (**45 de operații**) și n-a rulat niciodată,
pentru că `fanout.py:184` instanțiază `FanoutSession(...)` prin ORM, care încearcă să
scrie toate cele 23 de coloane din model în timp ce DB-ul are 13 — deci **și citirea și
scrierea** eșuau. Etapa 1.3 repară asta.

### 4.1 Validează că funcționează — ✅ FĂCUT (2026-09-02)
După fix-ul de schemă: rulează efectiv o analiză Fan-Out din UI (`/fanout`) și confirmă
că sesiunea se salvează, sursele se populează, iar „Recent Sessions" afișează rezultatul.
**Până acum nimeni nu a văzut acest feature funcționând — nu presupune că restul
codului e corect doar pentru că se salvează un rând.** Verifică tot lanțul:
`fanout_sessions` → `fanout_queries` → `fanout_sources`.

Rezultat: **confirmat live** — analiză reală prin `POST /api/fanout/analyze`, toate
cele 3 tabele populate corect, endpoint-uri derivate (`/coverage`, `/action-cards`,
`/composite-score`) cu rezultate sensibile pe date reale, UI (`/fanout`) randează
corect, zero erori de consolă. Fix-ul din Etapa 1 a fost suficient — niciun bug nou
în calea principală analyze→save.

### 4.2 Absoarbe sateliții — ⚠️ PREMISĂ CORECTATĂ (2026-09-02)
**Textul original de mai jos (tăiat) presupunea greșit, dintr-o citire superficială
(doar docstring-ul), că `projects.py` e duplicat de `visibility/`. După citire completă
și testare live, s-a confirmat că NU e — vezi „Ce s-a găsit în schimb" mai jos.**

~~Odată ce Fan-Out e viu, aceste module devin vederi peste el, nu module separate:~~
~~- `projects.py` (9 ops) — e literalmente „Fan-Out Project Management (Prompt 25)",~~
~~  CRUD pentru `FanoutProject`. Numele e înșelător; mută-l în `visibility/`.~~

**Ce s-a găsit în schimb:** `FanoutProject` e un concept distinct și legitim —
un „workspace per client" (`target_domain` + `target_brand` + `vertical` + `locale`,
folosit pentru comparație cu benchmark-uri de industrie) care **grupează** sesiuni
Fan-Out și configurări de tracking, nu un tracker de citări/mențiuni ca
`CitationTracker`. Nu se absoarbe în `visibility/` — rămâne un nivel de organizare
separat, posibil deasupra lui `visibility/` într-o eventuală ierarhie viitoare
(un client poate avea mai multe target-uri de vizibilitate urmărite).

**Bug real găsit și reparat, verificat live:** `projects.py` lega o sesiune de
proiectul ei scriind UUID-ul proiectului în `FanoutSession.audit_id` — o coloană
declarată ca FK real către `audits` (`ForeignKey("audits.id")`), semnalat chiar în
comentariul codului original ca „hack". Funcționa doar pentru că
`PRAGMA foreign_keys=ON` (`api/models/database.py`) e aplicat doar pe `sync_engine`,
niciodată pe motorul async pe care merg toate request-urile reale — deci FK-ul nu
era de fapt impus niciodată. **Reparat**: coloană dedicată `FanoutSession.project_id`
(migrarea `0011`), fără FK (consecvent cu convenția deja folosită de
`FanoutTrackingConfig.project_id`/`FanoutCompetitiveReport.project_id`).

**Constatare sistemică, nereparată acum** (risc mai larg decât acest caz):
verificarea de FK a SQLite e activă doar la pornirea sincronă (`init_db()`), nu și
pe motorul async folosit de toată aplicația live — deci **nicio constrângere de
foreign key din schema declarată nu se impune de fapt la runtime**, pentru nicio
tabelă. Nu s-a atins acum (risc de a strica ceva dacă există deja încălcări de FK
în date, needs-runtime-check separat) — de investigat separat dacă se dorește
integritate reală de date.

### 4.4 `answer_calibration.py` — ✅ validat live, 2 bug-uri de crash reparate (2026-09-02)
Nu era duplicat cu nimic — 3 endpoint-uri deja corect plasate sub
`/api/fanout/sessions/{id}/...`. Testat live end-to-end (sesiune reală, crossref
inserat manual cu gap_queries), au ieșit la iveală 2 bug-uri reale, ambele de tip
„scris corect sintactic, mort la prima rulare":
- `calibrate-all-gaps` interoga `FanoutCrossRefResult.fanout_session_id`, atribut
  care nu a existat niciodată (coloana reală e `session_id`) — 100% crash (500),
  de la prima rulare a acestui endpoint, vreodată.
- atât `calibrate()` cât și `calibrate-all-gaps` tratau `result_json` (coloană
  `Text`, string JSON) ca și cum ar fi deja dict — `.get()` pe string → crash.

Reparat, verificat live (toate 3 endpoint-urile, ambele ramuri crossref_id/
project_id), pytest+smoke+api_diff verde. Commit `ab94724`, merge `ab0cc30`.

### 4.5 `gsc_fanout.py` — ✅ examinat, nu se fuzionează, bug de design reparat (2026-09-02)
Modulul (Prompt 27, OAuth GSC + crossref cu sesiuni Fan-Out) e legitim și diferit
de restul — nu se absoarbe nicăieri. În timpul validării live a ieșit la iveală
un bug mai profund decât un typo:

**Bug găsit:** `crossref_fanout_gsc()` (`api/workers/gsc_fanout_crossref.py`) și
endpoint-ul separat `validate-serp` din `fanout.py` (Prompt 19, validare SERP
live via Serper.dev) calculau **independent, cu cod copy-pasted**, aceeași
matrice de 4 categorii (`synced`/`ai_gap`/`ai_only`/`double_gap`), cu
`ai_found = True` hardcodat („by definition — these are fan-out queries").
Asta făcea `ai_gap` și `double_gap` structural imposibil de populat în ambele
locuri — nu exista nicăieri în schema de date un semnal real „AI-ul chiar a
citat brandul meu la ACEASTĂ interogare" per query (`FanoutSource`, tabelul de
citări, e la nivel de sesiune, nu legat de `FanoutQuery` individual).
`traffic_at_risk` era deci mereu 0, iar „GEO gap prioritar" (scopul declarat al
feature-ului) nu a funcționat niciodată.

**Verificat înainte de fix:** `SERPER_API_KEY` nu e configurat deloc în `.env`
(deci `validate-serp` n-a fost niciodată apelabil în acest deployment) și
`validate-serp` nu are nicio referință în UI — spre deosebire de `gsc-fanout`,
conectat live în `projects.html` (connect/status/disconnect). Cele două module
nu sunt totuși duplicate pure: `validate-serp` funcționează pentru orice
domeniu (inclusiv competitori, fără OAuth), `gsc-fanout` doar pentru domeniul
propriu verificat, dar cu istoric real de clickuri/impresii — capabilități
diferite, nu doar aceeași funcție de două ori.

**Decizie (aprobată explicit):** păstrează ambele module (capabilități reale
diferite), dar extrage logica de clasificare duplicată într-un helper comun
`api/utils/serp_classification.py`, simplificată la 2 categorii care reflectă
ce chiar există în date — `synced` (domeniul țintă rankează deja în căutare
reală) / `gap` (nu rankează) — în loc de matricea de 4 cu jumătate din bucket-uri
mereu goale. Ambele apeluri (`crossref_fanout_gsc` și `validate_serp`) folosesc
acum același helper. Test de regresie: `tests/test_serp_classification.py`.

Nicio referință UI la `ai_gap`/`ai_only`/`double_gap`/`traffic_at_risk` (verificat
prin grep în `api/templates`/`api/static`) — schimbarea de formă a răspunsului
API e sigură.

Rămân neexaminate: `cocitation`, `mention_seeding`, `entity`, `serpiq` — fiecare
cu tabelul propriu, de citit complet (nu doar docstring) înainte de orice decizie
de fuziune.

### 4.6 `cocitation.py` — ✅ validat live, bug de crash reparat (2026-09-02)
Modul legitim și distinct (Prompt 34 — hartă de co-citare: ce domenii apar
alături de brandul țintă în sursele citate de AI, clasificate director/
review-site/competitor). Nu se fuzionează cu nimic — nu se suprapune cu
`visibility/` sau cu `gsc_fanout.py` (acelea măsoară prezența brandului
propriu; cocitation măsoară cu cine apare brandul, nu dacă apare).

**Bug găsit:** `build_cocitation_map()` (`api/workers/cocitation_analyzer.py`)
citea `FanoutSource.source_url`, atribut care nu a existat niciodată pe acel
model (coloana reală e `url` — `api/models/content.py:575`). Crash garantat
(`AttributeError`) la fiecare apel — `/api/cocitation/analyze` nu a funcționat
niciodată, de la implementare. Exact același tipar ca la `answer_calibration.py`
și `gsc_fanout_crossref.py`: cod scris sintactic corect, referință la o
coloană care nu există, niciodată exercitat până acum.

Reparat cele 2 apariții (`s.source_url` → `s.url`). Notă: modelul are și un
câmp `domain` precalculat, dar acesta e doar netloc fără „www.” (nu root
domain adevărat — un subdomeniu ca `blog.example.com` rămâne neschimbat), pe
când modulul își calculează singur root domain-ul via `_root_domain(url)`
pentru gruparea corectă a competitorilor. Am păstrat acest calcul propriu
(mai precis pentru scopul modulului), am corectat doar numele atributului.

Verificat live: sesiune + surse reale inserate în DB, `build_cocitation_map()`
apelat direct (clasificare corectă competitor/review_site), apoi toate 2
endpoint-urile prin HTTP (`analyze` cu `session_ids` explicit, `analyze` fără
`session_ids` — ramura de auto-descoperire după `target_url`, `analyze` cu
`project_id`, `GET .../latest` succes + 404). Test de regresie:
`tests/test_cocitation_analyzer.py`. pytest (81 passed), smoke, api_diff verde.

### 4.7 `mention_seeding.py` — ✅ validat live, bug de logică reparat (2026-09-02)
Modul legitim și distinct (Prompt 32 — monitorizare Reddit/Quora/G2/Capterra/
Trustpilot/presă pentru mențiuni de brand, via Serper.dev). Nu se fuzionează
cu nimic — nicio suprapunere cu celelalte module de vizibilitate (acelea
urmăresc citări AI; acesta urmărește mențiuni organice pe platforme terțe).
CRUD-ul de configurări (`create_config`/`list_configs`) și worker-ul
(`run_mention_scan`) sunt corecte — modelele se potrivesc exact cu utilizarea
lor în cod, fără drift de schemă.

**Bug găsit (nu crash, logică mereu greșită):** `GET .../latest` calcula
`coverage_score = covered / len(by_platform)`, dar `by_platform` se construiește
*doar* din platformele care au avut deja cel puțin o mențiune persistată — deci
`covered` (numărul de platforme cu `cnt > 0`) e mereu egal cu `len(by_platform)`
prin construcție. Rezultatul: `coverage_score` raporta mereu 100%, indiferent
câte platforme monitorizează de fapt configurația — niciodată procentul real
„din câte platforme urmărite am prezență". Worker-ul își calcula propriul
coverage_score corect (folosind `active_platforms`, derivat din flag-urile
`monitor_*` ale configurației, ca numitor real), dar acel calcul nu ajungea
niciodată la endpoint-ul de citire — era complet duplicat, greșit, separat.

**Reparat:** extras `get_active_platforms(cfg)` din worker într-o funcție
publică refolosită de ambele locuri; endpoint-ul `latest` folosește acum
`len(get_active_platforms(cfg))` ca numitor, la fel ca worker-ul.

Verificat live: config cu toate cele 4 flag-uri `monitor_*` active (→ 6
platforme monitorizate) + 2 mențiuni seed-uite (reddit, g2) → coverage 33.3%
(înainte de fix: 100%, greșit). Config separat cu doar `monitor_reddit=true`
(→ 1 platformă) + 1 mențiune → coverage 100% (corect, caz limită validat
separat). Test de regresie: `tests/test_mention_seeding_coverage.py`.
pytest (85 passed), smoke, api_diff verde.

### 4.8 `entity.py` — ✅ validat live, bug de acuratețe (fals pozitiv) reparat (2026-09-02)
Modul legitim și distinct (Prompt 31 — autoritate de entitate: Wikipedia,
Wikidata, schema markup Organization, Crunchbase/Knowledge Panel/LinkedIn via
Serper). Nu se fuzionează cu nimic. Singurul dintre sateliți care funcționează
parțial fără `SERPER_API_KEY` (Wikipedia/Wikidata/schema markup sunt HTTP-uri
publice fără nevoie de cheie) — verificat live, real, prin apeluri HTTP
reale către Wikipedia/Wikidata/homepage-ul țintei.

**Bug găsit (nu crash, fals pozitiv de acuratețe):** `_check_wikipedia()`
căuta articolul Wikipedia direct după numele brandului
(`/page/summary/{brand}`), fără nicio dezambiguizare. Pentru „Asana" asta
rezolva la articolul despre **postura de yoga** (un articol real, cu acel
titlu exact), nu la „Asana, Inc." (compania) — confirmat live comparând
răspunsurile reale ale API-ului Wikipedia. Efect: +25 puncte fals-pozitive
la `entity_authority_score`, `url`/`description` care indică spre un subiect
complet neînrudit, și — pentru un brand care chiar NU are prezență Wikipedia
dar coincide cu un cuvânt comun — recomandarea „creează un articol Wikipedia"
ar fi fost suprimată greșit. Multe nume de brand-uri SaaS coincid cu cuvinte
comune (Notion, Buffer, Monday, etc.), deci nu e un caz izolat.
`_check_wikidata` nu are această problemă — caută după URL-ul oficial al
site-ului (proprietatea P856), deja precis.

**Reparat** (după discuție explicită despre scop): fiecare candidat de
titlu e verificat prin `extlinks`-urile paginii (API MediaWiki) — dacă
pagina nu conține un link către `target_domain`, e respinsă ca fals-pozitiv.
Pentru engleză, se încearcă și tipare comune de dezambiguizare
(`"{brand} (company)"`, `"{brand}, Inc."`, `"{brand} (software)"`) înainte
de a renunța, ca să găsească articolul corect al companiei, nu doar să
suprime fals-pozitivul.

Verificat live (apeluri HTTP reale, nu mock-uri, către Wikipedia/MediaWiki):
„Asana" → găsește acum corect „Asana, Inc." (confirmat prin extlinks conținând
asana.com), nu mai returnează articolul de yoga. Fără regresie pe branduri
fără ambiguitate: „Salesforce" și „Anthropic" găsite corect pe primul candidat.
Brand inexistent → correct not-found. Testat și end-to-end prin HTTP
(`POST /check`, `POST /check` cu `project_id`, `GET .../latest` succes + 404).
Curățare colaterală: import mort `raise_bad_request` eliminat din
`api/routes/entity.py`. Test de regresie (cu mock-uri httpx, fără dependență
de rețea în CI): `tests/test_entity_checker_wikipedia.py`. pytest (88 passed),
smoke, api_diff verde.

### 4.9 `serpiq` — ✅ validat live, 3 bug-uri reparate (1 sistemic, 2 locale) (2026-09-02)
Modul mare și distinct (~2350 linii — `app/modules/serpiq/` + `api/models/serpiq.py`,
migrarea `0008`): "SERP Snapshot & Instant Analysis" — analiză completă a unei
căutări sau URL: fetch SERP live (DataForSEO), analiză on-page a URL-ului
țintă, verdict (optimize/create/compete/dominate/gap) + brief opțional Claude.
Nu se fuzionează cu nimic — arhitectură proprie, coerentă, deja cu propriile
tabele/migrare, cu un bug de concurență (`_save_snapshot` lazy-load greenlet)
deja reparat într-o sesiune anterioară (vezi git log).

**Bug #1 (sistemic, descoperit aici dar depășește serpiq) — `.lstrip("www.")`:**
`str.lstrip(chars)` elimină un *set* de caractere, nu un prefix literal —
`"wework.com".lstrip("www.")` → `"ework.com"` (corupe orice domeniu care
începe cu litera 'w', nu doar "www." literal: webflow.com, weebly.com,
wetransfer.com sunt companii reale afectate). Găsit identic duplicat în
**10 fișiere, 19 apariții**: `serp_validator.py`, cele 3 fișiere serpiq,
`fanout_tracker_worker.py` (×4), `prompt_discovery.py` (×5),
`fanout_cross_reference.py`, `fanout_competitive.py` (×2), `fanout_analyzer.py`
(cel mai grav — `FanoutSource.domain`, valoarea persistată în DB pentru
*fiecare* sursă din *fiecare* sesiune Fan-Out, folosită de citation tracking,
analiză competitivă, cross-reference, `is_target` matching), `fanout.py` (×2).

**Reparat (aprobat explicit ca reparație sistemică):** `api/utils/domain.py`
→ `strip_www(domain)`, verifică prefixul literal case-insensitiv, păstrează
cazul restului stringului. Înlocuit toate cele 19 apariții din cele 10
fișiere. Verificat live: `FanoutSource(url="https://wework.com/...")` →
`domain == "wework.com"` (înainte: `"ework.com"`); endpoint SerpIQ cu
`input_value="https://webflow.com/"` → keyword și domeniu corecte, poziție
găsită = 1 (înainte, bug-ul ar fi corupt keyword-ul derivat din hostname).
Test de regresie: `tests/test_domain_strip_www.py`.

**Bug #2 (serpiq, descoperit live) — tipuri SERP nefiltrate:** `SERPFetcher.
_parse_item()` folosea o listă neagră incompletă (`"paid", "shopping", "app",
"map"`) pentru a decide ce item-uri DataForSEO să păstreze. Blocurile
`ai_overview` și `people_also_ask` — extrem de comune pe SERP-urile reale din
2026 — treceau nefiltrate, fără `url`/`domain`/`title` proprii, ocupând un
slot din "top 20" cu un rând complet gol și **dislocuind un rezultat organic
real**. Confirmat live pe o căutare reală ("best crm software"): 3 din 20
rânduri persistate erau goale (poziții 1, 3, 12 — ai_overview + 2×
people_also_ask), 3 rezultate organice reale pierdute. Existase deja o
constantă `_ORGANIC_TYPES` definită dar niciodată folosită — exact tiparul
"scris corect, niciodată conectat" găsit deja de mai multe ori în alte
sateliți. **Reparat:** switch de la listă neagră la listă albă
(`_VALID_ITEM_TYPES = _ORGANIC_TYPES | {local_pack, video}`), folosind
constanta deja existentă. Verificat live: aceeași căutare → 19 item-uri
reale, zero rânduri goale. Test de regresie: `tests/test_serpiq_item_filtering.py`.

**Bug #3 (serpiq, consecință directă a #2) — "top 3" după număr de poziție
literal:** `verdict_engine.py` (3 locuri: `avg_word_count_top3`,
`_top3_domains_bullet`, `_schema_rate_top_n`) filtra după `position in (1,2,3)`
/ `position <= n` — dar odată ce blocurile ai_overview/people_also_ask nu mai
ocupă acele poziții ca rânduri goale (bug #2 reparat), pozițiile absolute rar
mai cad exact pe 1/2/3 pe SERP-uri reale. Confirmat live: bullet-ul "Top 3
rezultate" arăta **un singur** domeniu în loc de 3, deși existau clar 3+
competitori organici reali imediat după (poziții 4, 5, 6). **Reparat:**
folosește ordinea din listă (`serp_items[:3]`/`serp_items[:n]`) — lista e deja
ordonată după rank real și acum fără zgomot — în loc de numărul de poziție
literal. Verificat: `position_found`/`find_url_in_serp` NU au nevoie de
aceeași schimbare (folosesc corect poziția absolută reală, semnificativă
pentru "unde apari tu exact"). Test de regresie: `tests/test_serpiq_verdict_top3.py`.

pytest (100 passed), smoke, api_diff verde. Toate verificate cu apeluri reale
către DataForSEO live (nu mock-uri) — credențiale `DATAFORSEO_LOGIN`/
`DATAFORSEO_PASSWORD` sunt configurate în acest deployment.

Cu acest satelit, toate cele 6 module identificate ca neexaminate în Etapa
4.2 (`answer_calibration`, `gsc_fanout`, `cocitation`, `mention_seeding`,
`entity`, `serpiq`) sunt acum validate live. Etapa 4 poate fi considerată
închisă, rămâne doar 4.3 (reevaluare fuziune cu `visibility/` — niciun
candidat găsit, niciunul dintre cele 6 s-a dovedit duplicat).

### 4.3 Reunifică cu `visibility/` — de re-evaluat
Cu `projects.py` scos din ecuație, scopul acestei etape se restrânge la modulele
listate mai sus, dacă vreunul se dovedește, după citire completă, a fi într-adevăr
duplicat conceptual cu `visibility/` (nu doar tematic apropiat).

---

## Etapa 5 — Restul domeniilor

Același tipar ca Etapa 3, în ordinea crescătoare a riscului.

### 5.1 `actions/` — cinci module, același input și output
`action_cards` (6), `content_briefs` (8), `guide` (5), `insights` (5), `summary` (2).
Toate iau rezultate de audit și produc recomandări; diferă doar formatul de ieșire.
Țintă: un pipeline `findings → recomandări → artefacte`, cu formatul ca parametru.
Adaugă aici și generatoarele: `meta_generator`, `schema_gen`, `llms_txt`.
*Date reale de păstrat: 40 action_cards, 27 url_guides, 25 content_briefs, 74 schema_markups.*

#### 5.1a Premisă corectată după citire completă (2026-09-02)
Ca și la `projects.py` (Etapa 4.2): premisa „5+3 module, același input/output"
nu rezistă la citirea corpului efectiv al fiecărui fișier (survey complet,
8 fișiere, prin agent de research + verificare independentă a fiecărei
afirmații-cheie). Doar **`action_cards`+`content_briefs`** sunt cu adevărat
duplicate (aceeași formă: findings per-pagină → rând de recomandare
structurată → export-uri). Restul sunt conceptual distincte:
- `guide.py` — agregă multi-sursă **per URL** (nu per `result_id`), include
  date GSC live; mai aproape de `insights.py` decât de perechea de mai sus.
- `insights.py` — clasificator batch cross-sursă (GSC+GA4+Ads+audit), nu
  „findings → recomandare"; nu atinge deloc `ContentBrief`/`ActionCard`/
  `SchemaMarkup`. Candidat mai bun pentru gruparea din 5.2.
- `summary.py` — cardinalitate greșită pentru fuziune (un rând per audit
  întreg, nu per pagină) — și e sursa de infrastructură comună (vezi mai jos).
- `meta_generator.py` — zero persistență, zero legătură cu vreun audit;
  operează pe orice URL scrapuit live, nu pe „findings".
- `schema_gen.py` — artefact categoric diferit (JSON-LD validat determinist,
  nu o „recomandare"), și el însuși hub de infrastructură comună.
- `llms_txt.py` — artefact site-level pentru un consumator diferit (crawlere
  AI, nu om) — markdown, nu JSON structurat.

Scop revizuit: doar fuziunea `action_cards`+`content_briefs`.

#### 5.1b Bug reparat înainte de fuziune: crash CSV export (2026-09-02)
`export_csv()` din `action_cards.py` făcea `action.get("current", "")[:200]`
— `dict.get(key, default)` înlocuiește default-ul doar când **cheia** lipsește,
nu când valoarea e `None`, iar promptul LLM din același fișier instruiește
explicit modelul să întoarcă `"current": null` când nu există text curent
(și o altă ramură din același fișier construiește direct `"current": None`).
Export CSV crash garantat pentru orice card cu acest caz normal, așteptat.
Reparat (`current`/`recommended`/`reason`), verificat live cu un card real
având `"current": null` prin toate cele 4 formate de export. Test:
`tests/test_action_cards_csv_export.py`. Commit `6898122`.

#### 5.1c Extragere infrastructură comună înainte de fuziune (2026-09-02)
`call_llm_for_summary`/`clean_json_response` erau definite în `summary.py`
dar importate de **7 alte fișiere** (`action_cards`, `content_briefs`,
`benchmarks`, `content_gaps`, `draft_optimizer`, `gap_analysis`, `schedules`)
— infrastructură comună găzduită accidental într-un singur router, nu într-un
loc real partajat. Mutat verbatim în `api/utils/llm_json_client.py`;
`summary.py` re-exportă ambele nume (același tipar ca re-exportul
`app/modules/serpiq/models.py`/`api/models/clusteriq.py`), deci niciunul din
cele 7 fișiere nu și-a schimbat importul. Verificat: toate cele 8 fișiere se
importă fără eroare, apel Anthropic real prin funcția mutată confirmă
comportament identic. Commit `cce345e`.

#### 5.1d ⚠️ Cel mai mare bug găsit în tot acest proiect de consolidare (2026-09-02)
În timpul citirii complete a `content_briefs.py` (pas necesar înainte de
fuziune, exact disciplina învățată de la `projects.py`), a ieșit la iveală
că **`call_llm_for_summary` întoarce un tuple `(text, input_tokens,
output_tokens)`** — așa a fost din **primul commit al proiectului**, nu o
regresie — dar **6 din cele 8 fișiere care îl apelează tratează valoarea
întoarsă ca și cum ar fi direct textul**, apoi crapă când `clean_json_response()`
sau `.strip()` sunt apelate pe tuple:

| Fișier | Bug exact | Verificat live |
|---|---|---|
| `action_cards.py` | `response.strip()` pe tuple | ✅ apel Anthropic real reușit (200 OK), apoi crash — recomandările reale erau **mereu aruncate**, fallback la acțiuni generice |
| `content_briefs.py` (×2: brief + FAQ) | `clean_json_response(tuple)` | ✅ 500 live, eroare capturată direct în rândul din DB: `'tuple' object has no attribute 'strip'` |
| `benchmarks.py` | `clean_json_response(tuple)` | ✅ confirmat live (după fix, alt eșec — model retras din date istorice, nu bug de cod) |
| `content_gaps.py` (×2) | **mai grav** — `content=` în loc de `user_content=`, kwarg care nu există deloc → `TypeError` **înainte** să ajungă la LLM | ✅ confirmat live |
| `gap_analysis.py` | `clean_json_response(tuple)` | ✅ confirmat live |

Doar `draft_optimizer.py` și `generate_summary_task` din `summary.py` însuși
au despachetat corect tuple-ul dintotdeauna.

**Dovada din DB (cronologie):** toate cele 40 de rânduri `action_cards` (27
feb – 2 mar 2026) și toate cele 25 `content_briefs` (26 feb – 9 mar 2026) —
nimic de atunci (azi: 2 sep 2026). ~6 luni de tăcere completă. Aceste
funcționalități nu au funcționat NICIODATĂ cu adevărat — pentru
`action_cards`, degradau silențios la acțiuni generice (crash-ul era prins
și mascat de un fallback); pentru `content_briefs`, generarea eșua complet
(rânduri `status="failed"`, 4 din 25 rânduri istorice confirmă exact acest
tipar, restul fiind probabil dintr-o versiune anterioară a codului).

**Bug suplimentar găsit în `content_gaps.py` în timpul verificării live:**
toate endpoint-urile cu `gap_id` (`GET`/`PATCH`/`POST .../generate-full-brief`/
`DELETE`) tipau parametrul ca `int`, dar `ContentGap.id` e un UUID string
(`String(36)`) — FastAPI respingea orice `gap_id` real cu 422 înainte ca
handler-ul să ruleze vreodată. Reparat (`gap_id: int` → `gap_id: str`, 5
locuri), verificat live (422 → 200 pe același UUID real).

**Reparat și verificat live, cu apeluri Anthropic reale (nu mock-uri), pentru
toate cele 5 fișiere / 7 puncte de apel** — fiecare a produs conținut real,
substanțial, pentru prima dată vreodată:
- `action_cards.py`: 5 acțiuni reale, în română, specifice paginii testate.
- `content_briefs.py`: brief complet (executive summary, content_changes,
  seo/geo requirements) — prima generare reușită din istoria funcției.
- `benchmarks.py`: analiză competitivă completă (competitive_summary,
  strengths/weaknesses/opportunities).
- `content_gaps.py`: brief complet de conținut pentru un gap real.
- `gap_analysis.py`: analiză gap completă (overall_gap_score, gaps detaliate).

Teste de regresie: `tests/test_call_llm_for_summary_unpacking.py` (verifică
static, prin AST, că niciun apel nu mai atribuie tuple-ul unei singure
variabile), `tests/test_content_gaps_fixes.py` (fixează kwarg-ul corect +
tipul `str` pentru `gap_id`). pytest (110 passed), smoke, api_diff verde.

**Notă de context:** severitatea și domeniul acestui bug depășesc cu mult
scope-ul „fuzionează action_cards+content_briefs" — a fost prezentat explicit
utilizatorului înainte de reparare, dat fiind că afectează și module din
Etapa 5.2 (`benchmarks`, `content_gaps`, `gap_analysis`) neatinse încă de plan.

Fuziunea propriu-zisă `action_cards`+`content_briefs` rămâne următorul pas.

#### 5.1e Fuziunea propriu-zisă: doar export-ul unificat (2026-09-02)
Cu bug-urile reale reparate (5.1b–5.1d), a rămas întrebarea centrală: ce
înseamnă de fapt „fuzionează action_cards+content_briefs"? Analiza celor
două fișiere complete a arătat că **nu sunt de fapt duplicate la nivel de
generare**:
- Prompt-uri diferite: `action_cards.py` are prompt Python hardcodat;
  `content_briefs.py` încarcă din `prompts/content_brief.yaml` — teritoriu
  „nu se modifică fără plan explicit" din CLAUDE.md.
- Output diferit: action_cards = listă de todo-uri cu text exact de copiat;
  content_briefs = brief complet de strategie de conținut, cu propriul
  sub-flux de generare FAQ (`POST /{id}/faq`) pe care action_cards nu-l are.
- Algoritm de selecție a paginilor diferit: `select_pages_for_briefs`
  (content_briefs) dedup după `page_url` cu umplere pe două benzi de scor;
  selecția din `action_cards` e mai simplă, cheie de "deja procesat" pe
  `result_id`, nu `page_url`. Unificarea ar schimba comportament real pentru
  utilizatorii existenți ai uneia din cele două funcții.

Forțarea unui singur „pipeline cu formatul ca parametru" aici ar fi repetat
exact capcana deja identificată de două ori în această etapă (`projects.py`,
gruparea inițială 5+3). **Decizie (aprobată explicit):** singurul lucru
cu adevărat duplicat și sigur de unificat era stratul de **export**
(CSV/HTML/Trello) — `action_cards.py` avea toate 4 formatele,
`content_briefs.py` avea doar JSON.

Extras `api/utils/recommendation_export.py`: `ExportItem`/`ExportPage` +
`build_csv_response`/`build_html_response`/`build_trello_export`. Ambele
seturi de „item-uri de recomandare" au aceeași formă conceptuală (titlu,
current, recommended, reason, tag de severitate) sub nume de câmpuri
diferite — `action_cards`: `category/action/current/recommended/reason/
difficulty`; `content_briefs.content_changes`: `type/section/current/
recommended/rationale/impact` — mapate 1:1 pe `ExportItem`. Codarea pe
culori a badge-ului de severitate (verde/portocaliu/roșu) a fost făcută
agnostică de vocabular (funcționează atât pentru easy/medium/hard cât și
pentru critical/high/medium), ca să nu se piardă din vizual la generalizare.

Fidelitate verificată explicit față de codul original din `action_cards.py`
(nu doar „merge, testează la final"): coloana „Category" (pierdută într-o
primă variantă, adăugată înapoi), badge-ul de progres „X/Y acțiuni"
(la fel, pierdut apoi recuperat), fallback-ul `page_title or page_url`
pentru numele cardului Trello. `content_briefs.py` a primit acum, pentru
prima dată, export CSV/HTML/Trello (înainte doar JSON) — `format=json`
rămâne default-ul, deci niciun apelant existent nu-și schimbă comportamentul.

Verificat live cu date reale istorice (același audit `53a6d6f0...` care are
și action_cards și content_briefs): toate 4 formate, ambele fișiere, conțin
date reale corecte (Category/Tag/Current/Recommended/Reason intacte în CSV,
grupare pe priority în Trello, culori corecte în HTML). Test de regresie:
`tests/test_recommendation_export.py`. pytest (120 passed), smoke, api_diff
verde.

Cu aceasta, Etapa 5.1 se consideră închisă.

### 5.2 `compare/` — șase module pentru comparație și gap
`content_gaps` (9), `compare` (6), `gap_analysis` (5), `cross_reference` (5),
`benchmarks` (5), `multilingual` (2). `compare` și `benchmarks` fac amândouă comparație
între audituri. Trei module diferite detectează „gap". Adaugă `tracking` (evoluție în timp).

#### 5.2a Survey complet + 3 bug-uri reparate (2026-09-03)
Citire completă a celor 7 fișiere (`content_gaps`, `compare`, `gap_analysis`,
`cross_reference`, `benchmarks`, `multilingual`, `tracking` — prin agent de
research + verificare independentă a fiecărei afirmații-cheie, exact
disciplina din 5.1).

**Premisa „compare + benchmarks fac comparație, 3 module detectează gap"
se confirmă doar parțial:**
- `compare.py`/`benchmarks.py` chiar au suprapunere algoritmică reală
  (agregare scor din N audituri), dar `compare.py` e de fapt **trei lucruri
  diferite sub un singur router**: 4 endpoint-uri de dashboard-chart (SQL
  simplu, fără LLM), comparația ad-hoc efemeră `/compare` (2-4 audituri,
  nimic persistat), și `POST /rerun/{result_id}` (re-rulează o singură
  pagină prin `DirectAnalyzer` — nimic de-a face cu „comparație").
- „3 module detectează gap" e adevărat doar la nivel de cuvânt:
  `content_gaps` (topicuri lipsă din GEO/citation monitoring → conținut nou),
  `gap_analysis` (scor pe criteriu vs. competitor cunoscut → fix pagini
  existente), `multilingual` (pagină lipsă per limbă) — zero cod comun,
  aproape zero model DB comun.
- **Pereche reală, mai puternică decât zice planul:** `gap_analysis.py` ↔
  `benchmarks.py` — aceeași formă (1 țintă + N competitori), același ciclu
  de viață (pending→generating→completed/failed, proiect persistat), și
  **deja cuplate structural în schemă** (`CompetitorGapAnalysis.benchmark_id`
  → FK către `benchmark_projects.id`, CASCADE) — dar FK-ul e decorativ azi:
  crearea unei `CompetitorGapAnalysis` cu `benchmark_id` NU derivă
  `target_audit_id`/`competitor_audit_ids` din benchmark-ul legat; apelantul
  trebuie să le retrimită oricum.
- `cross_reference.py` — înfășoară un engine legacy separat
  (`core/cross_reference_analyzer.py`, 1943 linii) care citește din
  directoare de fișiere plate, nu din `AuditResult` — unitate de analiză
  complet diferită (toate paginile unui SINGUR audit, nu compară două).
  Nu se fuzionează.
- Logica de „diff două seturi de scoruri" apare de fapt de **trei ori**
  independent: `compare.py` (`/compare`), `tracking.py`
  (`/{project_id}/compare`), și **duplicat chiar în interiorul
  `benchmarks.py`** (aceeași logică weighted-avg/rank la liniile ~241-258
  și ~552-568) — candidat real pentru un helper comun, separat de decizia
  de fuziune a modulelor.

**3 bug-uri confirmate live și reparate, independent de orice decizie de
fuziune:**
- `content_gaps.py:110` (`deduplicate_gaps`) — `existing["sources"].extend(
  gap["sources"])` crapă cu `KeyError` de fiecare dată când două gap-uri
  din surse diferite sunt destul de similare pentru fuziune — exact
  scenariul pentru care există funcția („boost confidence dacă mai multe
  surse sunt de acord"). Fiecare generator de gap-uri setează doar cheia
  singulară `"source"`; `"sources"` (plural) se creează doar la prima
  apariție a unui topic, niciodată pe duplicatul care tocmai se fuzionează.
  Prins de un `except Exception` gol din task-ul de fundal — eșec complet
  silențios. Verificat live: crash confirmat înainte, fuziune corectă
  (cu `confidence` crescut) după reparare. Test: `tests/test_content_gaps_dedup.py`.
- `tracking.py` (3 locuri) — verificări de tip `if score and baseline_score`
  în loc de `is not None`: un scor de exact `0.0` (valoare validă pentru o
  pagină foarte slabă) e „falsy" în Python, deci delta rezultată era
  silențios `None` în loc de valoarea reală. Verificat live: proiect real
  cu snapshot la scor 0.0 → `overall_delta: null` înainte, `50.0` după.
- `compare.py` — două liste divergente de „chei root audit" (14 vs. 20,
  unele cu denumiri diferite: `internal_linking_audit` vs. `internal_linking`)
  în `_extract_criteria_averages()` și `rerun_single_page()` — același
  `audit_type` putea extrage scorul cu succes într-un endpoint și eșua
  silențios în celălalt. Unificate într-o singură constantă comună
  `AUDIT_ROOT_KEYS` (reuniune, aditiv — nimic pierdut din ce recunoștea
  fiecare înainte).

pytest (124 passed), smoke, api_diff verde. Commit-uri, merge pe master.

#### 5.2b `gap_analysis.py` + `benchmarks.py`: FK reparat + helper comun (2026-09-03)
Aprobat de utilizator: fuzionează perechea reală (nu literal într-un singur
fișier — cele două servesc scopuri diferite, narativ larg vs. listă de
fix-uri per criteriu — ci cuplarea lor structurală).

**FK decorativ reparat:** `GenerateGapAnalysisRequest.target_audit_id`/
`competitor_audit_ids` erau `Optional`-izate; când `benchmark_id` e dat și
ele lipsesc, `generate_gap_analysis()` încarcă acum `BenchmarkProject`-ul
legat și le derivă din `target_audit_id`/`competitor_audit_ids` proprii —
apelantul nu mai trebuie să le retrimită redundant. Dacă ambele sunt date
explicit, valorile explicite câștigă (niciun apelant existent nu-și schimbă
comportamentul). Verificat live: creat un benchmark real (2 audituri
GEO_AUDIT reale), apoi o gap analysis cu **doar** `benchmark_id` (fără
`target_audit_id`/`competitor_audit_ids`) — confirmat că ambele au fost
derivate corect, identic cu benchmark-ul legat, înainte ca task-ul de
fundal să pornească apelul LLM.

**Helper comun extras:** `_compute_comparison_stats()` în `benchmarks.py` —
media ponderată pe număr de pagini, scorul celui mai bun competitor, rank-ul
țintei — înlocuiește cele 2 copii identice din `_build_benchmark_data_payload`
și `get_benchmark_detail`.

**Găsit în timpul verificării live, NEATINS acum (semnalat separat via
spawn_task, `task_b3d32e2e`):** `generate_gap_analysis_task` a eșuat pe
perechea de audituri mari (531 + 1045 pagini) cu o eroare de parsare JSON —
`max_tokens=4096` e insuficient pentru un răspuns LLM cu o matrice de gap
completă pe atâtea date (comparativ, `content_briefs.py` folosește 8192
pentru un brief de-o singură pagină, deci 4096 aici pare sub-dimensionat).
Independent de fix-ul FK (care a funcționat corect înainte ca acest eșec să
apară în etapa LLM) — nu s-a atins acum, e o problemă de tuning/design
separată.

Teste de regresie: `tests/test_gap_analysis_benchmark_fk.py`,
`tests/test_benchmarks_comparison_stats.py`. pytest (132 passed), smoke,
api_diff verde.

#### 5.2c `compare.py` despărțit în 3 fișiere (2026-09-03)
`compare.py` (631 linii) chiar era trei feature-uri fără nicio legătură,
sub un singur router: 4 endpoint-uri de dashboard-chart (SQL simplu, fără
LLM), comparația efemeră reală `/api/compare`, și re-rularea unei singure
pagini `/api/audits/{id}/rerun/{result_id}`. Despărțit păstrând EXACT
aceleași căi URL (nicio schimbare de comportament — pură reorganizare):
- `api/routes/dashboard_charts.py` — cele 4 endpoint-uri de chart.
- `api/routes/compare.py` — rămâne doar `_extract_criteria_averages` +
  `/api/compare` (scopul original, real, al numelui fișierului).
- `api/routes/audit_rerun.py` — endpoint-ul de re-rulare.
- `api/utils/audit_json.py` — noua locație comună pentru constanta
  `AUDIT_ROOT_KEYS` (era deja partajată între `compare.py` și
  `rerun_single_page`, acum ambele o importă din același loc).

Verificat live: toate cele 4 chart-uri răspund 200, `/api/compare` cu 2
audituri reale găsește 522 pagini comune și extrage corect criteriile,
`api_diff` confirmă suprafața API identică (367 operații, neschimbată).

**Găsit în timpul verificării, NEATINS acum (semnalat separat via
spawn_task, `task_06f52aa7`):** endpoint-ul de re-rulare
(`/api/audits/{id}/rerun/{result_id}`) nu a funcționat niciodată —
`DirectAnalyzer.__init__()` cere `input_dir`/`output_dir` (nepasate
deloc, deși endpoint-ul le calculează local cu puțin mai sus), și mai
grav, `DirectAnalyzer` nu mai are deloc o metodă `analyze_single_page` —
interfața reală actuală e `_process_single_page` (privată), cu semnătură
și comportament complet diferite (citește fișierul singură, face propriul
chunking/rate-limiting, întoarce un `PageResult`, nu text brut). Bug
confirmat live, pre-existent (identic înainte și după despărțire — cod
mutat verbatim, nicio logică schimbată) — necesită o rescriere reală
față de API-ul actual al `DirectAnalyzer`, nu un patch rapid, și
`core/` nu se modifică fără plan explicit conform CLAUDE.md.

pytest (132 passed), smoke, api_diff verde.

**Rămas neatins, decizie viitoare:**
1. Extrage un helper comun de „diff scoruri" folosit de `compare.py`,
   `tracking.py` (algoritm similar dar nu identic cu cel din `benchmarks.py`
   — nu s-a forțat unificarea acestora acum).
2. `content_gaps.py`, `cross_reference.py`, `multilingual.py` rămân module
   separate — domenii distincte, fără suprapunere reală de cod sau date.

Cu aceasta, Etapa 5.2 se consideră închisă.

### 5.3 `sources/` — trei implementări identice de upload CSV
`gsc` (18), `ga4` (7), `ads` (7) — toate: upload CSV → parse → tabel → cross-reference.
Extrage un pipeline comun de ingestie, parametrizat pe schema de coloane.
*Date reale: 25.000 gsc_query_rows + 11.832 gsc_page_rows — cea mai valoroasă bază de
date din proiect. Tratează migrarea ei cu grijă maximă.*
Adaugă `bot_access` (fetch robots.txt) — devine relevant în Etapa 6.

#### 5.3a Survey complet + premisă corectată (2026-09-03)
Citire completă a celor 8 fișiere (`gsc/__init__.py`, `gsc/_shared.py`,
`gsc/oauth_sync.py`, `gsc/properties.py`, `gsc/optimizer.py`, `ga4.py`,
`ads.py`, `bot_access.py`) prin agent de research + verificare
independentă a fiecărei afirmații-cheie (inclusiv un query read-only
direct pe DB pentru a confirma proveniența datelor reale, dat fiind
avertismentul explicit din plan despre valoarea lor).

**Premisa „3 pipeline-uri CSV identice" nu se confirmă — GSC nu are deloc
unul funcțional:**
- **`_parse_gsc_csv` era apelată în `gsc/properties.py:135` dar nu era
  definită NICĂIERI în tot repo-ul** — confirmat prin grep pe întregul
  proiect (o singură apariție, punctul de apel). `git log -p` arată exact
  momentul: commit-ul `e14df80` (2026-04-05, împărțirea `gsc.py` monolitic
  de 1541 linii în subpachetul actual `gsc/`) a șters definiția funcției
  din fișierul vechi, dar punctul de apel a supraviețuit în noul
  `properties.py`. **100% din apeluri la `POST /api/gsc/properties/{id}/
  upload` crapau cu `NameError`, dintotdeauna.**
- **Verificat live, cu grijă maximă dat fiind avertismentul din plan:**
  query read-only direct pe `api/data/analyzer.db` confirmă că singurul
  `GscProperty` real (`site_url='sc-domain:conso.ro'`, `sync_type='api'`,
  `total_queries=25000`, `total_pages=11832`) — și toate cele 25.000/11.832
  rânduri din `gsc_query_rows`/`gsc_page_rows` — au ajuns **exclusiv prin
  sincronizarea live OAuth din `gsc/oauth_sync.py`**, niciodată prin CSV.
  **Zero risc de date** — funcționalitatea CSV nu a scris niciodată nimic,
  deci bug-ul nu a putut cauza pierdere sau corupere de date reale.
- `gsc/_shared.py` importă `csv, io, uuid, sqlite3, UploadFile, File, Form,
  ...` — nefolosite deloc în cele 121 de linii reale ale fișierului (doar
  helper-e OAuth) — semn clar că logica CSV era menită să locuiască acolo
  dar nu a supraviețuit refactorizării.
- GA4 (`ga4.py`) și Ads (`ads.py`) sunt de fapt auto-suficiente, fiecare cu
  propriul `_parse_ga4_csv`/`_parse_ads_csv` complet funcțional — aceeași
  formă generală (decode BOM → `csv.DictReader` → detectare tip raport din
  primul header → cast per-câmp → delete-apoi-insert), dar regulile de
  parsare diferă semnificativ și nu sunt interschimbabile: GA4 parsează
  durate `mm:ss`, Ads are CTR mereu cu `%` și costuri cu simboluri
  valutare, fiecare detectează o pereche diferită de tipuri de raport.
  Ambele tabele sunt goale azi (0 rânduri) — zero risc de date oricum.

**Reparat: `_parse_gsc_csv` scrisă de la zero** (aprobat explicit ca prim
pas, izolat de GA4/Ads), potrivită formatului real de export GSC
(„Top queries"/„Top pages", CTR ca procent `"12.34%"`), urmând stilul deja
stabilit în `_parse_ads_csv`. CTR normalizat la scara 0.0–1.0 deja folosită
de calea OAuth funcțională (API-ul Google întoarce CTR ca fracție brută —
CSV-ul trebuia să fie pe aceeași scară, altfel cele două căi de ingestie
ar fi fost silențios inconsistente pe unități).

Verificat live: property de test creat, upload real CSV queries (3 rânduri)
→ 200, rânduri persistate corect (CTR convertit corect: `2.4%` → `0.024`),
upload CSV pages (2 rânduri) → 200, header nerecunoscut → 422 corect.
Confirmat explicit că property-ul real (25.000/11.832 rânduri) a rămas
neatins pe tot parcursul testării. Test de regresie:
`tests/test_gsc_csv_parser.py`. pytest (139 passed), smoke, api_diff verde.

**Rămas neatins, decizie viitoare (fără risc de date, cosmetic):**
1. Scaffolding comun GA4+Ads (decode BOM, detectare tip raport, pattern
   delete-apoi-insert) — casterele specifice (`_dur`/`_ctr`/`_cost`) rămân
   neschimbate per sursă. Ambele tabele goale azi — zero urgență.
2. Helper comun pentru cele 3 endpoint-uri `cross_reference` (construiește
   lookup normalizat din sursa B, grupează rândurile sursei A, sortează,
   limitează la N) — regulile de grupare rămân specifice per pereche.
3. `bot_access.py` rămâne neatins — devine relevant în Etapa 6, nu participă
   deloc la pattern-ul de upload CSV.

### 5.4 `queries/`
`keyword_research` (5), `clusteriq` (16), `query_suggestions` (1) + partea de query din `gsc`.

#### 5.4a Survey complet: fără duplicare reală, un bug real reparat (2026-09-03)
Citire completă a celor 9 fișiere (`keyword_research.py`, `query_suggestions.py`,
`clusteriq/router.py` + cele 5 fișiere din `services/` + `tasks/pipeline.py`)
prin agent de research + verificare independentă. Spre deosebire de
etapele anterioare, planul aici nu afirmă explicit „sunt duplicate" — doar
le grupează sub „queries/". Confirmat: **nu sunt duplicate**, sunt trei
unelte distincte care doar împart cuvântul „query":
- `keyword_research.py` — descoperire de cuvinte cheie (expansiune
  DataForSEO sau import CSV, etichetare intent/question prin LLM).
- `query_suggestions.py` — generare de întrebări-probă pentru fan-out/
  GeoMonitor (ce ar întreba cineva un AI), stateless, fără storage propriu.
- `clusteriq/*` — clustering de URL-uri proprii ale unui site pe baza
  co-apariției în SERP, motor de decizie KUCD (Fix/Consolidate/Optimize/
  Keep/Prune/Create). „Query"-urile sunt doar semnal per-keyword care
  alimentează clustering-ul de URL-uri, nu obiectul analizei.

Nicio fuziune recomandată — ar amesteca trei modele mentale distincte
fără beneficiu real.

**Bug confirmat live și reparat:** `clusteriq/router.py` (liniile ~629 și
~686, `export_decisions_csv`/`export_prune_list_csv`) referenția
`CluCluster.avg_position` direct într-un `select(...)` — coloană care nu
există pe model (verificat în `api/models/clusteriq.py`: câmpurile reale
sunt `total_impressions`, `total_clicks`, `url_count`,
`search_demand_confirmed`, `louvain_community_id`). `avg_position` există
doar ca agregare pe-URL, calculată on-the-fly prin join cu `CluSerpData`
(exact ce face deja `get_cluster_detail`, la granularitate per-URL, nu
per-cluster) — accesarea ei ca atribut de clasă arunca `AttributeError`
înainte ca query-ul să apuce să ruleze. **Ambele endpoint-uri de export
CSV crapau la fiecare apel, de la scrierea codului.** Verificat direct:
`CluCluster.avg_position` → `AttributeError` confirmat în Python înainte
de orice altceva.

**Reparat:** helper comun `_avg_position_by_cluster()`, agregă
`CluSerpData.position` pe toate URL-urile unui cluster (join
`CluUrlCluster` → `CluUrl` → `CluSerpData`), reutilizând exact pattern-ul
deja folosit de `get_cluster_detail`, doar cu un nivel de agregare în plus
(per cluster, nu per URL). Verificat live: proiect de test construit direct
prin modelele SQLAlchemy (2 URL-uri, 1 cluster, poziții SERP 3.5 și 8.2),
ambele endpoint-uri de export → 200, `avg_position` calculat corect (5.8 —
media celor două). Date de test curățate după verificare. Test de
regresie: `tests/test_clusteriq_export_avg_position.py`. pytest
(143 passed), smoke, api_diff verde.

**Găsit, nu urgent, semnalat separat (spawn_task, `task_3b8a03e0`):**
`data_ingestion.py`'s `IngestionOrchestrator` e complet implementată dar
niciodată instanțiată — `tasks/pipeline.py` (singurul orchestrator conectat
efectiv la `POST /projects`) reimplementează aceeași secvență inline,
inclusiv 2 copii aproape identice ale `_top_keywords_without_serp`. Cod
mort/duplicat, fără impact la utilizatori azi — candidat curat pentru
consolidare (face `pipeline.py` să delege către orchestrator), nu urgent.

**Rezolvat (2026-09-03) — invers față de premisă.** Citire completă a
ambelor fișiere arată că `IngestionOrchestrator` NU e o abstracție
superioară de adoptat — e o schiță mai veche, mai îngustă, depășită de
`pipeline.py`. `run_full_ingestion()` face doar GSC+SERP, se oprește acolo
(fără sincronizare `CluUrl`, fără clustering, fără decizii KUCD), și
folosește un vocabular de status incompatibil (`crawling`/`clustering`)
pentru care `PHASE_LABEL`/`PHASE_PROGRESS` nu au nicio intrare — dacă ar fi
fost vreodată conectat, UI-ul ar afișa o fază necunoscută/goală. „Face
pipeline.py să delege" ar fi însemnat fie pierderea pașilor lipsă
(regresie), fie rescrierea completă a orchestrator-ului ca să reproducă
exact `pipeline.py` — fără beneficiu real dincolo de deduplicarea unui
singur helper de ~15 linii. Șters clasa întreagă (174 linii); cele 2
servicii de dedesubt (`GSCIngestionService`, `DataForSEOIngestionService`)
neatinse, folosite direct de `pipeline.py` exact ca înainte. pytest (192
passed), smoke, api_diff verzi.

**Găsit, latent, fără impact azi:** `_sync_urls_from_serp_data` (pipeline.py)
pretinde idempotență via `ON CONFLICT DO NOTHING`, dar `CluUrl` nu are
constrângere unică pe `(project_id, url)` — doar un index neunic — deci
clauza e un no-op. Inofensiv azi (pipeline-ul rulează o singură dată per
proiect, la creare, fără endpoint de re-ingest), dar ar duplica rânduri
silențios dacă se adaugă vreodată un buton de „refresh data". Documentat,
nu reparat — nu există cale de declanșare azi.

### 5.5 `audit/` — ultima, cea mai riscantă
`audits` (11) e motorul principal. `content_iq` (17) e **un al doilea motor complet**,
cu engine-uri proprii (`api/workers/contentiq/engines/eeat.py`, `freshness.py`) care
duplică `prompts/content_quality.yaml` și `prompts/content_freshness.yaml`.
`draft_optimizer` (5) e al treilea path.

Țintă: **un motor, patru moduri** — `site` / `page` / `draft` / `god-mode` — și N prompturi.
Se face ultima pentru că e inima produsului (84 de audituri, 6.352 de rezultate) și pentru
că etapele anterioare îți dau uneltele (client LLM unificat, structured outputs, teste).

**Actualizare (2026-09-03), după citire completă a tuturor celor ~5.000 de linii**
(`audits.py`, `audit_worker.py`, `content_iq.py`, `contentiq/*` inclusiv `engines/`,
`draft_optimizer.py`, plus ambele YAML-uri `content_quality`/`content_freshness`):
premisa „un al doilea motor complet" **nu se confirmă**. `content_iq` nu face nicio
apelare LLM în calea principală de crawl — `engines/eeat.py`/`freshness.py` sunt
formule aritmetice pure peste metadate (backlinks/DR/vechime/trafic), nu judecăți
de conținut; ele nu duplică YAML-urile, răspund la o întrebare complet diferită
(proxy de autoritate off-page vs. citire efectivă a textului paginii). Modelele
(`CiqAudit`/`CiqPage`, id întreg) nu au nicio suprapunere cu `Audit`/`AuditResult`
(id UUID) — zero referințe încrucișate. `draft_optimizer` e la fel de separat:
intrare text colat (nu URL live), un singur apel LLM, tabelă proprie
`DraftOptimization`. **Nu se face fuziunea „un motor, patru moduri"** — cele trei
sunt unelte arhitectural distincte (unitate de analiză, metodă, schemă, model DB
toate diferite), iar riscul unei restructurări forțate pe cea mai sensibilă parte
a produsului (`core/`+`prompts/`, date reale, fără beneficiu funcțional) nu se
justifică. Singura suprapunere reală, minoră: `contentiq/brief.py` instanțiază
`anthropic.AsyncAnthropic()` direct în loc de clientul LLM comun — parte din
lista deja cunoscută de 17 fișiere de la Etapa 2.2, nu dovadă de motor duplicat.

**Bug confirmat și reparat:** `contentiq/crawler.py`'s `crawl_audit()` — semaforul
de concurență proteja doar fetch-ul HTTP (`extract_page_meta`), nu și
select/add/setattr pe `AsyncSession`-ul comun, care nu suportă acces concurent
din mai multe task-uri. Cale live: `POST /api/contentiq/audits/{id}/start`.
Reprodus înainte de fix (`sqlalchemy.exc.ResourceClosedError: This transaction
is closed`, 40 URL-uri simulate, concurrency=10), reparat cu un `asyncio.Lock()`
dedicat în jurul secțiunii DB (fetch-urile rămân paralele). Test de regresie:
`tests/test_contentiq_crawler_concurrency.py`. pytest (144 passed), smoke,
api_diff verzi (367 operații, neschimbate — fișier de worker, nicio rută atinsă).

### 5.6 Curățenie (cod dovedit mort)
Singurele ștergeri propriu-zise, toate verificate cu import-graph:
- toolchain CLI legacy: `main.py` (root) + `core/{determine_score,generate_dashboard,generate_report,validate_audit}.py` — ~6.600 LOC, **zero importuri din `api/` sau `app/`**.
  ⚠️ `core/validate_audit.py` validează scheme YAML de prompturi — **recuperează-l ca test** (util în Etapa 6) înainte de a-l șterge.
- `api/models/schemas_clusteriq.py` — untracked, zero importuri în tot repo-ul
- 4 fișiere `.db` de 0 bytes de la rădăcină (DB-ul real e `api/data/analyzer.db`)
- 8 worktrees stale (`git worktree list` → toate cu 0 commit-uri față de master;
  `heuristic-raman` și `magical-brown` sunt directoare orfane, neînregistrate)
- `.gitignore` pentru `BUILD_LOG.md`, `*_report.txt`, `website_llm_analyzer.log.*`, `uvicorn_*.txt`

**Executat (2026-09-03).** Re-verificat totul înainte de ștergere, nu doar
citat planul:
- Toolchain CLI legacy șters (`main.py` + cele 4 `core/*.py`, ~6.600 LOC,
  zero importuri confirmate din nou via grep). `core/validate_audit.py`
  recuperat ca `tests/test_validate_audit_prompts.py` **înainte** de ștergere
  — logica de validare inlinată direct în test (nu poate importa dintr-un
  fișier șters). Rulat împotriva celor 20 de prompturi reale: 18 validează
  curat, `content_brief.yaml`/`draft_optimizer.yaml` eșuează previzibil la
  verificarea legacy de `output_schema` (schema lor reală vine din
  `core/output_schemas.py`, nu din YAML) — pinned ca excepții cunoscute, nu
  bug.
- `api/models/schemas_clusteriq.py` șters (confirmat: zero importuri).
- Nu 4, ci **6** fișiere `.db` de 0 bytes erau moarte: cele 4 de la rădăcină
  din plan, plus `api/audits.db` și `api/geo_tool.db` (nemenționate în plan,
  găsite separat) — toate zero-byte, `DATABASE_PATH` confirmat hardcodat la
  `api/data/analyzer.db` (45MB, actualizat azi). Toate șterse.
- Worktree-urile reale la verificare: nu 8, ci **7** directoare pe disc (5
  înregistrate + 2 orfane goale `heuristic-raman`/`magical-brown`, exact ca-n
  plan). **Găsit ceva ce planul nu menționa:** 3 din cele 5 worktree-uri
  înregistrate aveau modificări necommise reale, nu doar "0 commit-uri față
  de master" cum spunea planul — `frosty-euler-8c7531` conținea exact fix-ul
  deja verificat din Etapa 3 (`await track_cost()` în loc de
  `asyncio.create_task(track_cost())`, care poate pierde silențios commit-ul
  apelantului pe conexiunea SQLite comună/StaticPool), neaplicat încă în 4
  fișiere: `content_briefs.py` (2 locuri), `fanout.py`, `schema_gen.py` (2
  locuri), `summary.py`. Recuperat și aplicat (reprodus fix-ul lipsă cu
  `git apply --check` unde a mers curat, aplicat manual restul; verificat cu
  pytest 146 passed înainte de merge). Celelalte 2 (`angry-dijkstra`,
  `quizzical-poincare`) conțineau reparații UI cosmetice deja suprapuse de
  schimbări ulterioare de pe master (CSS `[x-cloak]` deja prezent;
  `fanout.html` deja rescris cu `modelOptionsHtml()`, o abordare mai bună
  decât cea din worktree) — confirmate depășite, nu doar ipotetic, înainte
  de a fi abandonate. Toate cele 7 directoare șterse, cu o excepție:
  `frosty-euler-8c7531` a rămas blocat de un lock Windows la nivel de OS
  ("device or resource busy") — conținutul lui era deja recuperat și
  commis, deci fără risc de pierdere, dar directorul necesită ștergere
  manuală după ce orice proces îl ține deschis e închis.
- `.gitignore` extins cu cele 4 pattern-uri din plan; `uvicorn_err.txt`/
  `uvicorn_out.txt` erau de fapt **tracked** (nu doar ignorate-dar-prezente)
  — untracked explicit cu `git rm --cached` (fișierele locale păstrate).

pytest (146 passed), smoke, api_diff verzi (367 operații, neschimbate).

---

## Etapa 6 — Fapte înainte de opinii (upgrade-ul de calitate)

Cea mai mare pârghie de calitate pentru GEO/AEO, și e consolidare, nu feature nou.

`prompts/technical_seo.yaml` e bine scris — e onest că detectează *semnale* din markup
(„Estimated length", „detectable?", „visible") — dar cere LLM-ului să **ghicească** lucruri
pe care tool-ul le poate **verifica**:

| Ce întreabă promptul | Ce ai deja | Cum se verifică |
|---|---|---|
| „Are known AI crawlers (GPTBot, ClaudeBot, PerplexityBot) blocked?" | `bot_access.py` | un GET pe `/robots.txt` |
| „Is there any reference to a /llms.txt file?" | `llms_txt.py` | un GET pe `/llms.txt` |
| „Any JSON-LD structured data detectable?" | `schema_gen.py`, 74 rânduri | parsare, nu inferență |

Cele trei module rulează în silozuri și **nu alimentează niciodată auditul**.

**Țintă:** pipeline-ul de audit devine `fetch fapte → injectează în prompt → LLM judecă`.
Adaugă un pas de colectare deterministă (robots.txt, llms.txt, headers, schema parsată,
status codes) înainte de apelul LLM, și pune faptele în prompt. LLM-ul evaluează
judecăți, nu ghicește fapte.

**Executat (2026-09-03) — doar partea deterministă, fără harness (decizie explicită a
utilizatorului: harness-ul rămâne amânat, vezi 6.1 mai jos).** Verificat din nou premisele
înainte de scris cod, nu doar citat tabelul de mai sus:
- `bot_access.py`/`llms_txt.py` NU sunt module reutilizabile cum sugera planul —
  `llms_txt.py` e un **generator** de llms.txt (nu verifică dacă unul există deja), iar
  logica reală de robots.txt e în `api/utils/bot_access_auditor.py`. Scris în schimb
  `core/technical_facts.py` de la zero, cu propria parsare minimă de robots.txt — ca să
  nu creeze `core/` → `api/` (core/ are azi zero importuri din api/, păstrat așa).
- Descoperire mai serioasă decât presupunea planul: `schema_gen.py` nu era problema —
  `core/html2llm_converter.py`'s `extract_content()` **decompune complet orice `<script>`**
  (deci și tot JSON-LD) înainte ca LLM-ul să vadă vreodată textul paginii, cu excepția
  conținutului FAQPage, reformatat ca proză. „Schema.org presence: detectable?" era
  întrebat contra unui text căruia i se scosese deja 100% din markup.
- `core/technical_facts.py` (nou): `fetch_domain_facts()` — un singur GET robots.txt +
  un singur GET llms.txt per audit (nu per pagină, același domeniu). `extract_structured_data_types()`
  — parsează `<script type="application/ld+json">` din HTML-ul ORIGINAL (încă pe disc în
  `input_html/`, doar niciodată citit după conversie), gestionează `@graph` și forme-listă.
  `format_facts_block()` randează ambele într-un bloc etichetat.
- `core/direct_analyzer.py`: `DirectAnalyzer` primește `html_dir` opțional; `run()` culege
  faptele de domeniu o singură dată (doar când `question_type == TECHNICAL_SEO`);
  `_process_single_page()` caută HTML-ul original al fiecărei pagini după nume de fișier
  și prepend-uiește blocul de fapte la `page_text` înainte de LLM. Toate celelalte tipuri
  de audit neatinse (gate exact pe `question_type`).
- `api/workers/audit_worker.py`: o linie, trece `html_dir=input_html/` mai departe.
- `prompts/technical_seo.yaml`: un paragraf adăugat în `task:` — LLM-ul tratează blocul
  de fapte ca adevăr verificat când e prezent, revine la judecata proprie doar când un
  fapt e marcat necunoscut/indisponibil. Nimic altceva din prompt atins.
- **Greșeală proprie, transparent semnalată utilizatorului**: la începutul acestei etape,
  am reîmprospătat `prompts_backup/` cu o copie a `prompts/` curent, fără să verific mai
  întâi ce conținea deja — se dovedește a fi o versiune „v2" reală, distinctă, folosită de
  `audit_worker.py` când `Audit.prompt_version == "v2"` (2 din cele 84 audituri reale,
  ambele READABILITY_AUDIT pe ing.ro). Conținutul original v2 e de nerecuperat (niciodată
  în git, `cp` suprascrie direct deci Recycle Bin nu prinde nimic, fără drepturi admin
  pentru shadow copies, directorul nu e în sincronizarea OneDrive). Utilizatorul a
  confirmat că nu există altă copie și a acceptat pierderea.
- **Găsit, nu al meu, semnalat separat (`task_4baec8cd`)**: apelarea `_process_single_page()`
  de două ori într-un proces de test, după ce alt test a folosit deja o sesiune reală
  `AsyncSessionLocal`, poate bloca la nesfârșit — `asyncio.create_task(record_cost_async(...))`
  atinge engine-ul SQLite global pe un event loop nou, posibil legat de conexiunea
  `StaticPool` a loop-ului anterior deja închis. Ocolit în testele proprii (client fals
  întoarce 0 tokeni, deci codul de cost tracking nu se declanșează), nefixat.

Teste: 16 pentru funcțiile pure (`tests/test_technical_facts.py`) + 6 pentru integrarea
reală cu `DirectAnalyzer._process_single_page()` (`tests/test_direct_analyzer_technical_facts.py`).
pytest (168 passed), smoke, api_diff verzi (367 operații, neschimbate).

Abia aici se ating prompturile mai departe — și doar cu un harness de evaluare:

### 6.1 Harness de evaluare pe prompturi
Momentan `prompts/` e înghețat prin convenție („nu modifica fără plan" — CLAUDE.md),
adică exact locul unde stă calitatea produsului e locul de care nu se atinge nimeni.
Construiește: 5-10 pagini de referință + scoruri așteptate + un runner care compară.
Punct de plecare: `core/validate_audit.py` (recuperat din 5.6) valida deja scheme YAML.

Cu harness-ul, prompturile devin editabile în siguranță — inclusiv cele două cu cadru
temporal învechit (`geo_audit.yaml:6` spune „2024-2025", `ux_content.yaml:8,85` spune
„2025"; suntem în 2026).

---

## Etapa 7 — Extinderea propriu-zisă (după consolidare)

Abia acum are sens să adaugi. Cu Fan-Out viu și `visibility/` unificat, două lucruri
devin ieftine și diferențiatoare:

1. **Competitorii în răspunsuri AI.** `gap_analysis` compară azi *audituri* (opinii LLM
   despre pagini). Ce lipsește: **cine e citat în locul tău** pentru interogările tale.
   Datele vor exista deja în `fanout_sources` — trebuie doar construită vederea.
2. **Axă explicită SEO / GEO / AEO.** Cele 20 de prompturi le amestecă azi. Un scorecard
   cu trei scoruri măsurate diferit — SEO (semnale on-page + date GSC reale),
   GEO (citabilitate), AEO (apariție măsurată în răspunsuri reale prin Fan-Out) — ar fi
   principiul de organizare al produsului, nu doar al codului. Infrastructura de ponderi
   există deja (`_COMPOSITE_WEIGHTS`, `api/routes/settings.py`, tabelul `audit_weight_configs`).

**Executat (2026-09-03).** Scop confirmat explicit cu utilizatorul înainte de cod:
date reale de la început (nu doar regrupare LLM), doar în cele 2 view-uri unde
compozitul deja există (nu peste tot). Verificat înainte: "`_COMPOSITE_WEIGHTS`
atinge UI-ul peste tot" — **fals**, e folosit într-un singur fișier, 2 locuri
(`api/routes/pages/audit_views.py`: `page_view.html` + `site_health.html`). Alte
"composite_score" din cod (`FanoutTrackingRun`, `ai_visibility.py`) sunt metrici
separate, nelegate.

- SEO/GEO = cele 18 tipuri de audit clasificate în 2 bucket-uri (`_SEO_AUDIT_TYPES`/
  `_GEO_AUDIT_TYPES` în `_shared.py`); SEO se combină 60/40 cu poziția medie GSC reală
  (`GscProperty`/`GscPageRow`) când există o proprietate care se potrivește. AEO **nu**
  vine din niciun tip de audit — refolosește exact formula `mention_rate*0.4 +
  citation_rate*0.6` deja calculată global de `ai_visibility.py`, dar scopată la
  `CitationTracker`-ele site-ului. Fără date reale → `None` ("not tracked"), niciodată
  ghicit.
- **Bug găsit prin verificare pe date reale, nu fixturi sintetice**: `SINGLE_GEO_AUDIT`/
  `SINGLE_SEO_AUDIT` (din `/api/audits/single`, tool-ul Instant Audit — 48+ rânduri reale
  doar pentru `SINGLE_GEO_AUDIT`) nu se potriveau cu niciun bucket fără normalizarea
  prefixului „SINGLE_" — scoruri reale apăreau ca „not tracked". Reparat.
- **Bug găsit tot prin verificare live, nelegat de scorecard**: `site_health()` arunca
  `jinja2.exceptions.UndefinedError` la orice apel real — `site_health.html` referea
  `total_cost_usd` și `cost_usd` per audit, niciodată calculate de rută. Bug pre-existent,
  a blocat verificarea scorecard-ului până a fost reparat.

19 teste noi, pytest (188 passed), smoke, api_diff verzi (368 operații, neschimbate —
doar argumente noi în template, nicio rută nouă).

---

## Ordinea și dependențele

```
Etapa 0  plasă de siguranță          ← BLOCANTĂ, nimic fără ea
   └─> Etapa 1  fix P0                 (smoke test → 0 × 500)
         ├─> Etapa 2  infra + LLM      (invizibil pentru API)
         │     └─> Etapa 3  visibility/        ← prima fuziune, proof-of-concept
         │           └─> Etapa 4  Fan-Out      (are nevoie de fix-ul din 1.3)
         │                 └─> Etapa 5  restul domeniilor (5.1→5.6)
         │                       └─> Etapa 6  fapte înainte de opinii
         │                             └─> Etapa 7  extindere
         └─> Etapa 5.6 curățenie        (se poate face oricând, risc zero)
```

**Estimare:** Etapele 0-1 într-o sesiune fiecare. Etapa 2 în 2-3 sesiuni (migrarea
clientului LLM e incrementală). Etapele 3-4 câte una. Etapa 5 una per sub-domeniu.

---

## Prompt de pornire pentru fiecare sesiune Sonnet

> Citește `docs/CONSOLIDATION_PLAN.md` — secțiunea „1. Principii" (inclusiv regulile de
> angajament) și secțiunea „Etapa N". Execută DOAR etapa N.
>
> Reguli: URL-urile publice nu se schimbă (regula de aur #2). Exclude
> `.claude/worktrees/` din orice scan. Nu atinge `prompts/` (excepție: Etapa 6).
> Înainte de orice ID de model Anthropic, încarcă skill-ul `claude-api` — nu scrie
> ID-uri din memorie.
>
> Începe cu `git status` curat și un branch nou. Termină cu: smoke test rulat efectiv
> (output-ul în răspuns, nu „ar trebui să meargă"), diff de path-uri OpenAPI gol,
> `pytest` verde, commit. Dacă smoke test-ul e mai roșu decât baseline-ul, **nu comite** —
> raportează ce s-a rupt.
>
> Contextul complet al auditului care a produs acest plan e în secțiunea 0; rapoartele
> detaliate în `docs/audit/`.
