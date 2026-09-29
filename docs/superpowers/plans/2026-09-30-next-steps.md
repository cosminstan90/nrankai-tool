# Pașii următori — 2026-09-30

Document de execuție pentru Claude Sonnet. Pașii sunt în ordinea priorității;
fă **un pas per sesiune, un commit per pas**. Nu trece la pasul următor până
nu trece poarta de verificare.

Dependențe: 2 → 10, 13, 15 (istoric GSC) · 8 → 16 · 13 → 14 · 17 construiește
embeddings-urile folosite și de 14 · 9 și 10 → 18. Ordinea recomandată pentru
partea a doua: 12, 13, 14, 15, 16, 17, 18.

Context: `docs/IMPROVEMENTS_PLAN.md` e executat în afară de punctul 9 (livrare
client, amânat — uz personal) și de `backlinks/`/`on_page/` din punctul 8
(respinse: fără abonament lunar; `on_page` ar dubla axe-core + CWV + Screaming
Frog). Ce urmează mai jos a fost găsit verificând starea reală, nu din plan.

---

## Reguli pentru fiecare pas

1. Citește `CLAUDE.md` întâi. Convențiile de acolo nu se negociază.
2. Poarta de verificare, înainte de commit:
   ```bash
   python -m pytest -q && python3 tests/smoke.py && python3 tests/api_diff.py
   ```
   `smoke.py`/`api_diff.py` cer serverul pornit — folosește
   `restart_server.bat` (scrie în `uvicorn.log`, pe care îl vei citi la pasul 3).
   Dacă `api_diff` arată o schimbare, trebuie să fie **exact** cea intenționată;
   abia atunci `python3 tests/api_diff.py --update`.
