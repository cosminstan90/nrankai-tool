# Faza 8 — Frontend: template-uri și JS inline

## Rezumat

| ID | Sev | Titlu | Verificat prin |
|----|-----|-------|-----------------|
| F8-01 | ✅ verificat, fără problemă | Toate `{% extends %}`/`{% include %}` (3 în total, foarte puține — majoritatea template-urilor sunt full-page, nu compuse) rezolvă corect către fișiere existente | comparație automată `extends`/`include` vs. fișiere din `api/templates/` |
| F8-02 | ✅ verificat, fără problemă | Niciun template orfan real. `base.html` (layout extins) și `partials/audit_row_content.html` (inclus din `audit_row.html`) apar „neapelate direct" doar pentru că verificarea inițială a căutat exclusiv `TemplateResponse()`, nu și `{% include %}` — ambele sunt de fapt folosite | verificare manuală după corectarea metodei |
| F8-03 | ✅ verificat, fără problemă | Toate cele 192 apeluri `fetch()` din template-uri corespund unui path real din OpenAPI (detaliat în `02-routes.md` F2-08) | cross-referință |
| F8-04 | **P0 (confirmare vizuală a F2-02)** | `/portfolio` — pagina se încarcă complet **goală** (doar titlu + buton Refresh), fără niciun mesaj de eroare vizibil pentru utilizator; eroarea reală apare doar în consolă | navigare live + captură ecran |
| F8-05 | **P0 (confirmare vizuală a F2-01)** | Click pe „Connect Google" din `/gsc` duce direct la `{"detail":"Internal server error"}` în loc de ecranul de consimțământ Google | navigare live + captură ecran |
| F8-06 | P1 (confirmare parțială a F2-03) | `/briefs` — pagina **se degradează corect** (arată „Total Briefs: 0" în loc să crape vizual), dar din cauza F2-03 nu poate arăta niciodată brief-urile existente | navigare live, 3 erori 500 în consolă, UI totuși funcțional pentru generare de brief-uri noi |
| F8-07 | ✅ verificat, fără problemă | `/` (Dashboard) și `/gsc` se încarcă curat, cu date reale, zero erori de consolă | navigare live + `read_console_messages` |
| F8-08 | P1 (confirmare vizuală a F2-04) | `/fanout` — degradare grațioasă identică cu `/briefs`: formularul principal funcționează, „Recent Sessions" arată gol în loc de eroare | navigare live, 500 în consolă |

---

## Verificare live în browser

Am pornit serverul din Faza 0 și am navigat la 4 pagini cheie, verificând
consola și conținutul randat.

### `/` — Dashboard: ✅ curat
Zero erori de consolă. Date reale afișate (29 audituri totale, 7 completate,
6469 pagini analizate, scor mediu 56.2). Funcționează corect.

### `/portfolio` — Portfolio Dashboard: 🔴 complet nefuncțional pentru utilizator

Confirmă vizual F2-02 (bug datetime naiv/aware în `detect_alerts_for_website`).
Pagina randează doar titlul și un buton „Refresh" — nicio secțiune de
conținut, niciun mesaj de eroare vizibil. Un utilizator care ajunge aici
fără să deschidă consola dezvoltatorului ar crede că pur și simplu nu are
date, nu că feature-ul e rupt.

```
[error] Failed to load resource: 500 (Internal Server Error)
[error] Error loading portfolio: Failed to fetch portfolio data
```

**Recomandare Faza 9:** dincolo de fix-ul de backend (F2-02/F3-03), adăugați
un mesaj de eroare vizibil în UI („Nu s-au putut încărca datele — reîncearcă")
în loc de pagină goală silențioasă — pattern relevant și pentru celelalte
endpoint-uri care ar putea eșua în viitor.

### `/gsc` → „Connect Google" → 🔴 rupt

```
GET /api/gsc/oauth/authorize → {"detail":"Internal server error"}
```

Confirmă vizual F2-01 (`NameError: _CLIENT_CONFIG`). Restul paginii `/gsc`
funcționează bine — proprietăți existente afișate corect cu date reale
(25.000 queries, 11.832 pagini pentru `sc-domain:conso.ro`) — doar butonul
de conectare nouă e rupt. Fluxul CSV manual (alternativa la OAuth) rămâne
funcțional.

### `/briefs` — Content Briefs: 🟡 degradare parțial grațioasă

```
GET /api/briefs → 500 (de 3 ori — probabil brief-uri + stats + listă)
```

Spre deosebire de `/portfolio`, această pagină **nu rămâne complet goală** —
formularul de generare brief-uri noi (bulk + single-page) se randează
complet și pare funcțional independent de endpoint-ul rupt. Doar contorul
„Total Briefs: 0 / Approved: 0 / In Progress: 0 / Completed: 0" e greșit
(ar trebui să reflecte brief-uri existente, dar din cauza F2-03 interogarea
de listă eșuează și clientul JS pare să cadă grațios pe valori implicite 0).
Impact real: utilizatorul nu poate vedea brief-urile deja generate din UI,
dar poate genera unele noi.

---

### `/fanout` — Fan-Out Analyzer: 🟡 degradare parțial grațioasă (ca `/briefs`)

```
GET /api/fanout/sessions → 500 (de mai multe ori)
```

Confirmă vizual F2-04. La fel ca `/briefs`, formularul principal
(„Analyze Fan-Out") se randează complet și pare funcțional. Secțiunea
„Recent Sessions" arată „No sessions yet — run your first analysis above."
în loc de un mesaj de eroare — degradare grațioasă din partea clientului
JS, dar utilizatorul nu poate vedea sesiunile deja existente în DB.

---

## Concluzie Faza 8

Verificarea statică (extends/include, template-uri orfane, fetch↔endpoint)
confirmă că infrastructura de front-end e curată — nicio referință ruptă la
nivel de fișiere. Verificarea live confirmă vizual **toate cele 4**
endpoint-uri 500 găsite în Faza 2, cu 2 tipare de degradare distincte:
`/portfolio` eșuează „tăcut" (pagină goală, fără mesaj către utilizator),
în timp ce `/briefs` și `/fanout` se degradează grațios (formularul de
creare rămâne funcțional, doar lista de elemente existente arată gol în loc
de eroare). `/gsc` e singura pagină unde utilizatorul lovește direct un
răspuns JSON brut de eroare, la un click pe un buton vizibil din UI.
Recomand ca fix-ul de backend din Faza 9 să fie însoțit de un mesaj de
eroare vizibil și pe `/portfolio`, după modelul mai bun deja prezent în
`/briefs`/`/fanout`.
