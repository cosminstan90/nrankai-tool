# Faza 3 — Stratul de date: model vs. migrare vs. DB real

## Rezumat

| ID | Sev | Titlu | Verificat prin |
|----|-----|-------|-----------------|
| F3-01 | **P0** (= F2-03/F2-04, cauza rădăcină) | Drift schemă confirmat **exhaustiv, pe toate cele 80 de tabele model** — exact 2 tabele afectate: `content_briefs` (2 coloane lipsă) și `fanout_sessions` (10 coloane lipsă). **Niciun alt tabel nu are drift.** | Diff programatic `Base.metadata` (80 tabele) vs. `PRAGMA table_info()` pe toate cele 81 de tabele din `api/data/analyzer.db` |
| F3-02 | **P0** | `alembic check` refuză să ruleze: „Target database is not up to date" — DB e la revizia `0005`, head-ul e `0008`. Rularea `alembic upgrade head` ar eșua probabil cu „table already exists", fiindcă tabelele ClusterIQ/SerpIQ (adăugate de migrările 0006-0008) **există deja** în DB — au fost create de `Base.metadata.create_all()`, nu de Alembic | `alembic current` = 0005, `alembic heads` = 0008 (head unic, fără branch), `alembic check` → FAILED |
| F3-03 | **P1** (extinde F2-02) | Bug-ul „naive vs aware datetime" din `portfolio.py:344` **are un al doilea loc identic**, într-un endpoint netestat de Faza 2 (parametrizat) | `api/routes/portfolio.py:609`, în `get_website_details()` (`GET /api/portfolio/website/{domain}`) |
| F3-04 | P2 | **115 coloane** `Column(DateTime, ...)` în tot `api/models/`, **zero** cu `DateTime(timezone=True)` — sursa structurală a F2-02/F3-03. SQLite+SQLAlchemy întorc naiv orice `DateTime` simplu la citire, indiferent cum a fost scris | `grep -c "Column(DateTime"` = 115, `grep -c "Column(DateTime(timezone=True"` = 0 |
| F3-05 | ✅ verificat, fără problemă | Există deja codul CORECT pentru acest bug într-un singur loc — un exemplu de pattern defensiv de urmat peste tot | `api/routes/audits.py:610`: `if started.tzinfo is None: started = started.replace(tzinfo=timezone.utc)` |
| F3-06 | ✅ corectare | `api/models/schemas_clusteriq.py` (untracked, semnalat ca risc P0 în recon-ul inițial) — verificat: **zero importuri în tot proiectul**. E cod orfan, nu un risc de build | `grep -rn "schemas_clusteriq"` pe tot repo-ul → 0 rezultate |
| F3-07 | ✅ verificat, fără problemă | `app/modules/serpiq/models.py` nu duplică modele — e un shim de re-export (`from api.models.serpiq import SiqSnapshot, SiqSerpItem`); sursa reală e `api/models/serpiq.py` + `api/models/clusteriq.py`, ambele deja importate în `api/models/database.py:57,62` | citire cod + verificare că metadata nu câștigă tabele noi la import separat |
| F3-08 | P3 | 3 apariții rămase `datetime.utcnow()` — niciuna nu cauzează bug live (una e comentariu, una e pe modul legacy CLI-only, una e doar formatare de string, nu comparație) | detaliat mai jos |

---

## A. Diff exhaustiv model ↔ DB (F3-01)

Am construit lista completă de tabele din `Base.metadata` (toate modelele
importate prin `api.models.database`, care include și ClusterIQ/SerpIQ) și am
comparat-o coloană-cu-coloană cu schema reală din `api/data/analyzer.db`
(confirmat în Faza 0 ca fiind DB-ul efectiv folosit de aplicație, nu cele 4
fișiere `.db` de 0 bytes de la rădăcină).

```
Tabele în model:  80
Tabele în DB:     81  (+1 = alembic_version, normal)

Tabele în model dar absente din DB:  0
Tabele în DB dar absente din model:  0 (în afară de alembic_version)

Drift de coloane — DOAR 2 tabele afectate din 80:
  content_briefs:   lipsesc  current_score, executive_summary
  fanout_sessions:  lipsesc  query_origin, source_origin, prompt_cluster,
                              run_cost_usd, locale, language, confidence_score,
                              engine, model_version, from_cache
```

