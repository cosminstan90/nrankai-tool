#!/usr/bin/env python3
"""
Smoke test de regresie — verifica statusul HTTP al tuturor GET-urilor fara
parametri de ruta, fata de un baseline cunoscut.

Foloseste:
    python tests/smoke.py                 # ruleaza si compara cu baseline
    python tests/smoke.py --update        # rescrie baseline-ul (dupa un fix confirmat)

Baseline: tests/baseline/get_paths.txt (lista de path-uri) +
          tests/baseline/expected_status.json (status asteptat per path)

Server-ul trebuie sa ruleze deja pe http://127.0.0.1:8000 (restart_server.bat).
"""
import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

BASE_URL = "http://127.0.0.1:8000"
BASELINE_DIR = Path(__file__).parent / "baseline"
PATHS_FILE = BASELINE_DIR / "get_paths.txt"
EXPECTED_FILE = BASELINE_DIR / "expected_status.json"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Nu urma redirect-uri — vrem statusul brut (307/302), la fel ca `curl` fara -L."""
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def check_path(path: str) -> int:
    try:
        req = urllib.request.Request(BASE_URL + path, method="GET")
        with _opener.open(req, timeout=8) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        print(f"  CONNECTION ERROR on {path}: {e}", file=sys.stderr)
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="Rescrie baseline-ul cu statusurile curente")
    args = parser.parse_args()

    if not PATHS_FILE.exists():
        print(f"Lipseste {PATHS_FILE} - genereaza-l intai din openapi.json", file=sys.stderr)
        sys.exit(2)

    paths = [p for p in PATHS_FILE.read_text(encoding="utf-8").splitlines() if p]
    results = {}
    for p in paths:
        results[p] = check_path(p)

    if args.update:
        EXPECTED_FILE.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Baseline actualizat: {len(results)} path-uri -> {EXPECTED_FILE}")
        return

    if not EXPECTED_FILE.exists():
        print(f"Lipseste {EXPECTED_FILE} — ruleaza intai cu --update", file=sys.stderr)
        sys.exit(2)

    expected = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))

    new_500 = []
    fixed = []
    changed = []
    for p, code in results.items():
        exp = expected.get(p)
        if exp is None:
            print(f"  NOU (nu era in baseline): {code}  {p}")
            continue
        if code == exp:
            continue
        if code == 500:
            new_500.append((p, exp, code))
        elif exp == 500 and code != 500:
            fixed.append((p, exp, code))
        else:
            changed.append((p, exp, code))

    print(f"Verificate: {len(results)} path-uri")
    counts = {}
    for c in results.values():
        counts[c] = counts.get(c, 0) + 1
    for c, n in sorted(counts.items()):
        print(f"  {c}: {n}")

    if fixed:
        print("\n=== REPARATE fata de baseline (era 500, acum nu mai e) ===")
        for p, old, new in fixed:
            print(f"  {p}: {old} -> {new}")

    if changed:
        print("\n=== ALTE SCHIMBARI (necesita explicatie) ===")
        for p, old, new in changed:
            print(f"  {p}: {old} -> {new}")

    if new_500:
        print("\n=== REGRESIE: 500 NOU aparut ===")
        for p, old, new in new_500:
            print(f"  {p}: era {old}, acum 500")
        sys.exit(1)

    print("\nOK - nicio regresie noua (500) fata de baseline.")


if __name__ == "__main__":
    main()
