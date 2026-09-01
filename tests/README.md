# Teste și plasă de siguranță pentru consolidare

Vezi `docs/CONSOLIDATION_PLAN.md` pentru context complet. Rezumat rapid:

## Teste unitare

```bash
python -m pytest
```

Config în `pytest.ini` (adaugă `core/` la `sys.path`). 34 de teste în
`test_content_chunker.py`.

## Smoke test de regresie (`smoke.py`)

Verifică statusul HTTP al celor 102 GET-uri fără parametri de rută, față de
un baseline cunoscut. **Serverul trebuie să ruleze deja** (`restart_server.bat`).

```bash
python tests/smoke.py              # compară cu baseline, exit 1 dacă apare 500 nou
python tests/smoke.py --update     # rescrie baseline-ul (după un fix confirmat manual)
```

Baseline curent (înainte de Etapa 1, `docs/audit/FINDINGS.md`): **5×500** —
`/api/briefs`, `/api/fanout/sessions`, `/api/gsc/oauth/authorize`,
`/api/portfolio/alerts`, `/api/portfolio/overview`. După fix-urile din
Etapa 1, rulează `--update` ca să confirmi 0×500 devine noul baseline.

## Diff de suprafață API (`api_diff.py`)

**Regula de aur a consolidării: URL-urile publice nu se schimbă.** Acest
script compară toate operațiile (metodă + path) din `openapi.json` curent
față de `tests/baseline/openapi.json`. După orice etapă de consolidare,
diff-ul trebuie să fie gol (fără path-uri dispărute).

```bash
python tests/api_diff.py           # compară, exit 1 dacă a dispărut ceva
python tests/api_diff.py --update  # rescrie baseline-ul (doar după schimbare intenționată)
```

## Ordinea de verificare pentru fiecare etapă de consolidare

1. `python -m pytest` — verde
2. `python tests/smoke.py` — fără 500 nou
3. `python tests/api_diff.py` — fără path-uri dispărute
4. Explică orice diferență față de pașii 2-3 înainte de commit