**Acestea sunt exact cele 2 tabele găsite accidental de smoke test-ul din
Faza 2** (F2-03, F2-04) — diff-ul exhaustiv confirmă că **nu există alt
drift ascuns** în restul celor 78 de tabele. Cauza rădăcină a fost deja
documentată complet în `02-routes.md` (F2-05): funcția de auto-migrare
pentru `fanout_sessions` există (`init_db_async()`) dar nu e apelată
niciodată; pentru `content_briefs.current_score`/`executive_summary` nu
există nicio cale de migrare, nici Alembic, nici ad-hoc.

---

## B. Alembic — stare inconsistentă (F3-02)

```bash
$ alembic current
0005

$ alembic heads
0008 (head)

$ alembic check
ERROR [alembic.util.messaging] Target database is not up to date.
FAILED: Target database is not up to date.
```

**Explicația combinată cu secțiunea A e importantă:** deși `alembic_version`
arată `0005`, toate cele 80 de tabele din model — inclusiv cele adăugate de
migrările `0006` (ClusterIQ), `0007` (ClusterIQ competitor) și `0008`
(SerpIQ) — **există deja** în DB. Asta înseamnă că tabelele au fost create
de `Base.metadata.create_all()` (apelat în `init_db()` la fiecare pornire a
serverului — vezi `api/models/database.py:149`), nu prin rularea efectivă a
migrărilor Alembic. `create_all()` creează orice tabel lipsă pe baza
modelelor, indiferent de ce știe Alembic — de-asta proiectul „funcționează"
deși e la revizia 0005.

**Risc real:** dacă cineva rulează azi `alembic upgrade head` (de exemplu pe
un deploy nou, sau crezând că repară drift-ul din secțiunea A), comanda va
încerca să creeze din nou tabelele ClusterIQ/SerpIQ din migrările 0006-0008
— **foarte probabil eșec cu „table already exists"**, blocând complet
pipeline-ul de migrare. Nu am rulat efectiv comanda (ar fi o modificare, nu
doar verificare) — marcat `NEEDS-RUNTIME-CHECK` pentru confirmarea exactă a
erorii, dar mecanismul e clar din felul în care Alembic funcționează.

**Recomandare Faza 9:** înainte de orice `alembic upgrade`, sincronizați
manual `alembic_version` la `0008` (`alembic stamp head`) — tabelele deja
există, deci „stamp" fără a re-rula DDL e corect aici — apoi scrieți o
migrare nouă (`0009`) care adaugă explicit cele 12 coloane lipsă găsite în
secțiunea A. Asta rezolvă simultan F3-01 și F3-02.

---

## C. Bug-ul datetime naiv/aware — extindere față de Faza 2 (F3-03, F3-04)

Faza 2 a găsit un singur loc rupt (`portfolio.py:344`). Verificare mai largă
arată acest tipar structural:

```bash
grep -c "Column(DateTime" api/models/*.py           # → 115
grep -c "Column(DateTime(timezone=True" api/models/*.py   # → 0
```

**Toate cele 115 coloane de tip dată/timp din întreaga schemă sunt `DateTime`
simplu**, niciuna `DateTime(timezone=True)`. Cu backend-ul SQLite (folosit de
acest proiect), SQLAlchemy întoarce întotdeauna un `datetime` **naiv** la
citire pentru coloane `DateTime` simplu — indiferent dacă valoarea a fost
scrisă cu `datetime.now(timezone.utc)` (aware) sau nu. Orice cod care scade
direct `datetime.now(timezone.utc) - <valoare_citită_din_DB>` va crăpa cu
`TypeError`, exact ca la F2-02.

**Al doilea loc confirmat, netestat de Faza 2:**

```python
# api/routes/portfolio.py:608-609, în get_website_details()
last_audit = datetime.fromisoformat(audits["last_audit_date"])
days_since = (datetime.now(timezone.utc) - last_audit).days
```

