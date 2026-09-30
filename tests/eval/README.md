# Audit quality eval — Pasul 11

`docs/superpowers/plans/2026-09-30-next-steps.md`, Pasul 11: a baseline for
"did a prompt change make things better or worse", which nothing measured
before this.

## Running it

**Costs real money.** Not part of `pytest`, never runs automatically.

```bash
# Windows: run as a module (plain `python scripts/run_eval.py` can't resolve
# the core./api. imports) with UTF-8 output -- DirectAnalyzer prints emoji in
# its summary, which crashes on the default cp1252 console otherwise. Both
# gotchas hit the first real run of this script; not hypothetical.
set PYTHONUTF8=1
python -m scripts.run_eval

python -m scripts.run_eval --audit-type ACCESSIBILITY_AUDIT    # one audit type
python -m scripts.run_eval --fixture thin_content               # one fixture
python -m scripts.run_eval --provider anthropic --model claude-sonnet-4-6
```

Runs the real `core.direct_analyzer.run_direct_analysis` pipeline — the same
function `api/workers/audit_worker.py` calls in production — against each
fixture, and checks whether the model's raw output contains any of the
keywords for each of that fixture's known issues. Reports recall per
fixture and a mean, and saves a timestamped JSON to `tests/eval/results/`
(`ran_at`, provider, model, a hash of the exact prompt text used, and the
per-fixture matches/misses).

Matching is a crude case-insensitive substring search, on purpose — the
plan asks for keyword matching, not semantic grading. A keyword list is a
starting guess at how the model phrases a finding; widen it after a real
run shows the actual wording, don't assume it's complete from the start.

**Avoid short, bare-word keywords** — there is no word-boundary check, so a
keyword like `"thin"` matches inside "every**thin**g" or "no**thin**g" and
false-positives on almost any output. This actually happened while writing
`thin_content`'s fixture and was caught by its own test
(`tests/test_run_eval.py`), not noticed by eye. Prefer multi-word phrases.

Uses `claude-haiku-4-5-20251001` by default (the cheapest current model) —
override with `--model` for a real before/after comparison against whatever
model production actually uses.

Every real call is recorded in the real `cost_records` table exactly as a
production audit would (`source="audit"`, `website=None`) — an eval run's
spend shows up in `/costs`, just not separable there from real audits.

## Adding a fixture

```
tests/eval/fixtures/<name>/
  page.txt          required — what DirectAnalyzer actually reads as page content
  page.html         optional — original markup, needed only if the audit type
                     reads facts from it (TECHNICAL_SEO: JSON-LD; ACCESSIBILITY_AUDIT:
                     the axe sidecar keys off this file's path)
  raw_axe.json      optional — a real axe-core result (see core/axe_runner.py);
                     the harness summarizes it and writes the sidecar itself,
                     the same way the real pipeline does
  expected.yaml     required
```

`expected.yaml`:

```yaml
audit_type: TECHNICAL_SEO      # must be a real prompts/*.yaml audit type
website: null                 # a real domain only if you need live robots.txt/
                               # llms.txt facts fetched for it -- most fixtures don't
description: >
  What's actually wrong with this page and why, for a human reading this later.
issues:
  - id: missing_schema
    description: "No structured data anywhere on the page"
    keywords: ["no schema", "structured data", "json-ld"]
```

Before adding a fixture that needs a specific fact-injection path (schema
detection, axe results, crawl facts, robots.txt), read
`core/direct_analyzer.py`'s `_process_single_page` dispatch for that
`audit_type` first — matching the actual code path is the entire point, and
getting it wrong (e.g. expecting a fact injected from a live network fetch
this harness never performs) makes the recall number for that fixture
meaningless rather than merely low.

## Current fixtures

| Fixture | Audit type | Known issue |
|---|---|---|
| `low_contrast` | ACCESSIBILITY_AUDIT | `#ff6200` button fails a WCAG contrast criterion (real axe-core measurement) |
| `thin_content` | CONTENT_QUALITY | ~35 words, no specifics |
| `no_schema` | TECHNICAL_SEO | Zero JSON-LD anywhere on an otherwise normal page |

Three, not the plan's suggested 5–10 — each one here is verified against
the actual fact-injection code path it exercises, rather than assumed to
work. Widen this set following the same verification, not by guessing.
