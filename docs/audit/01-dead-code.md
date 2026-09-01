# Faza 1 — Cod mort și duplicate

## Rezumat

| ID | Sev | Titlu | Verificat prin |
|----|-----|-------|-----------------|
| F1-01 | P2 | **Toolchain CLI legacy complet neconectat de aplicația web** — `main.py` (root) + `core/determine_score.py` + `core/generate_dashboard.py` + `core/generate_report.py` + `core/validate_audit.py` + parți din `core/history_tracker.py` — **~6.600 linii de cod**, 0 importuri din `api/` sau `app/` | import-graph exhaustiv + verificare manuală |
| F1-02 | P2 | 3 implementări divergente ale `clean_json_response()` — cea din `core/direct_analyzer.py` (folosită de pipeline-ul principal) are reparare structurală completă; cea din `api/routes/schema_gen.py` **nu are nicio reparare** | citire side-by-side a celor 3 corpuri de funcție |
| F1-03 | P2 | **17 fișiere** instanțiază direct clienți LLM (`AsyncAnthropic`/`AsyncOpenAI`) în loc să refolosească `AsyncLLMClient` din `core/direct_analyzer.py` (deja partajat corect de `audits.py`, `lead_audit_worker.py`, `compare.py`) | grep exhaustiv pe instanțiere clienți |
| F1-04 | P2 | 8 directoare de worktree stale în `.claude/worktrees/`, dintre care 6 sunt înregistrate în git — **toate cu 0 commit-uri neintegrate** în master; 2 (`heuristic-raman`, `magical-brown`) sunt directoare orfane, nici măcar înregistrate ca worktree | `git worktree list` + `git log master..<branch>` pentru fiecare |
| F1-05 | P2 | `api/models/schemas_clusteriq.py` — fișier untracked, **zero importuri** în tot proiectul | detaliat complet în `03-data-layer.md` F3-06, nu se repetă aici |
| F1-06 | P3 | 4 fișiere `.db` de 0 bytes la rădăcină (`analyzer.db`, `database.db`, `geo.db`, `geo_tool.db`) — DB-ul real e la `api/data/analyzer.db` | detaliat complet în `00-baseline.md` F0-02 |
| F1-07 | P3 | Fișiere untracked care ar trebui fie ignorate, fie comise: `BUILD_LOG.md`, `bandit_report.txt`, `flake8_report.txt` (inutilizabil — a scanat worktrees, vezi regula 3 din plan), `website_llm_analyzer.log.1/.2` | `git status` |
| F1-08 | ✅ verificat, fără problemă | Toate cele 46 de routere sunt corect conectate (vezi `02-routes.md` F2-06) — arhitectura duală `api/routes/` vs `app/modules/` **nu duplică logică de rutare**, doar convenție de organizare diferită pentru module noi (ClusterIQ/SerpIQ) | cross-referință |
| F1-09 | P2 | Singurul test din proiect nu rulează din locația lui (import greșit) — reparat manual pentru verificare, **33/34 teste trec**; singurul eșec e testul însuși, nu codul | vezi detaliu complet mai jos |

---

## F1-01 — Toolchain CLI legacy (detaliu)

Import-graph complet pe `api core app` + `scripts/` confirmă exact 3 module
100% neimportate de nimic:

```
core/generate_dashboard.py   (1999 linii) — dashboard HTML interactiv din output-uri CLI
core/generate_report.py      (1964 linii) — generator PDF din output-uri CLI
core/validate_audit.py        (658 linii) — validator YAML pentru prompturi (are __main__)
```

