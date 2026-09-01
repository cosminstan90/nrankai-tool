# Raport consolidat de audit — nrankai-tool (geo_tool)

**Data:** 2026-09-01
**Metodă:** 9 faze (`docs/CODEBASE_AUDIT_PLAN.md`), read-only, fiecare finding
verificat prin citire de cod, execuție directă, sau apel live pe serverul
local. Rapoartele complete per fază sunt în `docs/audit/0N-*.md`.

## Cifre cheie

- 179 fișiere `.py`, 61.916 LOC, 315 path-uri API, 57 template-uri, 20 prompturi
- **95 de zile** de la ultimul commit (2026-05-28) până azi
- **4 endpoint-uri 100% rupte** găsite dintr-un smoke test de 102 GET-uri simple
- Cauza rădăcină a 2 din cele 4: **o funcție de auto-migrare scrisă corect dar niciodată apelată**
- 26% dintre auditurile istorice au eșuat (22/84) — dar cu logging detaliat păstrat corect

---

## P0 — Rupt acum, în producție

### 1. Conectarea GSC (Google Search Console) e complet imposibilă
`api/routes/gsc/oauth_sync.py:81` — `NameError: name '_CLIENT_CONFIG' is not defined`.
`GET /api/gsc/oauth/authorize` crapă 100% din timp; confirmat live inclusiv
prin click pe butonul „Connect Google" din UI (`/gsc`) → utilizatorul vede
`{"detail":"Internal server error"}` în loc de ecranul Google.
**Fix:** import lipsă — `_shared.py` are deja o funcție `_client_config()`
echivalentă; probabil trebuie doar importată în loc de referința ruptă.
*(Faza 2 F2-01, Faza 8 F8-05)*

### 2. Portfolio Dashboard — 3 endpoint-uri rupte de un singur bug de timezone
`api/routes/portfolio.py:344` **și** `:609` —
`TypeError: can't subtract offset-naive and offset-aware datetimes`.
Rupe `GET /api/portfolio/alerts`, `GET /api/portfolio/overview` **și**
`GET /api/portfolio/website/{domain}`. Confirmat live: `/portfolio` se
încarcă complet gol, fără mesaj de eroare vizibil utilizatorului.
Cauză structurală: toate cele 115 coloane `DateTime` din schema DB sunt
fără `timezone=True` — SQLite+SQLAlchemy întorc naiv orice valoare la
citire, indiferent cum a fost scrisă. Există deja în cod (`audits.py:610`)
pattern-ul corect de reparat (`if x.tzinfo is None: x = x.replace(tzinfo=timezone.utc)`).
**Fix:** aplicați acel pattern la ambele locuri din `portfolio.py`.
*(Faza 2 F2-02, Faza 3 F3-03/F3-04, Faza 8 F8-04)*

### 3. `/api/briefs` (listă content briefs) complet nefuncțională
`sqlite3.OperationalError: no such column: content_briefs.current_score`.
Modelul SQLAlchemy are 2 coloane (`current_score`, `executive_summary`) care
nu există în DB-ul real — nicio migrare, nici Alembic nici ad-hoc, nu le-a
adăugat vreodată. UI-ul (`/briefs`) se degradează grațios (arată „0" în loc
să crape), dar brief-urile existente nu pot fi niciodată văzute.
**Fix:** 2 linii de `ALTER TABLE content_briefs ADD COLUMN ...` (manual sau
migrare Alembic nouă).
*(Faza 2 F2-03, Faza 3 F3-01, Faza 8 F8-06)*