Acesta e în `GET /api/portfolio/website/{domain}` — un endpoint cu parametru
de rută, exclus din smoke test-ul generic al Fazei 2 (care a testat doar
GET-uri fără parametri). **Al doilea endpoint din `portfolio.py` care crapă
100% din timp** cât timp domeniul respectiv are cel puțin un audit — de
verificat live cu un domeniu real din DB (`NEEDS-RUNTIME-CHECK` pentru
confirmarea finală, dar codul e identic cu bug-ul deja confirmat la linia 344).

**Exemplu de fix corect deja prezent în codebase** — merită folosit ca
model pentru reparare, nu inventat de la zero:

```python
# api/routes/audits.py:609-612 — pattern corect
if audit.started_at:
    started = audit.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - started
```

**Recomandare Faza 9:** aplicați acest pattern (`if x.tzinfo is None: x =
x.replace(tzinfo=timezone.utc)`) la ambele locuri din `portfolio.py` (liniile
344 și 609). Pe termen mediu, o migrare care schimbă toate cele 115 coloane
la `DateTime(timezone=True)` ar elimina clasa de bug definitiv, dar e o
schimbare mult mai mare — necesită plan separat (afectează 80 de tabele).

### F3-08 — celelalte 3 `datetime.utcnow()`

```
api/routes/gsc/_shared.py:98   → comentariu, nu cod executabil
core/history_tracker.py:393    → modul legacy CLI-only (vezi Faza 5) — impact redus
app/modules/clusteriq/router.py:771 → doar .strftime() pentru afișare text, nu comparație — fără risc
```
Niciuna nu cauzează bug live; nu necesită intervenție urgentă.

---

## D. Clarificări asupra fișierelor suspectate inițial

### F3-06 — `api/models/schemas_clusteriq.py` (untracked)

Recon-ul inițial (înainte de plan) semnalase acest fișier drept risc P0
(„dacă se importă, build-ul e rupt pe altă mașină"). Verificare directă:

```bash
grep -rn "schemas_clusteriq" . --exclude-dir=.claude
# → 0 rezultate în TOT proiectul
```

Fișierul conține scheme Pydantic pentru ClusterIQ (`CluProjectCreate`, etc.,
205 linii) dar **nu e importat de nimic, nicăieri**. E cod orfan (probabil
un draft anterior, înlocuit de `app/modules/clusteriq/router.py` care își
definește propriile scheme inline sau în alt fișier) — risc real: **P2**
(fișier mort, nu tracked în git), nu P0. Sigur de șters sau de adăugat în
git dacă cineva intenționează să-l folosească — de clarificat cu Cosmin în
Faza 9.

### F3-07 — `app/modules/serpiq/models.py` nu duplică ORM-ul

```python
# app/modules/serpiq/models.py:8
from api.models.serpiq import SiqSnapshot, SiqSerpItem  # noqa: F401
```

E un shim de compatibilitate — sursa reală a modelelor SerpIQ e
`api/models/serpiq.py`, deja importată corect în `api/models/database.py:62`.
Verificat că importarea separată a acestui shim nu adaugă tabele noi la
`Base.metadata` (ar fi însemnat un `Base` separat / metadata duplicată —
nu e cazul). **Arhitectura e corectă aici**, contrar suspiciunii inițiale
de „două arhitecturi paralele care se duplică" — cel puțin la nivel de
model de date, e un singur set de adevăr.

---

## Concluzie Faza 3

Stratul de date are o singură problemă structurală reală, dar **una foarte
concretă și cu impact deja confirmat live**: drift de schemă izolat la exact
2 tabele (cauzat de un bug de proces — funcție de auto-migrare nefolosită —
documentat complet în Faza 2), plus o stare Alembic „falsificată" de
`create_all()` care ar sabota orice încercare viitoare de `alembic upgrade`.
Bug-ul de timezone are acum 2 exemple confirmate în cod și un exemplu de
fix corect deja existent în același proiect. Restul arhitecturii de date
(80/80 tabele, fără duplicare reală de modele) e sănătos.