3. Nu afișa niciodată valori din `.env` — doar dacă o cheie e setată și ce lungime are.
4. `analyzer.db` e în mod WAL: backup doar prin `sqlite3` `.backup()`, în
   `D:\Projects\_geo_tool_backups\`, niciodată copiere de fișier. Fă backup
   înainte de orice pas care scrie în DB-ul real.
5. **Fiecare sesiune are acum conexiunea ei SQLite** (vezi `api/models/_base.py`).
   SQLite are un singur scriitor: nu ține o tranzacție de scriere deschisă peste
   un apel lent (LLM, HTTP, crawl). Commit înainte de munca lentă.
6. Lipsă ≠ zero. O dată care n-a fost măsurată se raportează ca nemăsurată,
   niciodată ca 0.
7. Nu modifica `prompts/`, `api/prompts/`, `api/middleware/auth.py`, și nu scrie
   migrații de mână (`python -m alembic revision`).

---

## Pasul 0 — acțiuni ale utilizatorului (nu Sonnet)

Sonnet nu poate face astea; ele deblochează pașii 1-2 și datele reale:

- **Reconectează Google OAuth** la `/gsc` („Connect Google"). Tabela
  `google_oauth_tokens` e goală; ultimul sync GSC (conso.ro) a fost 2026-03-19.
- Înlocuiește `PERPLEXITY_API_KEY` (cheia actuală e respinsă cu 401).
- Opțional: reîncarcă DataForSEO (sold ~$0.89) dacă vrei poziții SERP / AI Overviews.

---

## Pasul 1 — Repară ruta de sync GSC (bug, 15 minute)

**Problema, verificată live:** în `api/routes/gsc/oauth_sync.py`, commit-ul
`c8f72b4` a inserat `upsert_gsc_history` între decoratorul
`@router.post("/properties/{property_id}/sync")` și `sync_property`. Decoratorul
s-a lipit de funcția greșită:

- butonul Sync din `api/templates/gsc.html` (liniile ~367 și ~390) primește
  **422** din 2026-09-04 — cere `db_path`, `pid`, `q_daily`, `p_daily`;
- ruta expune un parametru `db_path` controlat de apelant, care alege în ce
  fișier SQLite se scrie.

**De făcut:**
1. Mută decoratorul pe `async def sync_property(...)`. `upsert_gsc_history`
   rămâne funcție simplă la nivel de modul (e testată direct, vezi docstring-ul ei).
2. Test nou care prinde regresia: caută în `app.routes` ruta
   `POST /api/gsc/properties/{property_id}/sync` și verifică
   `route.endpoint is sync_property`; verifică și că nicio rută nu are ca
   endpoint `upsert_gsc_history`.
3. `api_diff` va arăta schimbarea parametrilor acelei operații — e intenționată.
   Verifică să fie singura, apoi `--update`.

**Gata când:** testul pică pe codul vechi și trece pe cel nou;
`POST /api/gsc/properties/<id>/sync` fără body nu mai dă 422 (fără OAuth
conectat, răspunsul corect e 401 „Google account not connected").

---

## Pasul 2 — Worker de arhivare GSC (punctul 1, pasul 5 din IMPROVEMENTS_PLAN)

**De ce e prioritar:** `gsc_page_history` și `gsc_query_history` au **0 rânduri**
în DB-ul real. Google păstrează 16 luni; în fiecare lună care trece, o lună de
date se pierde definitiv. Tabelele și upsert-ul există, nimeni nu le umple.

**De făcut:**
1. Citește `sync_property` și blocul „Daily-granularity fetch" din
   `oauth_sync.py` (~linia 301): acolo e deja cererea per zi și paginarea.
   **Reutilizează-le**, nu scrie un al doilea client GSC. Dacă trebuie, extrage
   partea de fetch într-o funcție pe care o apelează și `sync_property`, și worker-ul.
2. `api/workers/gsc_archive_worker.py`:
   - pentru fiecare `GscProperty` cu `sync_type == "api"`: determină ce zile
     lipsesc din istoric în fereastra ultimelor 16 luni și le cere în
     bucăți (de ex. câte o lună), scriind prin `upsert_gsc_history` (idempotent);
   - fără credențiale OAuth: log `warning` o dată per rulare și ieși — nu
     arunca, nu raporta „0 rânduri" ca succes;
   - GSC are întârziere de ~2-3 zile: nu cere ultimele 3 zile;
   - rulează la pornire (după o întârziere scurtă) și apoi o dată pe zi.
3. Înregistrează-l în `lifespan()` din `api/main.py`, pe modelul
   `lead_audit_worker_loop` (liniile ~235-247: `create_task` + `cancel` la oprire).
   Dezactivabil cu `GSC_ARCHIVE_ENABLED=0`; implicit pornit.
4. Stare vizibilă: expune ultima rulare (când, câte zile scrise, eroare dacă a
   fost) — cel mai simplu, un câmp în răspunsul existent `GET /api/gsc/oauth/status`,
   ca să nu crești suprafața API fără motiv.
5. Teste cu clientul Google mock-uit: (a) umple doar zilele lipsă;
   (b) a doua rulare nu dublează rânduri; (c) fără OAuth nu scrie nimic și nu aruncă.

**Verificare live** (doar după pasul 0): backup, repornește serverul, așteaptă
prima rulare, apoi:
```sql
SELECT count(*), min(period_start), max(period_end) FROM gsc_page_history;
```
`min(period_start)` trebuie să fie ~16 luni în urmă. Actualizează secțiunea 1
din `docs/IMPROVEMENTS_PLAN.md` („Rămas neatins: pasul 5" → executat).

---

## Pasul 3 — Detector pentru tranzacții de scriere lungi

**De ce:** după eliminarea `StaticPool` (commit `ba88a10`), o sesiune care ține
o scriere deschisă peste un apel lent blochează celelalte scrieri și, după 30s,
le face să pice cu „database is locked". Fluxurile principale au fost verificate
(scanarea de vizibilitate, `fanout_tracker`, `audit_worker`), dar nu toate cele
47 de fișiere care deschid sesiuni.

**De făcut:**
1. În `api/models/_base.py`, listener-e pe `engine.sync_engine`:
   `before_cursor_execute` marchează în `conn.info` momentul primului
   INSERT/UPDATE/DELETE din tranzacție (și primele ~120 de caractere ale
   statement-ului); `commit` și `rollback` calculează durata și, peste
   `WRITE_TXN_WARN_S = 5`, loghează `warning` cu durata și statement-ul; apoi curăță.
2. Test: cu pragul redus la 0.1s, o sesiune care face flush, doarme 0.3s și
   commit produce warning-ul (`assertLogs`); una rapidă nu.
3. Live: pornește cu `restart_server.bat`, rulează un audit mic, o scanare de
   vizibilitate și o captură de snapshot, apoi caută warning-ul în `uvicorn.log`.
   Fiecare apariție: repară sesiunea (commit înainte de munca lentă sau sesiune
   scurtă separată), cu test.

**Gata când:** zero warning-uri pe fluxurile de mai sus, sau fiecare rămas e
explicat în mesajul de commit.

---

## Pasul 4 — Actualizează `docs/IMPROVEMENTS_PLAN.md`

Documentul e citit de agenți ca sursă de adevăr; câteva afirmații sunt depășite:
- Regula 5: „ultima e `0011`" — ultima e acum `0019` (verifică cu
  `ls migrations/versions`). Mai bine: scrie „vezi `migrations/versions/`" în loc
  de un număr care se învechește iar.
- Regula 3: adaugă nota despre conexiune per sesiune și commit înainte de
  munca lentă (regula 5 de aici).
- Punctul 8: `backlinks/` și `on_page/` — **decis: nu** (fără abonament lunar;
  `on_page` dublează axe-core + CWV + Screaming Frog). Nu „nefolosite încă".
- Tabelul „Ordinea recomandată": marchează ce e executat.

Doar documentație; poarta de verificare tot se rulează.

---

## Pasul 5 (opțional) — Deprecation `TemplateResponse`

Suita de teste afișează 33 de warning-uri, majoritatea de la
`TemplateResponse(name, {"request": request, ...})`. Sunt 56 de apeluri în 13
fișiere din `api/`. Forma nouă: `TemplateResponse(request, name, {...})`
(`request` poate rămâne și în context, e inofensiv).

Mecanic, dar atinge fiecare pagină HTML: după schimbare, `smoke.py` trebuie să
dea aceleași coduri ca înainte, iar numărul de warning-uri din `pytest` să scadă.
Dacă vreun apel pasează `status_code`/`headers` pozițional, verifică-l separat.

---

## Pasul 6 — Backup automat al `analyzer.db`

**De ce:** baza (48 MB) nu are backup automat; ultimul din
`D:\Projects\_geo_tool_backups\` e din 2026-09-04. De acum conține serii
temporale care nu se pot reconstrui: istoric GSC, snapshot-uri, poziții SERP.

**De făcut:**
1. `scripts/backup_db.py`: backup prin `sqlite3` `.backup()` (niciodată copiere de
   fișier — WAL), în `D:\Projects\_geo_tool_backups\daily\analyzer_YYYYMMDD.db`.
2. Verifică fiecare backup după scriere: `PRAGMA integrity_check` == `ok` și
   `SELECT count(*) FROM audits` > 0. Un backup neverificat nu contează.
3. Rotație: păstrează 7 zilnice și 4 săptămânale (cel de duminică). Șterge
   **doar** fișiere care se potrivesc cu tiparul de nume, nimic altceva din director.
4. Programare: **nu** crea tu task-ul Windows (e configurare persistentă a
   sistemului). Dă utilizatorului comanda `schtasks` exactă, cu ora propusă.
5. Copie în afara mașinii: întreabă utilizatorul unde (OneDrive, NAS etc.);
   destinația vine opțional din `GEO_TOOL_BACKUP_OFFSITE`.
6. Scrie rezultatul ultimei rulări (când, mărime, verificare ok) într-un JSON
   lângă backup-uri — pasul 7 îl citește.
7. Teste pe DB-ul temporar: backup-ul se deschide și are aceleași rânduri;
   rotația păstrează exact 7+4 și nu atinge fișiere străine.

---

## Pasul 7 — Panou de sănătate și alertă

**De ce:** aproape tot ce s-a stricat în ultimele luni s-a stricat fără să spună
nimic: Claude a eșuat toate scanările din februarie–martie, Perplexity dă 401,
OAuth e deconectat din martie, sync-ul GSC a dat 422 patru săptămâni, creditul
DataForSEO s-a terminat, discul C: a ajuns la 0 bytes.

**De făcut:**
1. Tabel nou `worker_runs` în `api/models/infra.py` (migrație Alembic):
   `worker`, `started_at`, `finished_at`, `ok`, `detail`. Fiecare worker (audit,
   lead, fanout tracker, scheduler, arhivă GSC, snapshot) scrie o linie la final,
   într-o sesiune scurtă proprie.
2. `api/routes/status.py` — `GET /api/status` + pagina `/status`. **Nu atinge
   `/api/health`** (e exceptat din auth și folosit extern). Verificări:
   - chei provider: apel minim **gratuit** unde există (listare modele la
     OpenAI/Anthropic/Gemini/Mistral); la Perplexity un apel cu `max_tokens=1`,
     cu costul înregistrat prin `record_cost_async`;
   - DataForSEO: sold prin `GET /v3/appendix/user_data` (gratuit); prag de
     avertizare configurabil, implicit $5;
   - Google OAuth: există token și reîmprospătarea reușește;
   - ultima rulare reușită per worker, cu prag de vechime per worker;
   - backup: JSON-ul de la pasul 6;
   - spațiu liber pe discul DB-ului și pe `C:` (`shutil.disk_usage`).
   Fiecare verificare întoarce `ok` / `warn` / `fail` / `unknown` — `unknown` când
   verificarea n-a putut rula (lipsă ≠ ok).
3. Worker zilnic care rulează verificările și trimite pe `N8N_WEBHOOK_URL`
   **doar la schimbare de stare** (ok→fail, fail→ok).
4. Teste cu apelurile externe mock-uite: fiecare stare, plus „webhook doar la tranziție".

---

## Pasul 8 — Păstrează tot SERP-ul plătit, nu doar poziția noastră

**De ce:** `serp_rank_observations` salvează rank-ul site-ului urmărit și un
`aio_cites_site` boolean. Restul rezultatelor organice (concurenții) și sursele
din AI Overview sunt plătite și aruncate — tiparul criticat la punctul 4 din
IMPROVEMENTS_PLAN.

**De făcut:**
1. Două tabele noi în `api/models/content.py` (migrație Alembic), legate de
   `serp_rank_observations` cu `ON DELETE CASCADE`:
   - `serp_organic_results`: `rank_group`, `rank_absolute`, `domain`, `url`, `title`;
   - `serp_aio_references`: `position`, `domain`, `url`, `title`.
2. Scrie-le în `record_observation` (`api/workers/rank_tracking.py`), în aceeași
   sesiune scurtă. `SerpResult` are deja datele (`organic`, `ai_overview["references"]`).
3. `GET /api/citations/trackers/{id}/competitors` — per query, domeniile din top 10
   în timp și domeniile citate de AI Overview în timp.
4. Teste: 20 de rezultate + 5 referințe AIO salvează 25 de rânduri; ștergerea
   trackerului le șterge.

Nu costă nimic în plus: datele vin deja în același apel.

---

## Pasul 9 — Vizibilitate AI cu eșantioane multiple și interval de încredere

**De ce:** fiecare scanare întreabă fiecare model **o dată** per query; trackerul
ING are 5 query-uri. Răspunsurile LLM variază între rulări, deci 1/5 vs 2/5 e în
mare parte zgomot.

**De făcut:**
1. `samples_per_query` în `providers_config` (implicit 3, maxim 10). Costul crește
   liniar — afișează costul estimat în UI înainte de scanare.
2. În `_run_visibility_scan`, N apeluri per query/provider; fiecare eșantion
   păstrat în `results_json`. Eșantioanele eșuate sunt `failed` și **excluse** din
   numitor (regula deja aplicată prin `_provider_point`).
3. Rata = citări / răspunsuri valide, cu interval Wilson 95% (`ci_low`/`ci_high`
   în `provider_breakdown`).
4. Graficele desenează banda; o schimbare între scanări e marcată ca reală doar
   dacă intervalele nu se suprapun.
5. Scanările vechi (un eșantion) rămân valide — interval larg, nu eroare.
6. Teste: Wilson pe cazuri cunoscute (0/3, 3/3, 5/10); eșecurile nu intră în
   numitor; scanările vechi se citesc fără excepții.

---

## Pasul 10 — Cronologie per URL: ce s-a schimbat și ce s-a mișcat după

**De ce:** piesele datate există separat — schimbări de pagină (`page_snapshots`
+ `core/page_diff.py`), istoric GSC (pasul 2), poziții SERP
(`serp_rank_observations.ranking_url`), citări AI. Nimic nu le pune pe aceeași axă.

**De făcut:**
1. `GET /api/timeline?url=...`: evenimente de schimbare din diff-uri + serii
   zilnice GSC + observații SERP unde `ranking_url` e URL-ul + citări AI.
   Normalizează URL-urile la fel peste tot (slash final, www, parametri).
2. Per schimbare: medii pe 28 de zile înainte și după, **cu numărul de zile cu
   date** în fiecare fereastră. Sub 14 zile: „date insuficiente", nu un procent.
3. UI: grafic cu schimbările ca marcaje verticale. Formulare „după", niciodată
   „din cauza" — sezonalitatea și update-urile Google nu sunt controlate.
4. Teste pe date sintetice: ferestre corecte; goluri → „insuficient"; un URL fără
   GSC arată doar ce are.

Depinde de pasul 2.

---

## Pasul 11 — Set de evaluare pentru calitatea auditurilor

**De ce:** orice schimbare de prompt schimbă toate auditurile, dar nimic nu arată
dacă a fost în bine. Asta blochează orice îmbunătățire a prompturilor.

**De făcut:**
1. `tests/eval/` cu 5-10 pagini salvate (HTML + text + sidecar-e `.axe.json` /
   `.head.json`), fiecare cu probleme **cunoscute** în `expected.yaml`: contrastul
   `#ff6200` din fixture-ul axe ING, H1 lipsă, canonical greșit, schema invalidă.
