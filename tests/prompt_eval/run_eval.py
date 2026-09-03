#!/usr/bin/env python3
"""
Prompt evaluation harness -- Etapa 6.1 of docs/CONSOLIDATION_PLAN.md.

`prompts/` is frozen by convention ("do not modify without a plan" --
CLAUDE.md) precisely because there was no way to tell whether a prompt
edit made real audits better or worse. This is that way: 9 real production
pages (confirmed by the user, 2026-09-03) spanning low/mid/high scores
across 3 audit types, re-run against whatever prompts/*.yaml currently
says, compared against the expected score with tolerance.

NOT a pytest test -- this makes real, paid LLM calls (9 calls per run,
default provider/model) and is never invoked automatically. Run by hand:

    python tests/prompt_eval/run_eval.py                      # all cases
    python tests/prompt_eval/run_eval.py --audit-type GEO_AUDIT
    python tests/prompt_eval/run_eval.py --provider anthropic --model claude-sonnet-5

Exit code 0 if every case is within tolerance, 1 otherwise -- usable in a
manual pre-merge check for a prompt change, never in CI/automatic testing.

Adding a new reference case: drop a JSON file into reference_cases/ with
the same shape as the existing ones (label, audit_type, expected_score,
tolerance, page_text, source). No code change needed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.direct_analyzer import AsyncLLMClient, clean_json_response  # noqa: E402
from core.output_schemas import get_output_schema  # noqa: E402
from core.prompt_loader import load_prompt  # noqa: E402
from api.utils.audit_json import AUDIT_ROOT_KEYS  # noqa: E402

REFERENCE_DIR = Path(__file__).parent / "reference_cases"


def load_reference_cases(audit_type_filter: str | None = None, label_filter: str | None = None) -> list[dict]:
    cases = []
    for path in sorted(REFERENCE_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        case["_file"] = path.name
        if audit_type_filter and case["audit_type"].upper() != audit_type_filter.upper():
            continue
        if label_filter and case["label"] != label_filter:
            continue
        cases.append(case)
    return cases


def extract_score(result_data: dict) -> int | None:
    """Same root-key walk audit_rerun.py uses to find the score in any
    audit type's output shape, kept consistent rather than reinvented."""
    for key in AUDIT_ROOT_KEYS + ["score", "overall_score"]:
        if key not in result_data:
            continue
        val = result_data[key]
        if isinstance(val, dict):
            for score_key in ("overall_score", "score"):
                if score_key in val:
                    try:
                        return int(val[score_key])
                    except (ValueError, TypeError):
                        continue
        elif isinstance(val, (int, float)):
            return int(val)
        elif isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                pass
    return None


async def run_one_case(case: dict, provider: str, model: str) -> dict:
    audit_type = case["audit_type"]
    expected = case["expected_score"]
    tolerance = case.get("tolerance", 15)
    page_text = case["page_text"][:30000]

    outcome = {
        "label": case["label"], "audit_type": audit_type,
        "expected": expected, "tolerance": tolerance,
    }

    if not page_text.strip():
        # Empty-content case (e.g. a login page) -- no LLM call needed,
        # the expected outcome IS "no scoreable content".
        outcome.update(actual=0, passed=(expected == 0), note="empty page_text, scored 0 without an LLM call")
        return outcome

    try:
        system_message = load_prompt(audit_type)
        client = AsyncLLMClient(provider=provider.upper(), model_name=model)
        try:
            response_text, in_tok, out_tok = await client.complete(
                system_message=system_message,
                user_content=page_text,
                output_schema=get_output_schema(audit_type),
            )
        finally:
            await client.close()

        result_data = json.loads(clean_json_response(response_text))
        actual = extract_score(result_data)
        if actual is None:
            outcome.update(actual=None, passed=False, note="could not extract a score from the response")
        else:
            outcome.update(
                actual=actual, passed=abs(actual - expected) <= tolerance,
                input_tokens=in_tok, output_tokens=out_tok,
            )
    except Exception as exc:
        outcome.update(actual=None, passed=False, note=f"error: {exc}")

    return outcome


async def main_async(args) -> int:
    cases = load_reference_cases(args.audit_type, args.label)
    if not cases:
        print(f"No reference cases found (audit_type filter: {args.audit_type!r}, label filter: {args.label!r}).")
        return 1

    print(f"Running {len(cases)} reference case(s) against current prompts/ "
          f"({args.provider}/{args.model})...\n")

    results = await asyncio.gather(*[run_one_case(c, args.provider, args.model) for c in cases])

    all_passed = True
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        if not r["passed"]:
            all_passed = False
        actual_str = str(r["actual"]) if r["actual"] is not None else "?"
        line = (f"[{status}] {r['label']:<24} {r['audit_type']:<18} "
                f"expected={r['expected']:>3} actual={actual_str:>3} (tolerance +/-{r['tolerance']})")
        if r.get("note"):
            line += f"  -- {r['note']}"
        print(line)

    print(f"\n{'ALL PASSED' if all_passed else 'SOME FAILED'} "
          f"({sum(1 for r in results if r['passed'])}/{len(results)})")
    return 0 if all_passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audit-type", default=None, help="Only run cases for this audit type (e.g. GEO_AUDIT)")
    parser.add_argument("--label", default=None, help="Only run the single case with this label (e.g. geo_audit_low)")
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
