# Plan de verificare completă — geo_tool (nrankai-tool)

> **Pentru cine:** sesiuni Claude Sonnet care execută auditul, fază cu fază.
> **Context:** proiect de ~6 luni (primul commit 2026-03-20), 179 fișiere .py trackuite,
> 63 module de rute, 34 workers, 49 template-uri, 20 prompturi YAML, 8 migrări Alembic.
> Zero suită de teste.
> **Scop:** duplicate, cod mort, funcționalități rupte, drift între cod / DB / prompturi.

---

## 0. Reguli de angajament (valabile în TOATE fazele)

1. **Fazele 1–8 sunt READ-ONLY.** Nu se modifică cod de proiect. Se scrie DOAR în
   `docs/audit/`. Excepție: Faza 0 poate porni serverul.
2. **Nu se ating fără plan explicit aprobat:** `prompts/`, `api/prompts/`, `core/`,
   `migrations/`, `api/middleware/auth.py`, `api/models/database.py` (vezi CLAUDE.md).
3. **EXCLUDE `.claude/worktrees/` din orice scan.** Conține 8 worktrees stale
   (`angry-dijkstra`, `cranky-edison-f2b5f2`, `dreamy-lewin`, `frosty-euler-8c7531`,
   `heuristic-kepler-6b893b`, `heuristic-raman`, `magical-brown`, `quizzical-poincare`)
   — copii complete ale proiectului. Raportul flake8 existent are **34.397 linii**
   exact pentru că a scanat acolo; e inutilizabil. Folosește mereu:
   `--exclude=.claude,venv,.venv,__pycache__,node_modules,migrations/versions`
   sau limitează scanarea la `api core app prompts scripts`.
4. **Fiecare finding trebuie verificabil.** Format obligatoriu: fișier:linie + ce e
   greșit + cum s-a verificat + impact. Fără „probabil".
5. **Nu raporta ca bug ceea ce nu ai executat sau citit.** Ce nu poate fi verificat
   static (ex. o cheie API expirată) se marchează `NEEDS-RUNTIME-CHECK`.
6. Nu porni sub-agenți. Nu face refactor „din mers". Nu comite nimic.

### Taxonomie severitate

| Nivel | Definiție |
|---|---|
| **P0 — Rupt** | Feature inaccesibil, endpoint care crapă 100%, integrare moartă, pierdere de date |
| **P1 — Degradat** | Merge parțial / eșec tăcut / rezultat greșit fără eroare |
| **P2 — Duplicat / mort** | Cod duplicat, fișier orfan, rută neînregistrată, template nefolosit |
| **P3 — Igienă** | Stil, import nefolosit, `print()` în loc de logger, lipsă tipuri |

### Artefactul comun

```
docs/audit/
  00-baseline.md        # Faza 0
  01-dead-code.md       # Faza 1
  02-routes.md          # Faza 2
  03-data-layer.md      # Faza 3
  04-integrations.md    # Faza 4
  05-prompts.md         # Faza 5
  06-workers.md         # Faza 6
  07-security-config.md # Faza 7
  08-frontend.md        # Faza 8
  FINDINGS.md           # Faza 9 — raport consolidat + backlog prioritizat
```

Fiecare fișier de fază începe cu un tabel rezumat, apoi findings detaliate:

```markdown
| ID | Sev | Titlu | Fișier:linie | Verificat prin |
|----|-----|-------|--------------|----------------|
| F1-01 | P0 | ... | api/routes/x.py:42 | curl / citire cod / import test |
```

---

## Faza 0 — Baseline: pornește-l și inventariază

**Obiectiv:** să știm ce merge ACUM, înainte să judecăm orice.

1. **Verifică importurile** (fără să pornești serverul):
   ```bash
   python -c "import api.main" 2>&1 | tail -30
   ```
   Orice `ImportError` aici = **P0** și blochează restul fazelor.

