# Faza 0 — Baseline

Data execuției: 2026-09-01. Server pornit manual (`uvicorn api.main:app --host 127.0.0.1 --port 8000`, `PYTHONUTF8=1`) — `restart_server.bat` echivalent, deschide fereastră separată deci s-a rulat direct.

## Rezumat

| Check | Rezultat |
|---|---|
| `python -c "import api.main"` | ✅ trece curat, fără erori/warnings |
| Server startup | ✅ `Application startup complete`, fără traceback |
| `GET /api/health` | ✅ 200, `"database":"connected"`, `db_response_ms: 2-4ms` |
| Providers active | anthropic ✅, openai ✅, perplexity ✅ — **gemini ❌, mistral ❌** (fără cheie API în `.env` local) |
| OpenAPI paths | **315 path-uri, 367 operații** |
| Ultimul commit | **2026-05-28** — **95 de zile în urmă** față de azi (2026-09-01) |
| Prim commit | 2026-03-20 → proiect cu 73 de commituri în ~2 luni de dezvoltare activă, apoi 3 luni de tăcere |

## Findings

| ID | Sev | Titlu | Verificat prin |
|----|-----|-------|----------------|
| F0-01 | P1 | Proiectul nu a mai fost atins de codebase de **95 de zile**, deși rulează încă în producție la app.nrankai.com | `git log -1 --format=%ci` = 2026-05-28; data curentă = 2026-09-01 |
| F0-02 | P2 | Cele 4 fișiere `.db` de la root (`analyzer.db`, `database.db`, `geo.db`, `geo_tool.db`) sunt **0 bytes** și complet moarte — DB-ul real e la `api/data/analyzer.db` (45MB, `DATABASE_DIR` din `api/models/_base.py:15`) | `ls -la *.db` vs `api/models/_base.py:15-17`; confirmat prin `ls -la api/data/*.db` |
| F0-03 | P3 | `api/data/analyzer.db` nu a mai fost scris din **21 aprilie** (`mtime`), deși serverul e "connected" acum | `ls -la api/data/analyzer.db` → mtime Apr 21. De verificat în Faza 3/6: se mai rulează audituri de fapt, sau doar health-check-ul reușește să deschidă conexiunea? |
| F0-04 | P1 | Worker-ul `lead_audit_worker` are erori recurente **`Worker poll error:` cu mesaj gol** (str(exception) vid) — 9 apariții în `uvicorn_out.txt`, plus una explicită `[Errno 11001] getaddrinfo failed` (eșec DNS) | `grep -c "Worker poll error:" uvicorn_out.txt` → 10 total, 9 cu mesaj gol. Semnal pentru Faza 6: excepția prinsă probabil nu are `str()` populat (ex. `httpx.ConnectError` fără mesaj) — logging-ul ascunde cauza reală |
| F0-05 | P3 | Gemini și Mistral nedisponibile în mediul local (lipsă chei API) | `/api/health` → `"gemini":false,"mistral":false`. Nu e neapărat bug — poate fi doar `.env` local incomplet. De reconfirmat pe producție în Faza 4 |
| F0-06 | — | Fișiere de log rotite masiv: `website_llm_analyzer.log` (4.5MB, azi) + `.log.1` (5.2MB, iunie) + `.log.2` (5.2MB, aprilie), toate untracked | Nu e bug, dar ar trebui în `.gitignore` (deja e, verificat) — sunt doar zgomot în `git status`. Conțin însă istoric util: `.log.2` are erori `RateLimitError 429` masive pe `gpt-4o-mini` (2 apr) — semnal că rate-limiting-ul OpenAI a fost o problemă reală, de verificat dacă mai există retry/backoff (Faza 4/6) |

## Inventar cantitativ

| Metrică | Valoare |
|---|---|
| Fișiere `.py` trackuite | 179 |
| LOC total (`api/ + core/ + app/`) | 61.916 linii |
| Rute API (module în `api/routes/`) | 63 fișiere → 315 path-uri OpenAPI / 367 operații |
| Workers (`api/workers/`) | 34 fișiere |
| Template-uri HTML | 57 (`api/templates/**/*.html`, include `partials/`, `clusteriq/`, `serpiq/`) |
| Prompturi YAML (`prompts/`) | 20 |
| Prompturi `.txt` (`api/prompts/`) | 1 (`meta_generator.txt`) |
| Migrări Alembic | 8 (`0001`–`0008`) |
| Teste | 1 fișier (`test_content_chunker.py`, root, fără config pytest) |

## Vechime pe module (`api/routes/`)

Cel mai vechi: `health.py` (2026-04-05), `results.py`/`tracking.py` (2026-04-09).
Cel mai nou: `schema_gen.py` (2026-05-28, ultimul commit din tot proiectul).
Marea majoritate a rutelor (~35 fișiere) au fost create/atinse simultan pe
**2026-04-24 09:14:00** — probabil un commit mare de refactor/adăugare în masă.
→ Pentru Faza 1: rutele din acel batch (24 apr) sunt cele mai probabile să conțină
cod copy-paste între ele, dat fiind că au fost scrise/mutate în același commit.

## Fișiere pentru fazele următoare

- `docs/audit/openapi.json` — dump complet, 315 paths — sursă de adevăr pentru Faza 2 și 8.

## Gate

✅ Serverul pornește curat. Nicio blocare. **Fazele 1–8 pot continua.**