2. `scripts/run_eval.py --audit-type X --model Y`: rulează `DirectAnalyzer` pe
   fixture-uri, verifică ce probleme așteptate apar în output (cuvinte-cheie din
   `expected.yaml`), raportează recall per tip de audit și costul.
3. **Nu intră în `pytest` implicit** — costă bani. Rezultatele în
   `tests/eval/results/` cu data, modelul și hash-ul promptului.
4. **Nu modifica niciun prompt în acest pas.** Scopul e o linie de bază.

---

# Partea a doua — rezultate SEO/GEO mai bune

Pașii 1-11 fac tool-ul fiabil și măsurat. Pașii 12-18 îl fac să spună **ce să
schimbi, pe ce pagină, cu ce câștig**, pe baza datelor măsurate, nu a sfaturilor
generice. Regula din IMPROVEMENTS_PLAN rămâne: **nicio recomandare nouă nu vine
dintr-un prompt nou în loc de o măsurătoare.** Unde e nevoie de LLM, el
formulează pe baza faptelor măsurate; nu le inventează.

Fiecare pas produce o listă de recomandări per URL. Toate intră, la pasul 18,
în același flux de acțiuni.

---

## Pasul 12 — Conținut invizibil pentru crawlerele AI (fără JavaScript)

**De ce:** GPTBot, ClaudeBot și PerplexityBot în mare parte nu execută JavaScript.
Tool-ul verifică dacă au voie (`robots.txt`), nu dacă **văd** conținutul. O pagină
al cărei text principal apare doar după JS e practic absentă din AI, oricât de
bine ar fi scrisă. Verifică afirmația despre boți în documentația publică a
fiecăruia înainte să o pui în UI, și citeaz-o acolo.