2. **Pornește serverul** (`restart_server.bat`) și confirmă:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/health
   ```

3. **Dump OpenAPI** — sursa de adevăr pentru Fazele 2 și 8:
   ```bash
   curl -s http://127.0.0.1:8000/openapi.json > docs/audit/openapi.json
   ```

4. **Citește logurile de pornire**: `uvicorn_err.txt`, `uvicorn_out.txt`,
   `website_llm_analyzer.log*`. Notează orice warning/traceback la startup, mai ales
   din `lifespan()` — acolo pornesc workerii și pot muri tăcut.

5. **Inventar cantitativ** în `00-baseline.md`: fișiere .py per director, LOC, nr. rute
   din OpenAPI, template-uri, prompturi, migrări. Plus vechimea per modul:
   ```bash
   for f in api/routes/*.py; do echo "$(git log -1 --format=%ci -- $f) $f"; done | sort
   ```
   Modulele netouched de >4 luni = candidați prioritari pentru Faza 1.

**Gate:** dacă serverul nu pornește, oprește-te și raportează. Nu continua orb.

---

## Faza 1 — Cod mort și duplicate

**Obiectiv:** ce se poate șterge fără să se rupă nimic.

### Leads deja confirmate prin recon (verifică-le, nu le presupune)

| Observat | De verificat |
|---|---|
| **Două arhitecturi paralele**: `api/routes/*.py` (63 fișiere, stil vechi) vs. `app/modules/{clusteriq,serpiq}/` (stil nou: `services/`, `tasks/`, `schemas.py`, `models.py`) | Se dublează logica? `app/modules/serpiq/schemas.py` vs. `api/models/schemas_clusteriq.py` (fișier **untracked!**) vs. `api/models/content.py`. Care e convenția-țintă? |
| **`main.py` la root** = orchestrator CLI (`scrape → convert → analyze → score`), separat de `api/main.py` | Mai e folosit? Duplică `api/workers/audit_worker.py`, care face aceiași pași? Dacă e legacy → propune arhivare |
| **4 fișiere `.db` la root, toate 0 bytes**: `analyzer.db`, `database.db`, `geo.db`, `geo_tool.db`. Codul folosește doar `analyzer.db` (`api/models/_base.py:17`) | Confirmă unde e DB-ul real (`DATABASE_DIR`). Celelalte 3 → șterse. Verifică și că nu sunt în `.gitignore` greșit |
| **8 worktrees stale** în `.claude/worktrees/` | `git worktree list`; pentru fiecare branch, `git log master..<branch>` — dacă e gol, propune `git worktree remove` |
| Untracked: `BUILD_LOG.md`, `bandit_report.txt`, `flake8_report.txt`, `website_llm_analyzer.log.1/.2`, `api/models/schemas_clusteriq.py`; modificate: `uvicorn_out.txt`, `uvicorn_err.txt` | Logurile și rapoartele → `.gitignore`. `schemas_clusteriq.py` → vezi Faza 3, e P0 dacă se importă |
| **Un singur test**: `test_content_chunker.py` la root, fără pytest config | Rulează-l. Trece? Mai e relevant? |

### Metodă

1. **Module niciodată importate:**
   ```bash
   for f in $(git ls-files 'api/*.py' 'api/**/*.py' 'core/*.py' 'app/**/*.py'); do
     m=$(basename "$f" .py); [ "$m" = "__init__" ] && continue
     n=$(grep -rl "\b$m\b" --include="*.py" api core app scripts 2>/dev/null | grep -v "^$f$" | wc -l)
     [ "$n" -eq 0 ] && echo "ORPHAN: $f"
   done
   ```
   Fals-pozitive de eliminat manual: routere înregistrate prin `api/routes/__init__.py`,
   module importate dinamic (`importlib`, nume în string-uri), workers porniți în `lifespan()`.

2. **Duplicate reale de logică** — nu te baza pe clone-detection automat:
   ```bash
   grep -rhoP "^(async )?def \K\w+" --include="*.py" api core app | sort | uniq -c | sort -rn | head -40
   ```
   Pentru fiecare nume cu ≥3 apariții, **citește** implementările și decide: duplicat
   real (copy-paste) vs. omonimie legitimă. Suspecți probabili în acest proiect:
   scoring, scraping, normalizare URL, apel LLM, parsare JSON din răspuns LLM.

3. **Apeluri LLM duplicate** — cel mai probabil loc de copy-paste:
   ```bash
   grep -rn "AsyncAnthropic\|AsyncOpenAI\|messages.create\|chat.completions" --include="*.py" api core app
   ```
   Există un client wrapper comun? Dacă fiecare modul își face al lui → **P2** + risc
   **P1** (retry / timeout / error-handling inconsistente).

4. **Funcții definite dar neapelate** (exclude route handlers și `__dunder__`).

**Deliverable:** `docs/audit/01-dead-code.md` — fiecare candidat cu dovada că e mort
(unde s-a căutat) și riscul ștergerii.

---

## Faza 2 — Rute și endpoint-uri: ce e chiar accesibil

1. **Rute definite vs. înregistrate.** Compară toate `@router.<verb>` din
   `api/routes/**` și `app/modules/**/router.py` cu `paths` din `openapi.json`.
   Diferența = router definit dar neinclus în `api/main.py` → **P2/P0**.
   Cele 46 de importuri de la `api/main.py:49` trebuie să acopere toate fișierele din
   `api/routes/` care definesc un `router`. Verifică și `api/routes/__init__.py`.

2. **Smoke test pe GET-uri.** Pentru fiecare path GET fără parametri, cu BasicAuth din `.env`:
   ```bash
   curl -s -u "$AUTH_USERNAME:$AUTH_PASSWORD" -o /dev/null -w "%{http_code} %{url_effective}\n" <url>
   ```
   **500 = P0. 404 pe pagină din meniu = P0.** NU chema POST/DELETE/PUT — doar le
   inventariezi. Pentru path-uri cu `{id}`, folosește un ID real din DB; altfel `NEEDS-DATA`.

3. **Pagini UI orfane.** `api/routes/pages/` are 15 module, `api/templates/` are 49
   template-uri. Mapează: template → handler care îl randează → link din navigație
   (`api/templates/base.html` + `partials/`).
   - template fără handler = **P2** (mort)
   - handler fără link în navigație = **P2** (feature „pierdut")
   - link în navigație către rută inexistentă = **P0**

4. **Rate limiting** (CLAUDE.md regula 1): `@limiter.limit` fără `request: Request`
   în semnătură = eroare la runtime → **P0**.
   ```bash
   grep -rn -A4 "@limiter.limit" --include="*.py" api app | grep "def " | grep -v "request"
   ```
   Verifică și că importul e din `api.limiter`, nu din `api.main`.

**Deliverable:** `docs/audit/02-routes.md` — tabel complet de status codes + matricea
template ↔ handler ↔ navigație.

---

## Faza 3 — Stratul de date: model vs. migrare vs. DB real

1. **Localizează DB-ul real** (`DATABASE_DIR`, `api/models/_base.py:17`) și dump-uie schema:
   ```bash
   python -c "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);[print(r[0]) for r in c.execute(\"select sql from sqlite_master where type='table'\")]" <path>.db > docs/audit/db_schema.sql
   ```

2. **Model SQLAlchemy vs. DB.** Pentru fiecare model din
   `api/models/{audit,analytics,content,infra}.py` + `app/modules/serpiq/models.py`:
   tabelul există? toate coloanele există? tipuri compatibile?
   **Coloană în model dar nu în DB = P0** — query-ul crapă la prima folosire.

3. **Alembic vs. model.** 8 migrări (`0001`–`0008`):
   - `alembic heads` — un singur head? (branch de migrare = **P1**)
   - `alembic current` pe DB-ul real — e la zi?
   - autogenerate dry-run: dacă produce diff nenul → drift **P1**.
     ⚠️ NU aplica migrarea generată. Doar raportează diff-ul.
   - **Capcană:** tabelele ClusterIQ/SerpIQ (`0006`–`0008`) au modelele în
     `app/modules/serpiq/models.py`, nu în `api/models/`. Sunt importate în
     `migrations/env.py`? Dacă nu, autogenerate le va vedea ca „de șters".

4. **`api/models/schemas_clusteriq.py` e untracked.** Se importă undeva? Dacă da,
   clonarea repo-ului pe altă mașină e ruptă = **P0**.

5. **Modele fără niciun query** (definite, nefolosite) = **P2**.

6. **Timestamps** (CLAUDE.md regula 6) — 3 apariții rămase:
   ```bash
   grep -rn "utcnow()" --include="*.py" api core app
   ```
   **P3** în general, dar **P1** dacă rezultatul e comparat cu un datetime aware
   (`TypeError: can't compare offset-naive and offset-aware`).

