"""
Audit-quality evaluation harness -- Pasul 11 of docs/superpowers/plans/2026-09-30-next-steps.md.

CLAUDE.md is explicit that any change to prompts/ changes every audit's
output, but nothing measured whether a change was an improvement or a
regression. This runs the real DirectAnalyzer pipeline
(core.direct_analyzer.run_direct_analysis, the same function
api/workers/audit_worker.py calls in production) against a handful of
fixture pages with KNOWN, deliberately-planted issues
(tests/eval/fixtures/<name>/expected.yaml) and reports recall: of the issues
a fixture is known to have, how many did the model's actual output mention.

Matching is a case-insensitive substring search for any of an issue's
`keywords` anywhere in the model's raw JSON output -- deliberately crude
(the plan asks for keyword matching, not semantic grading) and the keyword
lists in expected.yaml are a starting guess, worth widening after the first
real run shows how the model actually phrases a given finding.

Costs real money -- DirectAnalyzer.run() records every real call to the
`cost_records` table exactly as a production audit would (source="audit",
website=None since a fixture has no real site), so an eval run's spend is
counted in /costs like anything else, just not separable from real audits
there. This is NOT part of `pytest` and never runs automatically -- run it
manually, deliberately, before and after a prompt change to compare two
results/*.json files:

    python scripts/run_eval.py
    python scripts/run_eval.py --audit-type ACCESSIBILITY_AUDIT
    python scripts/run_eval.py --fixture thin_content --provider anthropic --model claude-haiku-4-5-20251001

This never modifies a prompt -- it only measures the ones that already exist.
"""
import argparse
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Optional

import yaml

from core.axe_runner import summarize as axe_summarize, write_axe_results
from core.direct_analyzer import run_direct_analysis
from core.prompt_loader import load_prompt

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(REPO_ROOT, "tests", "eval", "fixtures")
RESULTS_DIR = os.path.join(REPO_ROOT, "tests", "eval", "results")

DEFAULT_PROVIDER = "ANTHROPIC"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"   # cheapest current model -- an eval run should not need the flagship


def load_fixtures(fixtures_dir: str = FIXTURES_DIR, audit_type: Optional[str] = None,
                  fixture_name: Optional[str] = None) -> list:
    """Every tests/eval/fixtures/<name>/ with an expected.yaml, optionally filtered."""
    fixtures = []
    if not os.path.isdir(fixtures_dir):
        return fixtures
    for entry in sorted(os.listdir(fixtures_dir)):
        path = os.path.join(fixtures_dir, entry)
        expected_path = os.path.join(path, "expected.yaml")
        if not os.path.isdir(path) or not os.path.isfile(expected_path):
            continue
        if fixture_name and entry != fixture_name:
            continue
        with open(expected_path, encoding="utf-8") as f:
            expected = yaml.safe_load(f)
        if audit_type and expected.get("audit_type") != audit_type:
            continue
        fixtures.append({"name": entry, "path": path, "expected": expected})
    return fixtures


def prompt_hash(audit_type: str) -> str:
    """Short hash of the assembled prompt actually used for audit_type, so a
    result file can be tied to the exact prompt version it measured."""
    try:
        prompt_text = load_prompt(audit_type)
    except Exception as exc:
        return f"unavailable ({exc})"
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12]


def score_matches(expected_issues: list, output_text: str) -> dict:
    """
    Pure matching logic, split out from the async run so it's testable
    without spending money: given the model's raw output (already lowercased
    by the caller) and an expected.yaml's `issues` list, return which issue
    ids were matched vs missed, and the recall fraction.
    """
    matched, missed = [], []
    for issue in expected_issues:
        keywords = [k.lower() for k in issue.get("keywords", [])]
        if any(k in output_text for k in keywords):
            matched.append(issue["id"])
        else:
            missed.append(issue["id"])
    total = len(matched) + len(missed)
    recall = round(len(matched) / total, 3) if total else None
    return {"matched": matched, "missed": missed, "recall": recall}


