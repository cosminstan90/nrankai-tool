#!/usr/bin/env python3
"""
Diff intre suprafata API curenta si baseline-ul din tests/baseline/openapi.json.

Regula de aur din planul de consolidare: URL-urile publice nu se schimba.
Dupa fiecare etapa de consolidare, acest diff trebuie sa fie gol.

Foloseste:
    python tests/api_diff.py              # compara serverul curent cu baseline-ul
    python tests/api_diff.py --update      # rescrie baseline-ul (doar dupa o schimbare aprobata)
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:8000"
BASELINE_FILE = Path(__file__).parent / "baseline" / "openapi.json"


def fetch_current() -> dict:
    with urllib.request.urlopen(BASE_URL + "/openapi.json", timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def path_ops(spec: dict) -> set:
    ops = set()
    for path, methods in spec["paths"].items():
        for m in methods:
            if m in ("get", "post", "put", "delete", "patch"):
                ops.add(f"{m.upper()} {path}")
    return ops


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()

    current = fetch_current()

    if args.update:
        BASELINE_FILE.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Baseline API actualizat: {len(current['paths'])} path-uri -> {BASELINE_FILE}")
        return

    if not BASELINE_FILE.exists():
        print(f"Lipseste {BASELINE_FILE} - ruleaza intai cu --update", file=sys.stderr)
        sys.exit(2)

    baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))

    old_ops = path_ops(baseline)
    new_ops = path_ops(current)

    removed = sorted(old_ops - new_ops)
    added = sorted(new_ops - old_ops)

    print(f"Baseline: {len(old_ops)} operatii | Curent: {len(new_ops)} operatii")

    if removed:
        print("\n=== DISPARUTE fata de baseline (regula de aur incalcata) ===")
        for op in removed:
            print(f"  - {op}")

    if added:
        print("\n=== NOI fata de baseline (verifica daca e intentionat) ===")
        for op in added:
            print(f"  + {op}")

    if removed:
        print("\nFAIL - operatii publice au disparut. Regula de aur: URL-urile publice nu se schimba.")
        sys.exit(1)

    if not added:
        print("\nOK - suprafata API identica cu baseline-ul.")
    else:
        print("\nOK - nicio operatie disparuta (doar adaugiri, verifica manual daca sunt intentionate).")


if __name__ == "__main__":
    main()