**De făcut:**
1. `core/js_visibility.py`: pentru un URL, (a) HTML brut prin `httpx` cu
   User-Agent-ul GPTBot (și, separat, unul obișnuit — unele site-uri servesc
   altceva boților), (b) HTML-ul randat pe care `core/web_scraper.py` îl salvează
   deja (`<stem>.html`). Extrage textul din ambele cu `extract_content` din
   `core/html2llm_converter.py`, ca să compari la fel.
2. Metrici per pagină: cuvinte brut vs randat, raport, și **ce lipsește**: H1,
   primul paragraf de conținut, prețuri/cifre, FAQ, JSON-LD — fiecare prezent
   în randat dar absent în brut. Status HTTP diferit pentru UA-ul de bot
   (403/challenge) e o constatare separată, mai gravă.
3. Sidecar `<stem>.rawtext.json` lângă HTML, pe modelul `.axe.json` /
   `.head.json`, ca auditurile să-l poată citi fără a re-descărca.
4. Injectează faptele în auditurile GEO (`GEO_AUDIT` și ce alte tipuri GEO
   există — verifică în `api/prompts/`) prin același mecanism ca faptele axe în
   `core/direct_analyzer.py`: bloc de fapte înainte de chunking, doar pentru
   tipurile respective. **Nu modifica prompturile.** Pagină nemăsurată → blocul
   spune „nemăsurat", nu „vizibil".
