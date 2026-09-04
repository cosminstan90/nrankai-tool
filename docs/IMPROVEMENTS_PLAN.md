# Plan de îmbunătățiri — nrankai-tool

Document de execuție pentru agenți (Claude Sonnet). Fiecare secțiune e
independentă și poate fi implementată într-o sesiune separată.

Analiza din spate (2026-09-04): inventar complet al tool-ului — 44 de routere,
20 de tipuri de audit, GSC/GA4/Ads, keyword research, tracking de vizibilitate
AI, portfolio multi-client, PDF white-label. Concluzia nu a fost că lipsesc
funcții, ci că tool-ul e **greu pe judecată LLM și ușor pe date dure**.

Din cele 9 lipsuri de mai jos, patru sunt **date pe care nu le ai**, două sunt
**funcții existente pe jumătate**, restul sunt calitate și livrare.

> **Nu adăuga al 21-lea tip de audit.** Ai destule judecăți; îți lipsesc
> măsurătorile. Orice propunere care înseamnă un `.yaml` nou în `prompts/` în
> loc de o sursă de date nouă e aproape sigur greșită.

---

## Reguli obligatorii pentru orice task de aici

1. **Citește `CLAUDE.md` întâi.** Conține convențiile care nu se negociază:
   import-ul limiter-ului din `api/limiter.py`, sesiuni proprii în background
   tasks (nu `Depends(get_db)`), `datetime.now(timezone.utc)`, plasarea
   modelelor pe fișierul de domeniu corect, înregistrarea routerelor.

2. **Poarta de verificare, înainte de orice commit:**
   ```bash
   python -m pytest -q && python3 tests/smoke.py && python3 tests/api_diff.py
   ```
   `smoke.py` și `api_diff.py` cer serverul pornit (`restart_server.bat`).
   `api_diff` compară suprafața API cu baseline-ul — dacă adaugi endpoint-uri,
   creșterea e așteptată, dar trebuie să fie exact cea intenționată.

