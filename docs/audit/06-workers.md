# Faza 6 — Workers și joburi de fundal

## Rezumat

| ID | Sev | Titlu | Verificat prin |
|----|-----|-------|-----------------|
| F6-01 | ✅ verificat, fără problemă | Zero încălcări ale regulii „nu `Depends(get_db)` în background tasks" — toți workerii folosesc corect `AsyncSessionLocal()` | `grep -rn "Depends(get_db)" api/workers` → 0 rezultate |
| F6-02 | ✅ verificat, fără problemă | Zero audituri blocate în stări intermediare (`processing`/`scraping`/etc.) în DB-ul live — doar `completed` (62) și `failed` (22) | interogare directă pe `api/data/analyzer.db` |
| F6-03 | P2 | **26% dintre audituri (22/84) au eșuat istoric** — dar detaliile reale sunt păstrate corect în `audit_logs`, doar câmpul sumar `error_message` e generic („Analysis step failed") | verificat: un audit eșuat are în `audit_logs` mesajul real (`Input directory not found: ...`) — design corect, nu bug |
| F6-04 | P2 (extinde F4-06) | **92 de apeluri `print()` cu caractere non-ASCII** (✓, emoji) în **16 fișiere**, inclusiv `api/models/database.py` — exact clasa de bug reparată o dată în commit `2588815`, dar rămasă în alte 91 de locuri | grep exhaustiv pe `api core app` |
| F6-05 | ✅ verificat, fără problemă | Toate cele 20 module top-level din `api/workers/` au ≥1 referință în cod — niciun worker complet orfan | import-graph pe `api/workers/*.py` |
| F6-06 | P3 | Mesajul de eroare „Server restarted..." găsit în DB conține un caracter corupt (mojibake) și text diferit de versiunea curentă din `api/main.py:119` — probabil dintr-o versiune anterioară a codului, date istorice, nu bug reproductibil azi | comparație text sursă vs. conținut DB |

---

## F6-04 — `print()` cu non-ASCII: bomba din 2588815 nu e complet dezamorsată

Commitul `2588815` a reparat **un** `UnicodeEncodeError` cauzat de emoji în
`print()` pe Windows. Verificare exhaustivă arată că mai există **92 de
apeluri similare, în 16 fișiere**:

```
api/models/database.py        api/workers/prompt_discovery.py
api/routes/benchmarks.py      core/compare_audits.py
api/routes/citation_tracker.py core/config.py
api/routes/gap_analysis.py    core/cross_reference_analyzer.py
api/routes/geo_monitor.py     core/direct_analyzer.py
api/routes/gsc/optimizer.py   core/generate_report.py
api/routes/schedules.py       core/history_tracker.py
api/routes/templates_manager.py core/validate_audit.py
```

**Cel mai important caz:** `api/models/database.py` — funcția `init_db()`,
apelată **la fiecare pornire a serverului** (`api/main.py:71`, vezi și Faza 2
F2-05), conține chiar ea `print("✓ Migrat ...")`. Serverul local pornește azi
fără probleme **doar pentru că** `restart_server.bat` setează explicit
`set PYTHONUTF8=1` înainte de a lansa `uvicorn`. Orice altă metodă de
pornire pe Windows fără acest flag (ex. un serviciu Windows, un profil de
rulare din IDE, Task Scheduler) ar întâmpina exact `UnicodeEncodeError` **la
primul `print()` din `init_db()`, blocând complet startup-ul**.

**Nuanță importantă:** riscul e specific Windows. Dacă serverul de producție
(`app.nrankai.com`) rulează pe Linux (terminal UTF-8 implicit), aceste 92 de
apeluri nu prezintă niciun risc acolo — problema e limitată strict la
dezvoltarea locală pe Windows fără `PYTHONUTF8=1` explicit. Nu am verificat
sistemul de operare al serverului de producție (`NEEDS-RUNTIME-CHECK`).

**Recomandare Faza 9:** înlocuiți sistematic `print()` cu `logging` (deja
regula implicită de bun-simț, confirmată de fix-ul din `2588815`) — sau, mai
simplu și mai robust, setați `PYTHONUTF8=1` la nivel de proces în
`api/main.py` însuși (`sys.stdout.reconfigure(encoding='utf-8')` la
pornire), ca să nu depindă de cum e lansat procesul extern.

---

## F6-03 — Rata de eșec a audit-urilor: design OK, dar de urmărit

```sql
SELECT status, COUNT(*) FROM audits GROUP BY status;
-- completed: 62, failed: 22   (≈ 26% failure rate istoric)

SELECT error_message, COUNT(*) FROM audits WHERE status='failed' GROUP BY error_message;
--  11  Analysis step failed
--   6  Server restarted while audit was running ... (mesaj vechi/mojibake)
--   2  Scraping step failed
--   2  Conversion step failed
--   1  Audit cancelled by user
```

Verificat pe un caz concret (`audit_id=95804ad3-...`): deși
`audits.error_message` arată generic „Analysis step failed", tabela
`audit_logs` conține mesajul real:
`ERROR: Input directory not found: stancosmin.com\input_llm` — deci
utilizatorul care deschide detaliile audit-ului **poate** vedea cauza reală.
**Design corect, nu bug** — dar rata de 26% eșecuri istorice merită atenția
lui Cosmin, chiar dacă nu ține de acest audit (posibil legată de audituri
fără sitemap, fără fișiere de input, sau interupte manual — nu de un bug de
cod).

---

## Concluzie Faza 6

Partea de infrastructură a workerilor e sănătoasă structural — fără
încălcări de pattern DB, fără joburi blocate, fără workeri orfani. Singurul
risc real e clasa de bug „print() cu non-ASCII pe Windows", deja cunoscută
din istoricul de commit-uri dar încă prezentă în 16 fișiere — cu impact
potențial mare (poate bloca pornirea serverului) dar declanșat doar în
condiții specifice de lansare pe Windows, nereproductibil azi cu
`restart_server.bat`.