5. Endpoint + tabel în UI: paginile sortate după cât conținut pierd fără JS.
6. Teste cu fixture-uri: o pagină SSR (brut ≈ randat), o pagină SPA (brut
   aproape gol), o pagină care dă 403 pe UA de bot. Plus testul că faptele
   ajung la model (modelul `tests/test_axe_injection.py`).

**Verificare live:** ing.ro, câteva pagini. Raportează cifrele reale, oricare
ar fi — inclusiv „nu e nicio problemă".

---

## Pasul 13 — Oportunități din GSC: striking distance și CTR slab

**De ce:** cele mai rapide câștiguri SEO sunt pagini aproape de top și titluri
care nu atrag click-uri. Datele sunt în GSC; nu există nicio analiză de acest tip.

**De făcut** (depinde de pasul 2):
1. `core/gsc_opportunities.py`, pe `gsc_page_history` / `gsc_query_history`,
   ultimele 28 de zile:
   - **striking distance:** perechi pagină+query cu poziție medie 4–15 și
     impresii peste un prag (implicit 100 / 28 zile);
   - **CTR slab:** CTR-ul paginii sub curba așteptată pentru poziția ei.
     Curba se calculează **din datele proprietății** (mediana CTR pe fiecare
     poziție rotunjită), nu dintr-o curbă de pe internet — diferă mult pe nișă și
     pe prezența AI Overviews.