**Deliverable:** `docs/audit/03-data-layer.md` + `db_schema.sql`.

---

## Faza 4 — Integrări externe: ce mai e viu după 6 luni

Cea mai probabilă sursă de „nu mai funcționează". API-urile și modelele LLM se schimbă.

### Lead confirmat: model IDs vechi

Inventarul din recon arată modele de generație veche folosite în cod:

| Model ID găsit | Nr. apariții | De verificat |
|---|---|---|
| `claude-sonnet-4-20250514` | 38 | generație veche |
| `claude-haiku-4-5-20251001` | 32 | încă valid |
| `claude-opus-4-5-20251101` | 6 | generație veche |
| `claude-3-5-sonnet-20241022` | 2 | **foarte vechi** |
| `gpt-4o` / `gpt-4o-mini` | 21 / 20 | verifică status |
| `gpt-4-turbo` | 3 | **vechi** |
| `gemini-2.0-flash` / `-lite` | 8 / 6 | verifică status |
| `sonar-large-128k-online` | 2 | **denumire Perplexity retrasă** |

1. **Verifică fiecare ID** față de documentația curentă a providerului.
   **Model retras = P0** (auditul respectiv returnează eroare).
   > Pentru modelele Anthropic încarcă skill-ul `claude-api` înainte să judeci ID-uri
   > sau prețuri. Nu răspunde din memorie.

