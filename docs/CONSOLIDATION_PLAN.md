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

### 5.2 `compare/` — șase module pentru comparație și gap
`content_gaps` (9), `compare` (6), `gap_analysis` (5), `cross_reference` (5),
`benchmarks` (5), `multilingual` (2). `compare` și `benchmarks` fac amândouă comparație
între audituri. Trei module diferite detectează „gap". Adaugă `tracking` (evoluție în timp).

### 5.3 `sources/` — trei implementări identice de upload CSV
`gsc` (18), `ga4` (7), `ads` (7) — toate: upload CSV → parse → tabel → cross-reference.
Extrage un pipeline comun de ingestie, parametrizat pe schema de coloane.
*Date reale: 25.000 gsc_query_rows + 11.832 gsc_page_rows — cea mai valoroasă bază de
date din proiect. Tratează migrarea ei cu grijă maximă.*
Adaugă `bot_access` (fetch robots.txt) — devine relevant în Etapa 6.

### 5.4 `queries/`
`keyword_research` (5), `clusteriq` (16), `query_suggestions` (1) + partea de query din `gsc`.

### 5.5 `audit/` — ultima, cea mai riscantă
`audits` (11) e motorul principal. `content_iq` (17) e **un al doilea motor complet**,
cu engine-uri proprii (`api/workers/contentiq/engines/eeat.py`, `freshness.py`) care
duplică `prompts/content_quality.yaml` și `prompts/content_freshness.yaml`.
`draft_optimizer` (5) e al treilea path.

Țintă: **un motor, patru moduri** — `site` / `page` / `draft` / `god-mode` — și N prompturi.
Se face ultima pentru că e inima produsului (84 de audituri, 6.352 de rezultate) și pentru
că etapele anterioare îți dau uneltele (client LLM unificat, structured outputs, teste).

### 5.6 Curățenie (cod dovedit mort)
Singurele ștergeri propriu-zise, toate verificate cu import-graph:
- toolchain CLI legacy: `main.py` (root) + `core/{determine_score,generate_dashboard,generate_report,validate_audit}.py` — ~6.600 LOC, **zero importuri din `api/` sau `app/`**.
  ⚠️ `core/validate_audit.py` validează scheme YAML de prompturi — **recuperează-l ca test** (util în Etapa 6) înainte de a-l șterge.
- `api/models/schemas_clusteriq.py` — untracked, zero importuri în tot repo-ul
- 4 fișiere `.db` de 0 bytes de la rădăcină (DB-ul real e `api/data/analyzer.db`)
- 8 worktrees stale (`git worktree list` → toate cu 0 commit-uri față de master;
  `heuristic-raman` și `magical-brown` sunt directoare orfane, neînregistrate)
- `.gitignore` pentru `BUILD_LOG.md`, `*_report.txt`, `website_llm_analyzer.log.*`, `uvicorn_*.txt`

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

Abia aici se ating prompturile — și doar cu un harness de evaluare:

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