2. Câștig estimat per oportunitate: impresii × (CTR la poziția țintă − CTR
   actual), cu presupunerea scrisă explicit lângă cifră.
3. Exclude query-urile de brand (configurabil) — CTR-ul lor distorsionează tot.
4. Endpoint + tabel sortat după câștig estimat. Pentru CTR slab, un buton care
   folosește optimizer-ul existent (`api/routes/gsc/optimizer.py`) să propună
   titlu/meta, cu query-urile reale ale paginii ca input.
5. Teste pe date sintetice: curba, pragurile, excluderea brandului, pagină fără
   destule date → exclusă, nu scor 0.

---

## Pasul 14 — Sugestii de linkuri interne

**De ce:** graful de linkuri există (`crawl_pages`: `inlinks_total`,
`content_inlinks`, `is_orphan`; `crawl_links`: `source_url`, `dest_url`,
`anchor`), iar oportunitățile de la pasul 13 spun ce pagini merită împinse.
Nimic nu le leagă.

**De făcut:**
1. Ținte: pagini din pasul 13 (sau cu impresii mari) cu puține `content_inlinks`
   (linkurile din navigație nu contează aici), plus paginile `is_orphan`.
2. Surse candidate: pagini indexabile din același crawl, apropiate tematic de
   țintă. Apropierea tematică: embeddings pe textul paginilor (vezi pasul 17 —
   **construiește infrastructura de embeddings o singură dată**, folosită de
   ambii pași; dacă faci 14 înaintea lui 17, pune-o aici). Exclude sursele care
   au deja link spre țintă.