3. **Baza de date e în mod WAL.** Nu face backup cu copiere de fișier — pierzi
   ce e în `-wal`. Folosește API-ul SQLite:
   ```python
   s = sqlite3.connect(src); d = sqlite3.connect(dst)
   with d: s.backup(d)
   ```
   Backup-urile se pun **în afara repo-ului** (`D:\Projects\_geo_tool_backups\`).
   `api/data/` e în `.gitignore`.

4. **Testele sunt izolate** (`tests/conftest.py` → `GEO_TOOL_DB_PATH` pe un DB
   temporar). Nu scrie teste care presupun date reale în `analyzer.db`.

5. **Migrații:** ultima e `0011`. Următoarea e `0012_*`. Generează cu Alembic,
   nu scrie manual SQL în cod de aplicație.

6. **Apeluri externe** — mereu în `try/except` cu logging, conform `CLAUDE.md`.
   Dacă apelul costă bani, înregistrează costul (vezi `api/routes/costs.py` și
   `record_cost_async`).

---

## 1. Istoric GSC — coloană de dată și acumulare

**Prioritatea 1. Singura unde amânarea costă ceva ce nu se mai recuperează.**

**Executat (2026-09-04).** Implementat **aditiv**, nu ca în planul de mai jos
(coloană de dată direct pe `gsc_page_rows`/`gsc_query_rows`) — verificarea la
implementare a găsit 8 locuri în cod care filtrează acele tabele doar după
`property_id`, câteva cu `func.avg`/`func.sum`/`order_by(clicks.desc())`,
toate presupunând un singur rând per pagină/query. A transforma tabelele alea
într-o serie temporală ar fi stricat tăcut toate agregatele alea. În loc,
două tabele noi, `gsc_page_history`/`gsc_query_history` (migrația `0012`),
acumulează separat; `GscPageRow`/`GscQueryRow` rămân neschimbate.
Detalii complete în mesajul de commit `feat(gsc): add time-series history…`.
Rămas neatins din planul original: pasul 5 (worker de arhivare lunară) —
nu era necesar pentru pornire, poate veni separat.

### Problema

`gsc_page_rows` și `gsc_query_rows` au exact aceste coloane:

```
id, property_id, page|query, clicks, impressions, ctr, position
```

Nicio dată. Iar `api/routes/gsc/properties.py` (funcția `upload_csv`, în jur de
linia 245-266) face `DELETE` pe toate rândurile proprietății, apoi `INSERT` —
deci **fiecare sincronizare suprascrie precedenta**. Ai o singură fotografie
plată per proprietate, nu o serie temporală.

Blochează: content decay (ce pagini pierd trafic), verificarea „a funcționat
optimizarea", comparații an-la-an, sezonalitate.

**De ce e urgent:** API-ul GSC returnează doar ultimele **16 luni**. Ce nu
arhivezi tu dispare definitiv de la Google. Fiecare lună fără coloana asta e
pierdere ireversibilă.

### Implementare

1. Migrație `0012_gsc_time_series.py`:
   - adaugă `date` (DATE) sau `period_start`/`period_end` pe `gsc_page_rows` și
     `gsc_query_rows`
   - adaugă index compus `(property_id, date)` și `(property_id, page, date)` —
     interogările de trend vor filtra mereu pe astea
   - rândurile existente n-au dată: pune-le pe data importului sau marchează-le
     `NULL` și tratează-le ca „snapshot inițial, dată necunoscută". **Nu le
     șterge.**

2. Modele în `api/models/analytics.py` (`GscPageRow`, `GscQueryRow`).

3. `api/routes/gsc/properties.py` și `api/routes/gsc/oauth_sync.py`:
   - **elimină `DELETE`-ul global.** Înlocuiește cu upsert pe cheia
     `(property_id, page|query, date)` — reimportul aceleiași perioade
     actualizează, perioade noi se adaugă.
   - CSV-ul GSC nu conține data; ia-o din formularul de upload (interval
     selectat de utilizator) sau, pentru sync prin API, din intervalul cerut.

4. Sincronizarea prin API trebuie să ceară date **per zi**, nu agregat pe tot
   intervalul — altfel pierzi granularitatea chiar dacă ai coloana.

5. Un worker de arhivare (opțional, dar recomandat): rulează lunar, trage
   ultimele 16 luni pentru fiecare proprietate conectată, umple retroactiv ce
   lipsește. Înregistrează-l în `lifespan()` din `api/main.py`, alături de
   `lead_audit_worker` — vezi pattern-ul de acolo.

### Verificare

- Două import-uri cu date diferite produc rânduri distincte, nu suprascriere.
- Reimportul aceleiași date actualizează, nu dublează.
- Un query de trend pe o pagină întoarce o serie, nu un singur rând.

### Efort
Mediu. Migrația e simplă; partea delicată e upsert-ul și obținerea datei la
import.

---

## 2. Performanță — Core Web Vitals (categorie complet absentă)

**Executat (2026-09-04).** `core/performance_client.py` (CrUX + PSI),
`api/routes/performance.py` (`POST /check`, `GET /history`), model
`PerformanceSnapshot` (migrația `0013`), dated/additiv la fel ca istoricul
GSC. **Nu** integrat în `_COMPOSITE_WEIGHTS`, intenționat — exact avertismentul
de mai jos despre auditurile vechi fără acest pilon. Detalii complete în
mesajul de commit `feat(performance): add Core Web Vitals…`.

**Găsit pe drum, neplănuit:** discul `C:` al mașinii de dezvoltare avea **0
bytes liberi** — cauza unor eșecuri intermitente de test, nimic la codul
scris. `tests/conftest.py` a fost mutat să folosească un director lângă repo
(`.pytest_tmp/`) în loc de `%TEMP%` implicit, ca suita de teste să nu mai
depindă de starea discului de sistem. Nu rezolvă discul plin — doar
independentizează testele de el.

### Problema

Zero. Nu există nici PageSpeed, nici CrUX, nici Lighthouse, niciun LCP/INP/CLS
nicăieri în `api/` sau `core/`. Verificat prin grep pe toate variantele.

E cea mai mare gaură raportat la efort, pentru că **ambele API-uri Google sunt
gratuite**.

### Implementare

1. **CrUX API** (date de teren, utilizatori reali, istoric 25 de săptămâni) —
   sursa principală. Dă LCP/INP/CLS/FCP/TTFB reale, pe origine și pe URL.
2. **PageSpeed Insights v5** (date de laborator + diagnostice) — secundar, util
   pentru „ce anume să repari".

Structura:
- model nou în `api/models/infra.py` (e infrastructură, nu audit): stochează
  măsurătorile **cu dată**, ca să ai trend. Nu repeta greșeala de la punctul 1.
- worker/serviciu în `api/workers/` sau `core/` pentru fetch
- router nou `api/routes/performance.py`, înregistrat conform `CLAUDE.md`
- cheie în `.env`: `PAGESPEED_API_KEY` (CrUX și PSI acceptă aceeași cheie
  Google API)

### Integrarea în scorecard

Ponderile compozite sunt în `api/routes/pages/_shared.py:17`
(`_COMPOSITE_WEIGHTS`) — 18 tipuri de audit, care însumează **1.27**, nu 1.0.

Nu e o eroare și **nu trebuie renormalizate**: `_compute_composite`
(`_shared.py:69`) împarte la suma ponderilor **tipurilor efectiv prezente**
(`weighted_sum / weight_sum`), deci sunt ponderi relative, normalizate dinamic.
Un tip lipsă nu trage scorul în jos, doar iese din calcul. Tipurile
necunoscute primesc implicit 0.02.

Ponderile pot fi și suprascrise per proiect din tabelul `audit_weight_configs`
(vezi `_get_weights`, `_shared.py:55`) — dacă adaugi un pilon, adaugă-l și
acolo ca opțiune.

**Ce contează totuși:** auditele rulate înainte de existența performanței nu au
tipul respectiv, deci compozitul lor e calculat pe altă bază de ponderi.
Comparația „compozit înainte / compozit după" nu e apples-to-apples. De aceea
recomandarea rămâne să afișezi performanța ca pilon **separat**, lângă
compozit, cel puțin până ai suficient istoric cu ea inclusă.

### Verificare
Un URL real întoarce vitals de teren; un URL fără date CrUX suficiente
degradează elegant (CrUX nu are date pentru site-uri cu trafic mic — tratează
cazul, nu presupune că răspunde mereu).

### Efort
Mic-spre-mediu. API-uri gratuite, bine documentate, fără OAuth (doar API key).

---

## 3. Indexare — GSC URL Inspection API

**Executat (2026-09-04).** `api/routes/gsc/url_inspection.py` (`POST /inspect`,
`GET /inspect/quota`, `GET /inspections`), modele `UrlInspection` +
`UrlInspectionQuotaLog` (migrația `0014`), reutilizează OAuth-ul existent din
`_shared.py`. Cotă contorizată separat de rezultate (tabel append-only) —
un recheck forțat pe aceeași pagină în aceeași zi tot consumă o unitate de
cotă, chiar dacă rezultatul se suprascrie (upsert). Refuz la 1900/zi, sub
limita reală de ~2000 a Google, ca marjă de siguranță. Detalii complete în
mesajul de commit `feat(gsc): add URL Inspection API integration…`.

### Problema

Din GSC folosești doar `searchanalytics` și listarea de site-uri (verificat prin
grep pe endpoint-urile apelate). **URL Inspection API nu e atins deloc.**

Deci nu poți răspunde la întrebări elementare de SEO tehnic:
- e pagina indexată sau nu
- ce canonical a ales Google, față de ce ai declarat tu
- e în sitemap dar exclusă, și din ce motiv
- starea de rich results / mobile usability văzută de Google

### Implementare

- OAuth-ul Google e deja configurat (`api/routes/gsc/oauth_sync.py`) — refolosește-l,
  nu construi alt flux de autentificare.
- Endpoint: `urlInspection/index:inspect` pe Search Console API v1.
- **Limita e strictă: ~2000 inspecții/zi/proprietate.** Nu inspecta tot site-ul
  la fiecare audit. Fă-o la cerere, pe paginile dintr-un audit, cu cache pe
  câteva zile și cu contorizare vizibilă a cotei consumate.
- Stochează rezultatul cu dată (același principiu ca la 1 și 2).

### Verificare
O pagină indexată și una neindexată întorc stări diferite; cota se contorizează
corect; depășirea cotei dă eroare tratată, nu 500.

### Efort
Mic. Infrastructura de auth există deja.

---

## 4. Google AI Overviews — măsurare, nu doar sfaturi

### Problema

Contradicția cea mai vizibilă din tool: `api/routes/visibility.py` măsoară real
mențiuni și citări interogând LLM-uri (ChatGPT, Perplexity etc.) — foarte bine.
Dar **AI Overviews de la Google nu e măsurat nicăieri**, deși e cea mai mare
suprafață de căutare AI ca volum.

Motivul e tehnic: AI Overviews se vede din SERP, nu întrebând un LLM. Iar din
DataForSEO folosești **un singur endpoint**: `keywords_data/google_ads` (volum
de căutare). Plătești abonamentul și folosești ~5% din el.

Ai `ai_overview_optimization.yaml` care dă **sfaturi** despre AI Overviews, dar
nimic care să **verifice** dacă apari acolo.

### Implementare

- Folosește `serp/google/organic` din DataForSEO (credențialele există deja:
  `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD`). Răspunsul include blocul AI
  Overview când e prezent, cu sursele citate.
- Model nou cu **dată** — prezența în AI Overviews e volatilă, un singur punct
  nu spune nimic; trendul spune tot.
- Extrage: apare AIO pentru interogare (da/nu), e domeniul citat, pe ce poziție
  în surse, ce alte domenii sunt citate.
- Leagă-l de tracking-ul de vizibilitate existent (`visibility.py`) ca să ai o
  singură imagine „unde apar în AI", nu două feature-uri paralele. **Repo-ul a
  avut deja problema asta** — `citation_tracker.py` și `geo_monitor.py` erau
  aceeași funcție scrisă de două ori, unificate în Etapa 3. Nu o repeta.

### Efort
Mic-spre-mediu. Clientul DataForSEO există; e vorba de endpoint-uri noi.

---

## 5. Crawler care urmărește linkuri

### Problema

`core/web_scraper.py` pornește de la sitemap și atât (`fetch_sitemap_urls`) —
nu urmărește linkuri. Consecința: îți sunt **invizibile** paginile orfane,
linkurile interne rupte, lanțurile de redirect, adâncimea de crawl și orice
pagină care nu e în sitemap.

Ai `prompts/internal_linking.yaml`, dar fără graf de linkuri LLM-ul ghicește de
pe o singură pagină. Un crawl real transformă promptul din opinie în analiză.

E singura lipsă **structurală** care limitează calitatea auditelor existente,
nu doar numărul lor.

### Implementare

- Extinde `core/web_scraper.py` cu un mod de crawl care urmărește `<a href>`,
  cu limite: adâncime maximă, număr maxim de pagini, respectarea `robots.txt`,
  rate limiting.
- Stochează graful (noduri = URL-uri, muchii = linkuri) — modelele merg în
  `api/models/audit.py` sau un fișier de domeniu nou dacă devine mare.
- Derivă: pagini orfane (în sitemap dar fără linkuri interne), 404-uri interne,
  lanțuri de redirect, adâncime per pagină, distribuție de anchor text.
- Alimentează `internal_linking.yaml` cu graful ca fapte deterministe —
  pattern-ul există deja în `core/technical_facts.py`, care injectează fapte
  verificate în prompturi. Urmează-l.

### Atenție
Crawler-ul e partea cu cel mai mare risc de a lovi site-uri reale prea tare.
Rate limiting și `robots.txt` nu sunt opționale. Testează pe un site propriu.

### Efort
Mare. E singurul task din listă care merită împărțit în etape.

---

## 6. Detectarea schimbărilor de conținut

### Problema

Tool-ul n-are memorie în afară de scorurile de audit
(`TrackingProject`/`TrackingSnapshot`). Conținutul paginilor **se salvează** per
audit pe disc (`data/<site>/output_<tip>/`) și `core/scrape_state.py` calculează
deja hash-uri — dar nu există nicăieri „ce s-a schimbat pe pagina asta de la
ultimul audit".

Materia primă există. Lipsește doar funcția. Iar întrebarea e cea mai frecventă
în munca pe client: *clientul a modificat pagina — ce a stricat?*

### Implementare

- Folosește hash-urile existente din `scrape_state.py` ca să detectezi rapid ce
  s-a schimbat între două audituri.
- Pentru paginile schimbate, produ un diff util pentru SEO — nu un diff de text
  brut: title, meta description, H1-H*, count de cuvinte, schema JSON-LD,
  linkuri interne adăugate/scoase, imagini fără alt.
- Suprafață: extinde `api/routes/compare.py` (compară deja audituri) sau un
  endpoint nou dedicat. Verifică întâi ce face `compare.py` — nu duplica.

### Efort
Mic-spre-mediu. Cea mai mare parte din infrastructură există.

---

## 7. Accesibilitate deterministă (axe-core)

### Problema

`prompts/accessibility_audit.yaml` există, dar nu ai **niciun** motor
determinist — zero axe-core, pa11y sau verificare WCAG (verificat prin grep).
Un LLM care citește text nu poate evalua contrast, ARIA, ordinea de focus sau
DOM-ul randat.

E mai rău decât să lipsească: produce output care sună autoritar și pe care
nu-l poate verifica nimeni. Accesibilitatea e cea mai deterministă zonă din tot
ce atinge tool-ul — exact locul unde LLM-ul n-are ce căuta singur.

Notă: `ACCESSIBILITY_AUDIT` are pondere **0.08** în `_COMPOSITE_WEIGHTS` — deci
o judecată neverificabilă influențează scorul compozit al fiecărui client.

### Implementare

- Rulează axe-core pe DOM-ul randat. Scraper-ul are deja fallback pe Selenium
  (`core/web_scraper.py`) — refolosește-l, nu introduce un al doilea browser
  headless.
- Păstrează promptul LLM, dar schimbă-i rolul: **primește rezultatele axe ca
  fapte** și explică/prioritizează, în loc să ghicească. Din nou, pattern-ul din
  `core/technical_facts.py`.

### Efort
Mediu. Partea de infrastructură (browser headless) există.

---

## 8. Restul DataForSEO

### Problema

Legat de punctul 4, dar mai larg. Folosești un singur endpoint din tot
abonamentul. Nefolosite și relevante:

- `serp/google/organic` — poziții reale, tracking de rank, SERP features
- `backlinks/` — profil de linkuri (ai client Ahrefs în
  `api/workers/contentiq/ahrefs.py`, dar Ahrefs e scump; DataForSEO e deja plătit)
- `on_page/` — Lighthouse și parsing de conținut

Tracking-ul existent (`api/routes/tracking.py`) compară **scoruri de audit** în
timp, nu poziții în SERP. Deci nu poți închide bucla „am optimizat → s-a
schimbat clasamentul".

### Implementare
Prioritar `serp/google/organic`, împreună cu punctul 4 (aceleași apeluri pot
servi și AI Overviews, și pozițiile — nu le face în două treceri separate,
plătești de două ori).

### Efort
Mic. Clientul există.

---

## 9. Livrare către client

### Problema

PDF-ul white-label e bun (`api/routes/pdf_reports.py`, `BrandingConfig` în
`api/models/infra.py`), dar e un capăt de drum: fără link live, fără portal,
fără raport programat pe email.

Prioritate mică **atâta timp cât rămâne uz personal**. Dacă tool-ul ajunge să
fie folosit direct de clienți, urcă în listă.

### Implementare
Un link public read-only, cu token, care arată raportul live în loc de un PDF
trimis manual. Atenție la `BasicAuthMiddleware`, care e **global** — orice rută
publică trebuie exceptată explicit, ca `/api/health` și `/static/`. Vezi
`api/middleware/auth.py`.

---

## Ordinea recomandată

| # | Task | Valoare | Efort | De ce în ordinea asta |
|---|------|---------|-------|------------------------|
| 1 | Istoric GSC | Mare | Mediu | Singurul unde amânarea pierde date definitiv |
| 2 | Performanță (CWV) | Mare | Mic | Categorie întreagă lipsă, API-uri gratuite |
| 3 | Indexare (URL Inspection) | Mare | Mic | Auth-ul există, e gratis și nefolosit |
| 4 | AI Overviews + SERP | Mare | Mic-mediu | Contradicția centrală a unui tool GEO |
| 5 | Schimbări de conținut | Medie | Mic-mediu | Infrastructura există deja pe jumătate |
| 6 | Accesibilitate axe-core | Medie | Mediu | Repară o judecată neverificabilă cu pondere 0.08 |
| 7 | Crawler cu linkuri | Mare | Mare | Cel mai mare efort; îmbunătățește auditele existente |
| 8 | Livrare client | Mică | Mediu | Doar dacă iese din uz personal |

Punctele 1-4 sunt toate **surse de date noi** și se pot face independent.
Punctul 7 e singurul care cere planificare separată.

---

## Ce am respins deliberat

**Analiza de log-uri.** Verifici în `robots.txt` că GPTBot *are voie*, dar nu
poți ști dacă GPTBot *chiar a venit* — log-urile ar spune asta. Respinsă pentru
că cere acces la serverul clientului și e efort mare pentru un caz de nișă.

**Orice tip nou de audit LLM.** Vezi nota din capul documentului.