### 4. `/api/fanout/sessions` complet nefuncțională — 10 coloane lipsă
Aceeași clasă de bug ca #3, dar mai gravă: **10 coloane** lipsesc din
`fanout_sessions`. Cauza e cunoscută cu precizie: codul de reparare
**există deja și e complet** — `api/models/database.py:230-260`,
funcția `init_db_async()` — dar acea funcție **nu e apelată de nimeni**.
`api/main.py:71` cheamă în schimb `init_db()` (versiunea sincronă), care
are propriile migrări ad-hoc pentru alte 3 tabele, dar nimic pentru
`fanout_sessions`.
**Fix:** conectați `init_db_async()` la startup (cu grijă la suprapunerea
cu `init_db()` — au cod duplicat pentru alte tabele) sau portați blocul de
10 coloane în `init_db()`.
*(Faza 2 F2-04/F2-05, Faza 3 F3-01, Faza 8 F8-08)*

### 5. Starea Alembic e „fictivă" — un `alembic upgrade` viitor ar putea rupe tot
DB-ul e la revizia `0005`, head-ul e `0008` — dar toate cele 80 de tabele
din model (inclusiv cele din migrările 0006-0008, ClusterIQ/SerpIQ) **există
deja** în DB, create de `Base.metadata.create_all()`, nu de Alembic.
`alembic check` refuză să ruleze („Target database is not up to date").
Un `alembic upgrade head` rulat naiv ar încerca să recreeze tabele deja
existente → foarte probabil eșec cu „table already exists".
**Fix:** `alembic stamp head` (fără DDL, doar sincronizare de metadată),
apoi o migrare nouă `0009` pentru cele 12 coloane lipsă de la #3/#4.
*(Faza 3 F3-02)*

### 6. Auto-înregistrarea webhook-ului n8n poate pica tot serverul la pornire
`api/main.py:195-205` — blocul care înregistrează `N8N_WEBHOOK_URL` în DB la
startup **nu are `try/except`**, spre deosebire de blocul imediat următor
din același fișier care face o operație similară și e protejat corect.
Dacă acest INSERT eșuează (tabelă lipsă, DB blocată), tot `lifespan()`
crapă și serverul nu mai pornește.
**Fix:** un `try/except` de 3 linii, după modelul deja prezent mai jos în
același fișier.
*(Faza 4 F4-03)*

### 7. Fallback-uri de model retrase/greșite în `citation_tracker.py`
Două fallback-uri hardcodate în același fișier, verificate direct față de
catalogul oficial Anthropic (skill `claude-api`):
- `"claude-3-5-sonnet-20241022"` — **retras oficial din 2025-10-28** (aproape un an)
- `"llama-3.1-sonar-large-128k-online"` — schema veche Perplexity, retrasă în 2025

Orice apel din acest fișier care nu specifică explicit un model va eșua.
**Fix:** trageți fallback-urile din `api/provider_registry.py::get_default_model()`
în loc să le hardcodați separat aici — asta ar preveni și recurența.
*(Faza 4 F4-01/F4-02)*

---

## P1 — Degradat / risc apropiat

| # | Titlu | Detaliu | Sursă |
|---|---|---|---|
| 8 | Modelul Anthropic implicit al **întregii aplicații** (`claude-sonnet-4-20250514`, 17 fișiere, 38 apariții) e oficial „Deprecated (retiring soon)" | Nu rupe nimic azi, dar retragerea ar rupe simultan aproape toate tipurile de audit. Migrare recomandată către `claude-sonnet-5` (mai ieftin: $2/$10 vs $3/$15 per MTok) | Faza 4 F4-04 |
| 9 | 3 implementări OAuth Google paralele pentru GSC (`gsc/_shared.py`, `gsc_fanout.py`, `workers/contentiq/gsc.py`) — doar prima gestionează explicit `invalid_grant` | Risc: token expirat tratat ca eroare temporară în loc de re-autorizare clară, pe 2 din 3 flow-uri | Faza 4, secțiunea C |
| 10 | SSRF: 2 implementări divergente — cea de pe calea principală (`api/utils/url_validator.py`, folosită de UI-ul lui Cosmin) **nu rezolvă DNS**; cea de pe calea publică (`lead_audit_worker.py`, primește URL-uri de la utilizatori anonimi de pe nrankai.com) **rezolvă corect** | Ironie: partea mai expusă are protecția mai bună. Consolidați pe implementarea din `lead_audit_worker.py` | Faza 7 F7-06 |
| 11 | 92 de apeluri `print()` cu caractere non-ASCII rămase în 16 fișiere (aceeași clasă de bug reparată o dată în commit `2588815`) — inclusiv în `init_db()`, apelat la fiecare pornire | Risc specific Windows fără `PYTHONUTF8=1`; azi mascat de `restart_server.bat` | Faza 6 F6-04 |

---

## P2 — Duplicat / mort / risc minor

| # | Titlu | Sursă |
|---|---|---|
| 12 | Toolchain CLI legacy complet neconectat (`main.py` root + `core/determine_score.py` + `generate_dashboard.py` + `generate_report.py` + `validate_audit.py`) — **~6.600 linii moarte** pentru aplicația web | Faza 1 F1-01 |
| 13 | `clean_json_response()` triplicată, cu robustețe inegală — versiunea din `schema_gen.py` nu repară deloc JSON malformat, cea din `direct_analyzer.py` e completă | Faza 1 F1-02 |
| 14 | 17 fișiere instanțiază clienți LLM direct în loc să refolosească `AsyncLLMClient` din `direct_analyzer.py` (deja folosit corect de 3 fișiere) | Faza 1 F1-03 |
| 15 | 8 directoare de worktree stale (`.claude/worktrees/`), 0 commit-uri neintegrate — sigur de șters | Faza 1 F1-04 |
| 16 | `api/models/schemas_clusteriq.py` — fișier untracked, zero importuri; nu e risc de build (contrar suspiciunii inițiale), doar cod mort | Faza 1/3 F1-05/F3-06 |
| 17 | 4 tipuri de audit legacy (`relevancy_audit`, `greenwashing`, `advertisment`, `kantar`) fără YAML în `prompts/`, dar și fără nicio cale de a fi invocate din UI — cod mort, nu bug live | Faza 5 |
| 18 | Upload logo acceptă `.svg` — risc minor de stored-XSS dacă fișierul e deschis direct | Faza 7 F7-07 |
| 19 | Singurul test din proiect (`test_content_chunker.py`) nu rulează din locația lui (import greșit) + testul însuși e învechit (așteaptă un câmp redenumit) — codul de producție testat e de fapt corect | Faza 1 F1-09 |
| 20 | `content_brief.yaml`/`draft_optimizer.yaml` nu au output_schema documentat (dar au loader propriu, nu e bug) | Faza 5 F5-03/F5-06 |

---

## P3 — Igienă

- Gemini/Mistral fără cheie API în mediul local (posibil doar `.env` incomplet) — Faza 0
- `geo_audit.yaml`/`ux_content.yaml` încadrează LLM-ul cu „2024-2025"/„2025" ca prezent, acum învechit — Faza 5
- `draft_optimizer.py` folosește o generație de Sonnet diferită (`claude-sonnet-4-6`, de fapt validă) față de restul aplicației — Faza 4 F4-05
- CLAUDE.md: câteva chei de mediu folosite dar nedocumentate (`AHREFS_API_KEY`, `SERPER_API_KEY`, `VELOCITYCMS_*`, `WORKER_API_KEY`, `*_RPM`/`*_TPM`); afirmația despre GA4 OAuth e greșită (GA4 e CSV-only) — Faza 4
- `api/prompts/` conține un singur fișier nelegat de audituri — denumire derutantă față de CLAUDE.md — Faza 5
- 3 apariții rămase `datetime.utcnow()` — fără impact live confirmat — Faza 3
- `requirements.txt` — 0 versiuni pinuite din 55 — Faza 7
- Auth middleware: `SKIP_PATHS` cu prefix match neexact (inofensiv azi) — Faza 7

---

## Decizii de arhitectură pentru Cosmin

Următoarele nu sunt decizii pe care un agent ar trebui să le ia singur:

1. **Toolchain CLI legacy (P2 #12, ~6.600 linii)** — se arhivează definitiv,
   se păstrează ca tool separat documentat, sau se integrează treptat în
   aplicația web? Afectează și cele 4 tipuri de audit „moarte" (#17).
2. **`api/routes/` vs `app/modules/`** — verificat în Faza 1/3: nu există
   duplicare reală de rutare sau de modele de date între cele două stiluri.
   Rămâne totuși o întrebare de convenție: modulele viitoare (după
   ClusterIQ/SerpIQ) urmează stilul nou (`app/modules/<nume>/{router,services,models}`)
   sau cel vechi (`api/routes/<nume>.py`)?
3. **Consolidarea prompturilor** — `prompts/` (20 YAML) vs `api/prompts/`
   (1 `.txt`, nelegat) — un singur director, o singură convenție?
4. **Introducerea unei suite de teste reale** — `pytest` nici măcar nu e în
   `requirements.txt` azi. Cel mai bun punct de plecare, confirmat de acest
   audit: `core/prompt_loader.py` (validare schemă YAML) și
   `core/content_chunker.py` (are deja 34 de teste scrise, doar
   nefuncționale din cauza locației/importului — cel mai ieftin restart).
5. **Migrarea modelului Anthropic implicit** (P1 #8) — către `claude-sonnet-5`
   sau `claude-sonnet-4-6`? Afectează 17 fișiere; recomand `/claude-api migrate`
   după ce scope-ul e ales explicit.
6. **Rata de eșec de 26% a auditurilor istorice** (Faza 6) — nu e neapărat
   un bug de cod, dar merită investigată separat (ce fracție e input
   invalid de la utilizator vs. eroare reală de infrastructură).

---

## Quick wins — sub 15 minute fiecare, impact real

1. **Fix `_CLIENT_CONFIG` în `gsc/oauth_sync.py:81`** — import lipsă, o linie. Repară complet conectarea GSC. *(#1)*
2. **Guard de timezone în `portfolio.py:344` și `:609`** — copiați pattern-ul deja existent din `audits.py:610`. Repară 3 endpoint-uri. *(#2)*
3. **`try/except` în jurul blocului n8n din `api/main.py:195-205`** — 3 linii, elimină un risc de crash total la pornire. *(#6)*
4. **`git worktree remove` pe cele 6 worktree-uri înregistrate** + ștergere manuală a celor 2 directoare orfane — spațiu recuperat, elimină sursa unui raport flake8 inutilizabil. *(#15)*
5. **Ștergeți sau adăugați în git `api/models/schemas_clusteriq.py`** — clarificare simplă, elimină un fals-pozitiv de risc pentru viitoarele audituri. *(#16)*
6. **Reparați `test_content_chunker.py`**: mutați-l în `core/` (sau adăugați `conftest.py`) + actualizați linia 491 la `citation_probability` — 33/34 teste deja trec, câștigați o suită de regresie funcțională imediat. *(#19)*

---

## Ce n-a fost verificat (`NEEDS-RUNTIME-CHECK`)

- Model IDs OpenAI/Gemini/Perplexity (`gpt-4o`, `gemini-2.5-*`, `sonar*`) —
  fără catalog autoritar disponibil în acest audit; doar Anthropic a fost
  verificat riguros (skill `claude-api`).
- Comportamentul exact al `alembic upgrade head` pe DB-ul curent (am
  raționat din felul în care Alembic funcționează, nu am rulat comanda).
- Sistemul de operare al serverului de producție (app.nrankai.com) — dacă e
  Linux, riscul F6-04 (print non-ASCII) nu se aplică acolo.
- `pip list --outdated` / `pip-audit` — necesită acces la indexul PyPI.
- Bandit complet (raportul existent a scanat greșit worktrees).
- Endpoint-urile cu parametri de rută (119 din 315) — smoke test-ul Fazei 2
  a acoperit doar cele 102 fără parametri; F3-03 a găsit deja un al doilea
  bug ascuns tocmai într-un astfel de endpoint (`/api/portfolio/website/{domain}`) —
  posibil să mai existe altele similare, netestate.