3. Ancoră propusă: query-urile GSC reale ale țintei (cu impresii), nu text inventat.
4. Rezultat: „adaugă link din A spre B cu ancora «X»", cu motivul (impresii,
   poziție, câte linkuri de conținut are acum B).
5. Teste pe un crawl sintetic: nu propune link existent, nu propune surse
   non-indexabile sau 4xx, țintele orfane apar primele.

---

## Pasul 15 — Decay de conținut

**De ce:** paginile care pierd trafic treptat sunt cel mai ieftin loc de
recuperat trafic; verdictul UPDATE din ContentIQ
(`api/workers/contentiq/verdict.py`) nu folosește nicio tendință reală.

**De făcut** (depinde de pasul 2 și de cel puțin ~4 luni de istoric — spune
explicit în UI când istoricul e prea scurt):
1. Per pagină, clicks săptămânale; decay = pantă negativă consistentă pe 12+
   săptămâni **și** scădere față de vârful propriu peste un prag (implicit 30%).
2. Separă decay-ul de sezonalitate: unde există 12+ luni de istoric, compară cu
   aceeași perioadă de anul trecut; unde nu, marchează „sezonalitate necontrolată".
3. Separă cauza, unde datele o arată: poziție scăzută (competiție/relevanță) vs
   poziție stabilă cu CTR scăzut (SERP schimbat, de ex. AI Overview nou — vezi
   `serp_features` din pasul 8).
4. Transmite semnalul în ContentIQ ca metrică măsurată; lipsa istoricului rămâne
   `None`, nu „fără decay".
5. Teste pe serii sintetice: decay liniar, sezonalitate pură (nu e decay),
   istoric prea scurt.

---

## Pasul 16 — De ce e citat altcineva: comparație cu paginile citate

**De ce:** sfatul „optimizează pentru AI" e generic. Paginile efectiv citate
pentru un query pot fi măsurate și comparate cu pagina ta.

**De făcut** (depinde de pasul 8):
1. Pentru fiecare query urmărit: URL-urile citate de AI Overview
   (`serp_aio_references`) și de LLM-uri (sursele deja salvate în scanări).
   Descarcă-le (cache pe disc, respectă `robots.txt`), plus pagina ta cea mai
   bine clasată pentru query.
2. Trăsături deterministe per pagină, în `core/citation_features.py`:
   - răspuns direct: query-ul (sau entitatea lui) apare în primele ~100 de
     cuvinte ale conținutului principal;
   - tabele, liste, perechi întrebare/răspuns;
   - densitate de cifre concrete (sume, procente, date);
   - dată vizibilă de publicare/actualizare și cât de recentă e;
   - JSON-LD (tipuri), autor, lungime, structura de headings.
3. Raport per query: „N din M pagini citate au X; a ta nu" — doar trăsăturile
   unde diferența e clară. Cu M mic (sub 3), spune că eșantionul e mic.