async def run_one_fixture(fixture: dict, provider: str, model: str) -> dict:
    """Run the real pipeline on one fixture and score its output. Costs money."""
    name, path, expected = fixture["name"], fixture["path"], fixture["expected"]
    audit_type = expected["audit_type"]
    website = expected.get("website")

    with tempfile.TemporaryDirectory(prefix=f"eval_{name}_") as tmp:
        input_dir = os.path.join(tmp, "input")
        html_dir = os.path.join(tmp, "html")
        output_dir = os.path.join(tmp, "output")
        for d in (input_dir, html_dir, output_dir):
            os.makedirs(d, exist_ok=True)

        txt_src = os.path.join(path, "page.txt")
        if not os.path.isfile(txt_src):
            return {"fixture": name, "audit_type": audit_type, "error": "page.txt missing", "recall": None}
        shutil.copy(txt_src, os.path.join(input_dir, "page.txt"))

        html_src = os.path.join(path, "page.html")
        have_html = os.path.isfile(html_src)
        html_dst = os.path.join(html_dir, "page.html") if have_html else None
        if have_html:
            shutil.copy(html_src, html_dst)

        # Reuses the exact sidecar-writing code the real accessibility
        # pipeline uses (core/axe_runner.py) -- a fixture just supplies the
        # raw axe-core JSON that would have come from a real measurement.
        raw_axe_path = os.path.join(path, "raw_axe.json")
        if os.path.isfile(raw_axe_path) and html_dst:
            with open(raw_axe_path, encoding="utf-8") as f:
                raw = json.load(f)
            write_axe_results(html_dst, axe_summarize(raw))

        stats = await run_direct_analysis(
            input_dir=input_dir, output_dir=output_dir, question_type=audit_type,
            provider=provider, model_name=model, website=website,
            html_dir=html_dir if have_html else None,
        )

        output_files = [f for f in os.listdir(output_dir) if f.endswith(".json")]
        if not output_files:
            return {
                "fixture": name, "audit_type": audit_type, "error": "no output produced",
                "matched": [], "missed": [i["id"] for i in expected.get("issues", [])], "recall": 0.0,
            }

        combined_text = ""
        for fname in output_files:
            with open(os.path.join(output_dir, fname), encoding="utf-8") as f:
                combined_text += f.read().lower()

        result = score_matches(expected.get("issues", []), combined_text)
        result.update({
            "fixture": name, "audit_type": audit_type,
            "input_tokens": stats.total_input_tokens, "output_tokens": stats.total_output_tokens,
        })
        return result


async def run_eval(audit_type: Optional[str], fixture_name: Optional[str],
                   provider: str, model: str) -> dict:
    from api.routes.costs import calculate_cost

    fixtures = load_fixtures(audit_type=audit_type, fixture_name=fixture_name)
    if not fixtures:
        return {"error": f"No fixtures matched (audit_type={audit_type!r}, fixture={fixture_name!r})",
                "fixtures_dir": FIXTURES_DIR, "results": []}

    results = []
    for fx in fixtures:
        result = await run_one_fixture(fx, provider, model)
        result["prompt_hash"] = prompt_hash(fx["expected"]["audit_type"])
        result["cost_usd"] = round(calculate_cost(
            provider.lower(), model, result.get("input_tokens", 0) or 0, result.get("output_tokens", 0) or 0
        ), 4)
        results.append(result)
        recall_str = f"{result['recall']:.0%}" if result.get("recall") is not None else "n/a"
        print(f"  {result['fixture']:<20} recall={recall_str:>5}  "
             f"matched={result.get('matched')}  missed={result.get('missed')}  "
             f"${result['cost_usd']:.4f}")

    recalls = [r["recall"] for r in results if r.get("recall") is not None]
    summary = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider, "model": model,
        "fixtures_run": len(results),
        "mean_recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "total_cost_usd": round(sum(r["cost_usd"] for r in results), 4),
        "results": results,
    }
    return summary


def _write_result_file(summary: dict) -> str:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(RESULTS_DIR, f"{stamp}_{summary.get('model', 'unknown')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--audit-type", default=None, help="Only run fixtures for this audit type")
    parser.add_argument("--fixture", default=None, help="Only run this one fixture by directory name")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--no-save", action="store_true", help="Don't write a result file")
    args = parser.parse_args()

    print(f"Running eval: provider={args.provider} model={args.model} "
         f"audit_type={args.audit_type or 'all'} fixture={args.fixture or 'all'}")
    print("This calls a real LLM and costs money.\n")

    summary = asyncio.run(run_eval(args.audit_type, args.fixture, args.provider, args.model))

    if "error" in summary:
        print(summary["error"])
        return

    print(f"\nMean recall: {summary['mean_recall']}  "
         f"Total cost: ${summary['total_cost_usd']}")

    if not args.no_save:
        path = _write_result_file(summary)
        print(f"Saved: {path}")


if __name__ == "__main__":
    main()