2. **`api/provider_registry.py`** — lista din UI corespunde cu ce se cheamă efectiv în cod?
   Model în registry dar necunoscut providerului = **P0** vizibil pentru user.

3. **Pattern try/except obligatoriu** (CLAUDE.md regula 4) pentru fiecare apel
   Anthropic/OpenAI/Gemini/Mistral/Perplexity. Apel fără handling = **P1**
   (workerul moare tăcut, auditul rămâne în „processing").

4. **Variabile de mediu** — compară ce citește codul cu CLAUDE.md și cu `.env`:
   ```bash
   grep -rhoP "getenv\(\s*[\"']\K[A-Z_]+" --include="*.py" api core app scripts | sort -u
   ```
   Cheie citită dar nesetată → feature mort tăcut = **P1**.
   ❗ Nu afișa valorile. Doar numele.

5. **OAuth Google (GSC + GA4).** Commitul `62b2155` a tratat `invalid_grant` într-un
   loc. E tratat peste tot? Există flow de re-autorizare accesibil din UI?

6. **DataForSEO** (`api/routes/keyword_research.py`) — versiunea de API și structura
   răspunsului mai corespund?

7. **nrankai-cloud** (`api/workers/lead_audit_worker.py`) — pollează
   `api.nrankai.com/api/lead-audits/next` la 30s. Din loguri: primește 200 sau eșuează
   în buclă? (zgomot + risc de rate-limit).

8. **Webhook n8n** (`api/workers/webhook_sender.py`, auto-înregistrare la startup) —
   URL-ul mai e valid?

**Deliverable:** `docs/audit/04-integrations.md` — tabel per integrare: status
(viu / mort / necunoscut), dovada, ce se rupe dacă e mort. Marchează explicit
`NEEDS-RUNTIME-CHECK` unde nu ai chemat efectiv API-ul.

---

## Faza 5 — Auditul prompturilor

20 YAML în `prompts/` + 1 `.txt` în `api/prompts/`. Verificăm consistență, acoperire
și potrivirea cu codul care le consumă.

### Lead confirmat — de investigat PRIMUL

Codul referă tipuri de audit care **nu au fișier YAML cu același nume**:

| Cheie folosită în cod | YAML existent |
|---|---|
| `relevancy_audit` / `output_relevancy_audit` | ❌ **niciunul** |
| `gdpr_audit` | `legal_gdpr.yaml` |
| `ecommerce_audit` | `e_commerce.yaml` |
| `freshness_audit` | `content_freshness.yaml` |
| `brand_voice_audit` | `brand_voice.yaml` |
| `local_seo_audit` | `local_seo.yaml` |
| `ux_content_audit` | `ux_content.yaml` |
| `ai_overview_audit` | `ai_overview_optimization.yaml` |

`relevancy_audit` apare în 5 fișiere din `core/`: `determine_score.py`,
`direct_analyzer.py`, `generate_dashboard.py`, `history_tracker.py`,
`monitor_completion_LLM_batch.py`.

**De stabilit:** există un tabel de mapping cheie→fișier? Caută în
`core/prompt_loader.py`, `core/config.py`, `core/audit_builder.py`.
- Dacă DA → documentează-l; e fragil dar funcțional (**P3**).
- Dacă NU → tipurile respective crapă cu `PromptNotFoundError` la rulare = **P0**.

Verifică prin execuție, nu prin citire:
```bash
python -c "from core.prompt_loader import PromptLoader; p=PromptLoader(); print(p.list_available())"
python -c "from core.prompt_loader import PromptLoader; print(PromptLoader().load('relevancy_audit'))"
```

### Restul verificărilor

1. **Schema uniformă** — compară cheile de nivel 1 din toate cele 20 de YAML-uri:
   ```bash
   python -c "
   import yaml,glob
   for f in sorted(glob.glob('prompts/*.yaml')):
       d=yaml.safe_load(open(f,encoding='utf-8'))
       print(f, sorted(d.keys()) if isinstance(d,dict) else type(d))"
   ```
   Prompt căruia îi lipsește un câmp pe care codul îl citește
   (`prompt.get(...)` în `prompt_loader.py` / `audit_builder.py`) = **P1**.

2. **Contractul de output vs. scoring.** Fiecare prompt cere LLM-ului un JSON.
   Câmpurile cerute trebuie să fie exact cele citite de `core/determine_score.py`
   și de template-ul care afișează rezultatul. Nepotrivire = scor greșit sau
   secțiune goală în UI, **fără nicio eroare** = **P1**. Ăsta e cel mai perfid tip
   de bug din proiect. Fă tabel: prompt → câmpuri cerute → câmpuri consumate → diff.

3. **Prompturi orfane** — YAML nechemat de nicio cale de cod = **P2**.
   Invers: tip de audit expus în UI (`new_audit.html`, `provider_registry.py`,
   `templates_manager.py`) fără YAML = **P0**.

4. **Igiena conținutului fiecărui prompt:**
   - instrucțiuni contradictorii sau repetate în același fișier
   - dimensiune (`wc -c prompts/*.yaml`) — outlierii cresc costul per audit
   - **conținut învechit**: referințe la ani sau la „cel mai recent" ceva, scrise acum
     6 luni. Ex.: praguri Core Web Vitals, denumiri de feature Google (SGE vs. AI
     Overviews), reguli GDPR. = **P1** pentru calitatea outputului
   - cerințe imposibile pentru un LLM fără tool-uri (ex. „măsoară viteza reală de
     încărcare") → produc halucinații = **P1**
   - limbă inconsistentă (RO vs. EN) între prompturi
   - format de output: JSON strict cerut? există exemplu? exemplul e JSON valid?

5. **`api/prompts/` conține un singur fișier** (`meta_generator.txt`), deși CLAUDE.md
   îl descrie ca director de prompturi. Clarifică: e legacy sau documentația e greșită?
   Propune consolidare într-un singur loc.

6. **Prompturi inline în cod** (ocolesc complet sistemul YAML = **P2**, imposibil de
   versionat sau editat din UI):
   ```bash
   grep -rn "You are a\|You are an\|Ești un\|system_prompt\s*=\s*[\"']\{3\}" --include="*.py" api core app
   ```

**Deliverable:** `docs/audit/05-prompts.md` — tabel per prompt: nume, folosit de,
schema OK, contract output OK, dimensiune, probleme de conținut.

---

## Faza 6 — Workers și joburi de fundal

34 de fișiere în `api/workers/`. Care mai rulează, care mor tăcut.

1. **Ce se pornește în `lifespan()`** din `api/main.py` vs. ce există în `api/workers/`.
   Worker existent, nepornit, neapelat de nicio rută = **P2**.

2. **Sesiuni DB în background** (CLAUDE.md regula 3): NU `Depends(get_db)`, ci
   `async with AsyncSessionLocal()`. Încălcare = **P0**.
   ```bash
   grep -rn "Depends(get_db)" --include="*.py" api/workers app/modules
   ```

3. **Lazy-load / greenlet.** Commitul `2efce3a` a reparat un greenlet error în SerpIQ
   (`_save_snapshot`). Aceeași clasă de bug: acces la relații lazy după închiderea
   sesiunii. Caută `selectinload`/`joinedload` lipsă acolo unde se accesează relații
   după `commit()`.

4. **Task-uri fără try/except la nivel de top** — o excepție într-un
   `asyncio.create_task` dispare complet tăcut. **P1**.

5. **Stări blocate în DB** — audituri rămase în `processing`/`pending` de mult timp =
   dovadă directă că un worker moare. Numără pe status și pe vechime.

6. **Scheduler** (`api/routes/schedules.py`, `api/workers/fanout_tracker_worker.py`) —
   chiar rulează? Care e ultima execuție înregistrată?

7. **`print()` cu non-ASCII.** Sunt 356 de `print()` în `api/ core/ app/`. Commitul
   `2588815` a reparat deja un `UnicodeEncodeError` din emoji în `print()` pe Windows.
   Caută aceeași bombă în rest:
   ```bash
   grep -rnP "print\(.*[^\x00-\x7F]" --include="*.py" api core app
   ```
   `restart_server.bat` setează `PYTHONUTF8=1`, dar orice proces pornit altfel
   (scripturi din `scripts/`, task scheduler, worker separat) nu îl are = **P0 potențial**.

**Deliverable:** `docs/audit/06-workers.md`.

---

## Faza 7 — Securitate și configurare

1. **Auth global.** `BasicAuthMiddleware` — verifică lista de excepții
   (`/api/health`, `/static/`, `/favicon.ico`). `/openapi.json` și `/docs` sunt
   protejate? Dacă nu → **P0** (tool intern expus la app.nrankai.com).

2. **Secrete în git** — pe istoric, nu doar pe working tree:
   ```bash
   git log --all --oneline -- .env
   git ls-files | grep -iE "\.env|credential|token|secret"
   ```
   Cheie comisă vreodată = **P0** + trebuie rotită.
   ❗ În raport scrii doar numele și locația, niciodată valoarea.

3. **CORS** — `ALLOWED_ORIGINS`; `*` în producție = **P1**.

4. **Bandit** — raportul existent are 1 linie, deci nu a rulat corect. Re-rulează curat:
   ```bash
   bandit -r api core app -x .claude,venv,__pycache__ -ll -f txt -o docs/audit/bandit_clean.txt
   ```

5. **SQL raw** — `text()` cu f-string = **P0**:
   ```bash
   grep -rn "text(f\"\|text(f'\|execute(f\"" --include="*.py" api core app
   ```

6. **SSRF pe scraper.** `core/web_scraper.py` primește URL de la user. Validează
   schema și hostul? Blochează IP-uri private (`127.x`, `10.x`, `169.254.x`,
   `192.168.x`)? = **P1**.

7. **Uploads.** `api/static/uploads/` e servit ca static. Fișiere încărcate de user
   servite din același origin = risc XSS. Ce tipuri se acceptă? = **P1**.

8. **`except:` gol** — 3 apariții — și `except Exception: pass`. Maschează erori.

9. **Dependențe** (`requirements.txt`, 55 linii): versiuni pinuite?
   ```bash
   pip list --outdated
   ```
   Compară cu importurile reale: pachete instalate nefolosite, și importuri fără
   pachet declarat (build rupt pe mașină curată = **P0**).

**Deliverable:** `docs/audit/07-security-config.md`.

---

## Faza 8 — Frontend: template-uri și JS inline

1. **Template-uri orfane** — confirmă lista din Faza 2.

2. **`extends` / `include` către fișiere inexistente** = **P0** la randare:
   ```bash
   grep -rhoP "(extends|include)\s+[\"']\K[^\"']+" api/templates | sort -u
   ```
   Compară cu fișierele reale din `api/templates/` (49 + `partials/`, `clusteriq/`, `serpiq/`).

3. **`fetch()` către endpoint-uri inexistente.** `api/static/` conține doar `css/` și
   `uploads/`, deci tot JS-ul e inline în template-uri:
   ```bash
   grep -rhoP "fetch\(\s*[\`\"']\K/api/[^\`\"'?]+" api/templates | sort -u
   ```
   Compară fiecare cu `paths` din `openapi.json`. Nepotrivire = **P0** — buton mort în UI.
   Ăsta e testul cel mai eficient pentru „nu mai funcționează".

4. **Runtime.** Deschide în browser preview paginile principale (dashboard, new_audit,
   results, keyword_research, gsc, fanout, clusteriq, serpiq) și citește erorile de
   consolă + request-urile 4xx/5xx. Screenshot pentru fiecare pagină ruptă.

5. **CSS duplicat / clase moarte** — doar dacă rămâne timp. **P3**.

**Deliverable:** `docs/audit/08-frontend.md` + screenshot-uri.

---

## Faza 9 — Consolidare și backlog

1. Adună findings-urile din `01`–`08` în `docs/audit/FINDINGS.md`.
2. **Deduplică** — aceeași cauză raportată în două faze = un singur finding.
3. **Grupează pe cauză-rădăcină, nu pe simptom.** Ex.: „12 endpoint-uri dau 500"
   poate fi un singur model ID retras.
4. Sortează P0 → P1 → P2 → P3; în interiorul nivelului, după efort estimat crescător.
5. Pentru fiecare finding, fix-ul propus în 1–3 propoziții. **Nu implementa nimic.**
6. Secțiune **„Decizii de arhitectură pentru Cosmin"** — ce nu decide un agent singur:
   - `api/routes/` vs. `app/modules/` — care devine standardul? migrăm sau înghețăm?
   - `main.py` (CLI) — se păstrează sau se arhivează?
   - consolidarea prompturilor: un singur director, o singură convenție de nume,
     un singur mapping cheie→fișier
   - se introduce o suită de teste? de unde se începe (`prompt_loader` + `audit_worker`)?
7. Secțiune **„Quick wins"** — fix-uri sub 15 minute fiecare, cu impact real.

---

## Ordinea de execuție

```
Faza 0  (blocantă)
   ├─> Faza 1  cod mort / duplicate
   ├─> Faza 2  rute            ──┐
   ├─> Faza 3  date             │
   ├─> Faza 4  integrări        ├─> Faza 9  consolidare
   ├─> Faza 5  prompturi        │
   ├─> Faza 6  workers          │
   ├─> Faza 7  securitate       │
   └─> Faza 8  frontend ────────┘   (are nevoie de Faza 0 + Faza 2)
```

Fazele 1–7 sunt independente. Rulează-le în **sesiuni Sonnet separate**, una câte una,
pentru context curat. Faza 8 după Faza 2. Faza 9 la final.
Estimare: ~1 sesiune per fază; Fazele 2 și 5 sunt cele mai mari.

---

## Prompt de pornire pentru fiecare sesiune Sonnet

> Citește `docs/CODEBASE_AUDIT_PLAN.md` — secțiunea „0. Reguli de angajament" și
> secțiunea „Faza N". Execută DOAR faza N. Read-only: nu modifica niciun fișier din
> proiect în afară de `docs/audit/0N-*.md`. Fiecare finding trebuie să aibă
> fișier:linie și metoda de verificare. Ce nu poți verifica prin execuție sau citire
> directă, marchează `NEEDS-RUNTIME-CHECK` — nu presupune. Exclude `.claude/worktrees/`
> din orice scan. La final, scrie în consolă rezumatul: nr. findings per severitate
> și top 3 cele mai grave.