Toate au bloc `if __name__ == "__main__":` — sunt scripturi de linie de
comandă funcționale de sine stătător, nu cod mort din greșeală, dar complet
disconnectate de `api/`. Împreună cu `main.py` (root) și
`core/determine_score.py` (importat doar de `main.py`, vezi `05-prompts.md`
secțiunea „Investigație"), formează un **al doilea pipeline CLI complet**,
paralel cu aplicația web, care lucrează pe convenția `output_<tip_audit>/`
de foldere pe disc — o convenție complet diferită de baza de date +
`prompts/*.yaml` folosită de web app.

**De ce contează:** 4 din cele 5 tipuri de audit din acest pipeline
(`greenwashing`, `advertisment`, `kantar`, `relevancy_audit`) nu au deloc
YAML corespunzător în `prompts/` (vezi Faza 5) — pipeline-ul CLI e probabil
nefuncțional de mult, sau lucrează cu prompturi generate manual, în afara
sistemului curent.

**`core/validate_audit.py` e singura piesă cu potențial de reutilizare** —
validează structura YAML a prompturilor față de schemă, exact ce am făcut
manual în Faza 5. Ar putea deveni un test automat (`pytest`) dacă e adaptat.

**Recomandare Faza 9 (decizie pentru Cosmin, nu de implementat automat):**
- Dacă pipeline-ul CLI nu mai e folosit → arhivați-l într-un branch/tag și
  ștergeți-l din `main` (~6.600 linii moarte, ~10% din codul non-template).
- Dacă e încă folosit ocazional → mutați-l într-un director clar separat
  (`legacy_cli/`) cu un README care explică relația cu aplicația web, ca să
  nu mai fie confundat cu cod activ de viitoare sesiuni de audit.

---

## F1-02 — `clean_json_response` triplicat, cu robustețe inegală

| Locație | Reparare aplicată |
|---|---|
| `core/direct_analyzer.py` | strip fences → `json.loads` direct → reparări regex (virgule) → `json_repair` (librărie, reparare structurală profundă) |
| `api/routes/summary.py` | strip fences → `_repair_json()` local (reparare structurală, dar mai simplă) |
| `api/routes/schema_gen.py` | **doar** strip fences — nicio reparare |

Cele 18 tipuri de audit principale (prin `audits.py` → `direct_analyzer.py`)
beneficiază de reparare completă. `/api/schema` (generare schema markup) și
`/api/summary` (rezumate) au versiuni proprii, mai slabe — dacă LLM-ul
produce JSON ușor malformat (virgulă la final, ghilimele nescăpate), aceste
2 endpoint-uri au șanse mai mari să eșueze cu `json.JSONDecodeError`
netratat, unde pipeline-ul principal ar fi recuperat automat.

**Recomandare Faza 9:** extrageți versiunea din `direct_analyzer.py` într-un
modul comun (`core/json_utils.py` sau similar) și refolosiți-o peste tot —
elimină 2 din cele 3 copii și uniformizează robustețea.

---

## F1-03 — Clienți LLM instanțiați direct, în afara wrapper-ului comun

```
api/routes/citation_tracker.py       api/workers/contentiq/brief.py
api/routes/geo_monitor.py            api/workers/fanout_analyzer.py
api/routes/health.py                 api/workers/prompt_discovery.py
api/routes/meta_generator.py         api/workers/sentiment_analyzer.py
api/routes/query_suggestions.py      api/workers/velocitycms_bridge.py
api/routes/schema_gen.py             core/cross_reference_analyzer.py
api/routes/summary.py                core/perplexity_researcher.py
api/utils/answer_calibrator.py       app/modules/clusteriq/services/decision_engine.py
                                      app/modules/serpiq/services/orchestrator.py
```

`core/direct_analyzer.py` are deja o clasă `AsyncLLMClient` reutilizată
corect de `api/routes/audits.py`, `api/workers/lead_audit_worker.py` și
`api/routes/compare.py` (confirmat în Faza 4) — deci pattern-ul „corect"
există deja în codebase, dar nu e adoptat peste tot. `api/routes/health.py`
apare în listă probabil doar pentru un ping de sănătate (nu un apel real de
audit) — verificare rapidă arată că e folosit pentru `_probe_google` etc.,
deci risc redus acolo. Restul (16 fișiere) sunt puncte reale de apel LLM,
fiecare cu propriul retry/timeout/error-handling — inconsecvență confirmată
deja parțial în Faza 4 (fallback-uri de model diferite în
`citation_tracker.py` vs restul aplicației, F4-01/F4-02).

**Recomandare Faza 9:** nu de refactorizat acum (schimbare mare, ~16
fișiere) — dar orice bug nou de tip „model retras"/„fallback greșit"
descoperit în viitor ar trebui rezolvat prin migrarea punctului respectiv
către `AsyncLLMClient`, nu prin patch local, pentru a reduce suprafața asta
treptat.

---

## F1-04 — Worktrees stale

```
git worktree list:
  angry-dijkstra          [claude/angry-dijkstra]     — 0 commit-uri față de master
  cranky-edison-f2b5f2    (detached HEAD)             — 0 commit-uri față de master
  dreamy-lewin            [claude/dreamy-lewin]       — 0 commit-uri față de master
  frosty-euler-8c7531     (detached HEAD)             — 0 commit-uri față de master
  heuristic-kepler-6b893b (detached HEAD)             — 0 commit-uri față de master
  quizzical-poincare      [claude/quizzical-poincare] — 0 commit-uri față de master
```

`heuristic-raman` și `magical-brown` există ca directoare pe disc dar **nu
apar deloc** în `git worktree list` — probabil rămase după o ștergere
incompletă (directorul n-a fost curățat, deși worktree-ul a fost eliminat
din git). Interesant: unul din rapoartele generate greșit în trecut
(`flake8_report.txt`, vezi regula 3 din plan) a scanat exact
`.claude/worktrees/angry-dijkstra/api/main.py` — confirmă că aceste
directoare sunt suficient de „reale" încât tool-uri automate le confundă cu
codul principal.

**Recomandare Faza 9:** sigur de rulat `git worktree remove` pe toate cele 6
înregistrate (0 risc de pierdere de muncă, verificat) + ștergere manuală a
celor 2 directoare orfane (`heuristic-raman`, `magical-brown`) după o
verificare vizuală rapidă că nu conțin fișiere modificate necomise
(`git -C .claude/worktrees/heuristic-raman status`, dacă directorul mai are
un `.git`).

---

## F1-09 — Singurul test din proiect: nerulabil din locația lui, apoi 33/34 OK

`test_content_chunker.py` stă la **rădăcina proiectului**, dar face
`from content_chunker import (...)` (fără prefix `core.`) — modulul real e
la `core/content_chunker.py`. Rulat direct sau cu `pytest`, eșuează instant:

```
ModuleNotFoundError: No module named 'content_chunker'
```

Nu există `pytest.ini`/`pyproject.toml`/`conftest.py` care să seteze
`sys.path` — deci testul **n-a mai rulat cu succes din locația curentă**,
probabil de la mutarea lui `content_chunker.py` în `core/`. `pytest` însuși
nici nu e instalat (`pip show pytest` → lipsă).

Am rulat manual, doar pentru verificare (nicio modificare de fișiere),
cu `PYTHONPATH=core`:

```
Ran 34 tests in 0.027s
FAILED (errors=1)
```

**33/34 teste trec.** Singurul eșec:

```python
# test_content_chunker.py:491
self.assertEqual(merged["geo_audit"]["ai_citation_likelihood"], 70)
KeyError: 'ai_citation_likelihood'
```

Verificare suplimentară — **codul de producție e corect, testul e învechit**:
`core/content_chunker.py:804` (`_merge_geo_audit`) folosește deja
`score_fields = [..., "citation_probability", ...]`, care e exact numele de
câmp curent din `prompts/geo_audit.yaml` (verificat în Faza 5). Testul
așteaptă vechiul nume `ai_citation_likelihood`, dinainte de o redenumire a
câmpului în prompt — un semn bun (codul de merge pentru audituri chunked e
sincronizat cu schema curentă), dar testul n-a fost actualizat odată cu
redenumirea.

**Recomandare Faza 9:** (1) mutați fișierul în `core/` sau adăugați un
`conftest.py`/`pyproject.toml` cu `pythonpath = ["core"]`; (2) actualizați
linia 491 la `citation_probability`; (3) instalați `pytest` în
`requirements.txt` (lipsește complet azi) dacă se dorește o suită de teste
reală pornind de aici.

---

## Concluzie Faza 1

Cel mai mare bloc de cod mort e coerent și izolat: un pipeline CLI legacy
întreg (~6.600 linii, F1-01), ușor de arhivat fără risc pentru aplicația
web. Duplicarea reală de logică (F1-02, F1-03) e limitată la utilitare mici
de parsare JSON și instanțiere de clienți LLM — inconsecventă, dar nu
critică imediat, cu excepția impactului deja documentat în Faza 4 pe model
IDs. Curățenia de worktree-uri (F1-04) e o acțiune sigură, cu risc zero,
oricând.
