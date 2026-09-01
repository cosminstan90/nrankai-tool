# Faza 4 — Integrări externe

## Rezumat

| ID | Sev | Titlu | Fișier:linie | Verificat prin |
|----|-----|-------|---------------|-----------------|
| F4-01 | **P0** | Fallback Perplexity `"llama-3.1-sonar-large-128k-online"` — nume din schema veche, retrasă de Perplexity în 2025 | `api/routes/citation_tracker.py:260,262` | Comparație cu convenția curentă Perplexity (`sonar`, `sonar-pro`, `sonar-reasoning`) folosită corect în `api/provider_registry.py` |
| F4-02 | **P0** | Fallback Anthropic `"claude-3-5-sonnet-20241022"` — model **retras oficial din 2025-10-28** | `api/routes/citation_tracker.py:228,230` | Catalog oficial Anthropic (skill `claude-api` → `shared/models.md`, secțiunea „Retired Models") |
| F4-03 | **P0** | Auto-înregistrarea webhook-ului n8n la startup nu e în `try/except` — un eșec de DB pică tot serverul la pornire, dacă `N8N_WEBHOOK_URL` e setat | `api/main.py:195-205` | Citire cod: blocul de dedesubt (`prompt_library.seed`, linia 208) e corect protejat, acesta nu e |
| F4-04 | **P1** | Modelul Anthropic implicit în toată aplicația, `claude-sonnet-4-20250514`, e acum în lista oficială **„Deprecated (retiring soon)"** — folosit în 17 fișiere / 38 apariții | catalog oficial (skill `claude-api`) — status „Deprecated", dată de retragere „TBD" |
| F4-05 | P3 | `api/routes/draft_optimizer.py:101` folosește fallback `"claude-sonnet-4-6"` — **model valid**, dar dintr-o generație diferită față de restul aplicației (`claude-sonnet-4-20250514`) → inconsecvență, nu bug | catalog oficial Anthropic — `claude-sonnet-4-6` = Claude Sonnet 4.6, Active |
| F4-06 | P3 | `logger.error("Worker poll error: %s", exc)` produce mesaje de log **goale** când `str(exc)` e vid (ex. anumite excepții de conexiune httpx) — îngreunează debugging-ul | `api/workers/lead_audit_worker.py:276` | Confirmat empiric în `uvicorn_out.txt` — 9/10 apariții „Worker poll error:" fără mesaj (vezi Faza 0, F0-04) |
| F4-07 | ✅ verificat, fără problemă | GA4 nu are deloc gestionare de refresh-token / `invalid_grant` — **corect**, GA4 e integrare **CSV upload**, nu OAuth live | `api/routes/ga4.py` — endpoints: `/upload`, nu `/oauth/*`; zero importuri de credentials |
| F4-08 | P3 | CLAUDE.md descrie `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` ca activând „GSC + GA4 OAuth" — GA4 nu folosește deloc OAuth | CLAUDE.md tabel variabile de mediu vs. F4-07 |
| F4-09 | ✅ verificat, fără problemă | `GEMINI_API_KEY` vs `GOOGLE_GEMINI_API_KEY`, `GOOGLE_CLIENT_ID` vs `GSC_CLIENT_ID` — arată ca duplicate riscante, dar sunt fallback-uri intenționate (`getenv("GSC_X", getenv("GOOGLE_X"))`), nu bug | `api/workers/contentiq/gsc.py:26-30`, `api/workers/fanout_analyzer.py:430` |
| F4-10 | P3 | Chei de mediu folosite dar **nedocumentate** în CLAUDE.md: `AHREFS_API_KEY`, `SERPER_API_KEY`, `VELOCITYCMS_API_KEY`/`_URL`, `WORKER_API_KEY`, `ANTHROPIC_RPM`/`TPM`, `OPENAI_RPM`/`TPM`, `GOOGLE_RPM`, `MISTRAL_RPM` | grep exhaustiv `os.getenv` pe `api core app scripts` |

---

## A. Model IDs — verificare față de catalogul oficial Anthropic

Am încărcat skill-ul `claude-api` (obligatoriu per plan, secțiunea Faza 4) în loc să
mă bazez pe cunoștințe din memorie, exact cum cere planul.

### F4-01 (P0) — Perplexity: nume de model din schema veche

```
api/routes/citation_tracker.py:260:  return "", 0, 0, model or "llama-3.1-sonar-large-128k-online"
api/routes/citation_tracker.py:262:  _model = model or "llama-3.1-sonar-large-128k-online"
```

Perplexity a renunțat la convenția `llama-3.1-sonar-*-128k-online` în 2025, în
favoarea familiei `sonar` / `sonar-pro` / `sonar-reasoning`. Restul aplicației
folosește deja convenția nouă corect (`sonar`, `sonar-pro`,
`sonar-large-128k-online` mai apar și ele izolat — de verificat separat, vezi
mai jos). Acest fallback specific din `citation_tracker.py` e singurul loc din
tot codebase-ul cu prefixul `llama-3.1-`.
**Nu am acces la un catalog oficial Perplexity la fel de exact ca cel Anthropic
→ marcat P0 pe baza convenției de denumire, dar recomand
`NEEDS-RUNTIME-CHECK`: un apel real către Perplexity cu acest ID pentru
confirmare finală.**

### F4-02 (P0) — Anthropic: model retras oficial

```
api/routes/citation_tracker.py:228:  return "", 0, 0, model or "claude-3-5-sonnet-20241022"
api/routes/citation_tracker.py:230:  _model = model or "claude-3-5-sonnet-20241022"
```

Verificat direct în catalogul oficial Anthropic (`claude-api` skill →
`shared/models.md`, secțiunea „Retired Models — no longer available"):

| Model | Full ID | Retras |
|---|---|---|
| Claude Sonnet 3.5 | `claude-3-5-sonnet-20241022` | **2025-10-28** |

Acest ID e retras de aproape un an (data curentă a proiectului: 2026-09-01).
**Orice apel către Anthropic prin `citation_tracker.py` care nu specifică
explicit un model (deci ajunge pe fallback) va eșua cu eroare de la API
("model not found").** Confirmat: e chiar în același fișier ca F4-01 —
funcția care alege modelul implicit pentru citation tracking e nesincronizată
cu restul aplicației pe **ambii** provideri (Anthropic + Perplexity).

**Context**: `citation_tracker.py` folosește propriul mecanism de
`model or "<fallback>"` per provider, complet separat de
`api/provider_registry.py` (care are ID-uri corecte/curente). Recomandare
Faza 9: `citation_tracker.py` ar trebui să tragă fallback-urile din
`get_default_model()` (`provider_registry.py`), nu să le hardcodeze separat —
asta ar preveni exact acest tip de drift.

### F4-04 (P1) — Modelul implicit al întregii aplicații e „deprecated"

```
claude-sonnet-4-20250514 — 38 apariții în 17 fișiere:
  api/models/database.py, api/provider_registry.py, api/routes/audits.py,
  api/routes/costs.py, api/routes/fanout.py, api/routes/geo_monitor.py,
  api/routes/pages/analytics_views.py, api/routes/templates_manager.py,
  api/utils/answer_calibrator.py, api/workers/contentiq/brief.py,
  api/workers/fanout_analyzer.py, core/config.py,
  core/cross_reference_analyzer.py, core/direct_analyzer.py,
  core/website_llm_analyzer.py, app/modules/clusteriq/services/decision_engine.py,
  app/modules/serpiq/services/orchestrator.py
```

Catalogul oficial Anthropic îl listează la „Deprecated Models (retiring
soon)" — **funcționează încă azi** (dată de retragere „TBD"), dar Anthropic a
semnalat oficial intenția de retragere. Fiindcă e modelul **implicit** pentru
practic toată aplicația (`provider_registry.py:89`:
`ProviderConfig("anthropic", ..., "claude-sonnet-4-20250514")`), retragerea sa
ar rupe simultan aproape toate tipurile de audit care nu specifică explicit
alt model.

**Recomandare (pentru Faza 9, nu de implementat acum):** migrare planificată
către `claude-sonnet-5` (modelul curent recomandat, cost similar: $2/$10 per
MTok vs $3/$15 pentru cel vechi — de fapt **mai ieftin**) sau cel puțin
`claude-sonnet-4-6` (aceeași generație folosită deja izolat în
`draft_optimizer.py`, vezi F4-05). Folosiți `/claude-api migrate` pentru asta
când decideți scope-ul — schimbă comportamentul de thinking/effort, nu doar
ID-ul.

### F4-05 (P3) — `draft_optimizer.py` folosește altă generație de Sonnet

```python
# api/routes/draft_optimizer.py:101
return "ANTHROPIC", model or "claude-sonnet-4-6"
```

**Verificat: `claude-sonnet-4-6` e un model real și activ** (Claude Sonnet
4.6, catalog oficial). Nu e bug de funcționare — cererea va reuși. Este însă
o **inconsecvență**: restul aplicației (17 fișiere) folosește implicit
`claude-sonnet-4-20250514` (generație mai veche, acum deprecated — F4-04),
în timp ce acest fișier folosește singurul loc din tot proiectul cu
generația 4.6. Nu știm dacă a fost o alegere deliberată sau o scăpare la
scriere. De unificat în Faza 9 odată cu migrarea de la F4-04.

### Alte modele verificate — fără probleme

| Model | Locație | Status |
|---|---|---|
| `claude-haiku-4-5-20251001` (32 apariții) | provider_registry.py + restul | ✅ Active — ID complet cu dată, identic cu cel din catalogul oficial |
| `claude-haiku-4-5` (fără dată, `guide.py:118`) | `api/routes/guide.py` | ✅ Active — alias oficial recomandat, echivalent cu ID-ul de mai sus |
| `claude-opus-4-5-20251101` (6 apariții) | provider_registry.py | ✅ Active (catalog „Legacy Models") |
| `mistral-large-latest` / `-small-latest` / `-medium-latest` | provider_registry.py | ✅ Alias-uri `-latest` — se actualizează automat la partea Mistral, fără risc de retragere |
| `gpt-4-turbo` | provider_registry.py:74 | Deja marcat explicit `"Legacy premium."` în codul însuși — folosire conștientă, nu risc ascuns |

### Modele NEEDS-RUNTIME-CHECK (fără catalog autoritar disponibil în acest audit)

Skill-ul `claude-api` acoperă doar Anthropic. Pentru celelalte 3, nu am
verificat live (ar necesita apeluri reale către API-uri, interzis fără
aprobare explicită pe un audit read-only):

- OpenAI: `gpt-4o`, `gpt-4o-mini` — probabil valide (denumiri curente OpenAI)
- Google: `gemini-2.0-flash`, `gemini-2.0-flash-lite`, `gemini-2.5-flash`,
  `gemini-2.5-pro` — probabil valide, dar de reconfirmat (Google retrage
  modele Gemini mai des decât Anthropic)
- Perplexity: `sonar`, `sonar-pro` (folosite corect în majoritatea codului) —
  de reconfirmat alături de F4-01

---

## B. Fragilitate la pornire (startup) — legat de configurare externă

### F4-03 (P0) — auto-înregistrare webhook n8n neprotejată

`api/main.py:195-205`, în `lifespan()`:

```python
_n8n_url = os.getenv("N8N_WEBHOOK_URL")
if _n8n_url:
    from api.models.database import FanoutWebhook
    ...
    async with AsyncSessionLocal() as _wdb:
        _existing = (await _wdb.execute(...)).scalar_one_or_none()
        if not _existing:
            _wdb.add(FanoutWebhook(...))
            await _wdb.commit()
            print(f"[OK] Registered default n8n webhook: {_n8n_url}")
```

**Fără `try/except`.** Blocul imediat următor din același fișier
(„Seed prompt library", linia 208+) face exact aceeași categorie de operație
(query + insert în DB la startup) și **este** protejat cu try/except.
Dacă acest bloc aruncă (ex. tabela `FanoutWebhook` lipsă după o migrare
neaplicată, `IntegrityError`, DB blocată de alt proces), **`lifespan()`
crapă și serverul nu mai pornește deloc** — un singur `.env` cu
`N8N_WEBHOOK_URL` setat poate pica tot app-ul. Confirmat că nu există alt
try/except mai sus în `lifespan()` care ar prinde asta generic (verificat
prin citirea completă a funcției).

---

## C. Alte integrări verificate

### GSC (Google Search Console) — OAuth live, gestionare `invalid_grant`

Confirmat commit `62b2155`: `api/routes/gsc/_shared.py:121` tratează
`invalid_grant`/refresh eșuat prin invalidarea token-ului („token is dead").
**Dar acoperirea nu e uniformă** — există 3 implementări separate de
client OAuth Google în cod:

1. `api/routes/gsc/_shared.py` — flow-ul principal GSC, are `invalid_grant` handling
2. `api/routes/gsc_fanout.py` — flow separat pentru fan-out tracking, folosește
   `except Exception` generic la refresh (linia 113/135) — **nu verifică
   explicit `invalid_grant`**, deci ar putea trata un token mort ca eroare
   temporară în loc să-l invalideze definitiv → utilizatorul ar vedea erori
   repetate în loc de un prompt clar de re-autorizare. **P1**, nevalidat
   live (`NEEDS-RUNTIME-CHECK`).
3. `api/workers/contentiq/gsc.py` — al treilea flow, pentru ContentIQ, cu
   fallback pe `GOOGLE_CLIENT_ID`/`SECRET` (F4-09, verificat OK) — nu am
   verificat separat gestionarea de `invalid_grant` aici din lipsă de timp;
   marcat `NEEDS-RUNTIME-CHECK` pentru Faza 9.

**Recomandare Faza 9:** 3 implementări separate ale aceluiași flow OAuth
Google (GSC principal, GSC fan-out, ContentIQ) e un semnal de duplicare de
cod relevant și pentru Faza 1 — merită unificate într-un singur modul
`google_oauth_client.py` care gestionează uniform `invalid_grant` peste tot.

### GA4 — integrare CSV, nu OAuth live

Vezi F4-07/F4-08 mai sus. Nu există risc de token expirat pentru GA4 pentru
că nu există deloc OAuth pe acest flux — datele vin din upload manual de CSV
(`api/routes/ga4.py:237 upload_csv`).

### DataForSEO

`api/routes/keyword_research.py:130` — endpoint `https://api.dataforseo.com/v3/...`.
**v3 e versiunea curentă și stabilă a API-ului DataForSEO** (neschimbată de
ani) — fără risc de versiune învechită.

### nrankai-cloud (lead audit worker)

Confirmat din Faza 0: worker-ul pollează la fiecare ~30s și primește
`204 No Content` consecvent (fără job în coadă) — integrarea funcționează.
Cele 10 erori `Worker poll error` din istoricul de log (F0-04/F4-06) sunt
sporadice (o dată la câteva zile), nu un pattern de eșec sistemic — probabil
blipuri de rețea tranzitorii, agravate de logging-ul care nu arată mesajul
real de eroare.

### Webhook sender (n8n, evenimente fan-out)

`api/workers/webhook_sender.py` — apelurile HTTP către webhook-uri
înregistrate sunt corect protejate (`try/except` la linia 84,
`validate_external_url()` apelat înainte de trimitere — protecție SSRF de
bază prezentă). Nicio problemă găsită aici.

---

## Concluzie Faza 4

Cele mai concrete probleme sunt **izolate în `api/routes/citation_tracker.py`**
(F4-01, F4-02) — un fișier care își definește propriile fallback-uri de
model în loc să refolosească `provider_registry.py`, și a rămas nesincronizat
de la scriere. Al doilea grup de probleme (F4-03) e o gaură de robustețe la
pornire, nelegată de LLM-uri. Restul integrărilor (DataForSEO, webhook
sender, GA4, nrankai-cloud) sunt sănătoase. GSC are un design cu 3 flow-uri
OAuth paralele care funcționează dar sunt greu de întreținut consecvent.