4. Opțional, un LLM formulează recomandarea, primind **doar** tabelul de
   trăsături măsurate. Nu îi da paginile întregi să „judece".
5. Teste pe HTML-uri fixture pentru fiecare trăsătură; raportul nu afirmă
   diferențe pe trăsături nemăsurate.

---

## Pasul 17 — Sub-query-uri Fan-Out fără pagină pe site

**De ce:** Fan-Out știe ce sub-întrebări pun motoarele AI (`fanout_queries`), dar
raportul de acoperire (`GET /api/fanout/sessions/{id}/coverage`) spune doar ce
procent din surse e domeniul tău. Nu spune **la ce sub-întrebări nu ai răspuns**.

**De făcut:**
1. Infrastructură de embeddings (o singură dată, refolosită la pasul 14):
   `core/embeddings.py`, provider OpenAI `text-embedding-3-small` (cheia există),
   cost înregistrat prin `record_cost_async`, cache în DB pe (hash text, model)
   ca să nu plătești de două ori același pasaj. Tabel nou în
   `api/models/content.py`, migrație Alembic.
2. Pasaje: conținutul paginilor deja scrapate/crawlate, tăiat în bucăți de
   ~150-300 de cuvinte, pe headings unde se poate.
3. Pentru fiecare sub-query: cel mai bun pasaj de pe site și scorul de
   similaritate. Calibrează pragul „acoperit / slab / neacoperit" pe câteva
   exemple verificate manual și scrie exemplele în test — nu alege pragul din burtă.
4. Rezultat: lista de sub-query-uri neacoperite, grupate tematic, cu pagina
   existentă cea mai apropiată (de extins) sau „pagină nouă". Butonul spre
   content brief-ul existent, cu sub-query-urile ca input.
5. Teste cu vectori fixați (mock la provider): pasaj potrivit → acoperit;
   niciun pasaj → neacoperit; cache-ul evită al doilea apel.

---

## Pasul 18 — Bucla de învățare: acțiune aplicată → efect măsurat

**De ce:** fără ea, pașii 12-17 produc recomandări, dar nu afli niciodată care
funcționează. `action_cards` (`api/models/content.py`) există, dar e legată
obligatoriu de un audit (`audit_id` NOT NULL) și nu reține **când** s-a aplicat
o acțiune (doar `updated_at`, care se schimbă la orice editare).

**De făcut:**
1. Extinde `action_cards` (migrație Alembic, cu `batch_alter_table` pentru SQLite):
   `audit_id` nullable, plus `source` (`audit`, `js_visibility`,
   `gsc_opportunity`, `internal_link`, `decay`, `citation_gap`, `fanout_gap`),
   `applied_at`, `metric_baseline` (JSON). Testează migrația up/down/up pe o copie
   WAL-safe a DB-ului real și verifică `foreign_key_check` și numărul de audituri.
2. Pașii 12-17 creează acțiuni în acest tabel, cu URL-ul și valorile măsurate
   la momentul recomandării.
3. Marcarea „aplicat" setează `applied_at`. Cronologia de la pasul 10 afișează
   acțiunile aplicate ca marcaje, lângă schimbările detectate de snapshot.
4. Raport: per `source`, acțiunile aplicate cu 28+ zile de date după, și
   evoluția metricii relevante (clicks, poziție, rată de citare cu interval de la
   pasul 9). Formulare „după", nu „din cauza"; sub 5 acțiuni per tip, fără concluzii.
5. Teste: acțiune fără audit se salvează; raportul exclude acțiunile cu date
   insuficiente; migrația păstrează cardurile existente.

---

## În afara acestui plan

- **Punctul 9 (livrare către client)** — doar dacă tool-ul iese din uz personal.
- **Performanța în scorul compozit** — lăsată separat intenționat (vezi punctul 2
  din IMPROVEMENTS_PLAN).
- **Rularea neexplicată „ING Romania" absent** — nereprodusă în două reîncercări;
  nu merită timp fără o a doua apariție.
