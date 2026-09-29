"""
Deterministic accessibility checks with axe-core (Etapa 7 of
docs/IMPROVEMENTS_PLAN.md).

prompts/accessibility_audit.yaml asked an LLM to judge accessibility from page
TEXT. The prompt itself admits it "cannot evaluate ... computed color contrast
values", yet ACCESSIBILITY_AUDIT carries a 0.08 weight in the composite score,
so an unverifiable judgement moved every client's number. axe-core runs in the
real browser against the rendered DOM and measures what text cannot show.

Measured on ing.ro/persoane-fizice before this was written: page load 7.3 s,
axe.run 1.7 s, five rules violated. That timing is why axe runs while the
scraper already has the page open, rather than in a second pass that would
visit every client page again.

Two distinctions the output keeps, both from that real result:

  * WCAG failure vs best practice. Of the five rules, only color-contrast
    maps to a WCAG success criterion (1.4.3). The other four are axe
    best-practice rules. Reporting "5 WCAG violations" to a client would
    overstate it five-fold.
  * Impact vs conformance. label-title-only is impact "serious" and still
    only best practice. Severity and WCAG status are separate fields.

The engine is vendored (core/vendor/axe-core, version and licence recorded
there) rather than read from Screaming Frog's bundled copy, which moves with
every SF update.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

AXE_PATH = Path(__file__).parent / "vendor" / "axe-core" / "axe.min.js"
AXE_SIDECAR_SUFFIX = ".axe.json"

_EXAMPLES_PER_RULE = 3
_HTML_SNIPPET_CHARS = 160
_IMPACT_ORDER = ["critical", "serious", "moderate", "minor"]

_AXE_RUN_SCRIPT = """
const done = arguments[arguments.length - 1];
axe.run(document, {resultTypes: ['violations']})
  .then(r => done(JSON.parse(JSON.stringify({
      testEngine: r.testEngine,
      violations: r.violations,
      incomplete: r.incomplete.map(i => ({id: i.id, impact: i.impact, nodes: i.nodes.length}))
  }))))
  .catch(e => done({error: String(e)}));
"""

_axe_source: Optional[str] = None


def _source() -> str:
    global _axe_source
    if _axe_source is None:
        _axe_source = AXE_PATH.read_text(encoding="utf-8")
    return _axe_source


def run_axe(driver, timeout: int = 90) -> Optional[dict]:
    """
    Inject axe-core into the page the driver has open and run it.

    Returns the raw axe result, or None on any failure -- a page axe could not
    measure must read as "not measured", never as "no violations".
    """
    try:
        driver.execute_script(_source())
        driver.set_script_timeout(timeout)
        raw = driver.execute_async_script(_AXE_RUN_SCRIPT)
    except Exception as exc:
        logger.warning("axe-core run failed: %s", exc)
        return None
    if not isinstance(raw, dict) or raw.get("error"):
        logger.warning("axe-core returned an error: %s", (raw or {}).get("error"))
        return None
    return raw


def wcag_criteria(tags: List[str]) -> List[str]:
    """
    WCAG success criteria from axe tags: "wcag143" -> "1.4.3",
    "wcag1410" -> "1.4.10". Level tags ("wcag2aa", "wcag21a") are not
    criteria and are skipped, as are best-practice and non-WCAG tags.
    """
    out = []
    for tag in tags or []:
        if not tag.startswith("wcag"):
            continue
        digits = tag[4:]
        if not digits.isdigit() or len(digits) < 3:
            continue
        criterion = f"{digits[0]}.{digits[1]}.{digits[2:]}"
        if criterion not in out:
            out.append(criterion)
    return out


def _contrast(node: dict) -> Optional[dict]:
    for check in node.get("any") or []:
        data = check.get("data") or {}
        if isinstance(data, dict) and "contrastRatio" in data:
            return {
                "foreground": data.get("fgColor"),
                "background": data.get("bgColor"),
                "ratio": data.get("contrastRatio"),
                "required": data.get("expectedContrastRatio"),
                "font": data.get("fontSize"),
            }
    return None


def summarize(raw: dict, url: Optional[str] = None) -> dict:
    """
    The compact per-page record stored next to the HTML.

    A real raw result is ~38 KB; this keeps what a report needs -- which rules,
    how many elements, a few concrete examples with their selector, and the
    measured colours for contrast failures.
    """
    violations = []
    for v in raw.get("violations") or []:
        criteria = wcag_criteria(v.get("tags"))
        examples = []
        for node in (v.get("nodes") or [])[:_EXAMPLES_PER_RULE]:
            ex = {
                "target": " ".join(node.get("target") or [])[:200],
                "html": (node.get("html") or "")[:_HTML_SNIPPET_CHARS],
            }
            contrast = _contrast(node)
            if contrast:
                ex["contrast"] = contrast
            examples.append(ex)
        violations.append({
            "id": v.get("id"),
            "impact": v.get("impact"),
            "help": v.get("help"),
            "help_url": v.get("helpUrl"),
            "wcag": criteria,
            # Anything without a WCAG criterion is an axe best-practice rule:
            # worth fixing, but not a conformance failure.
            "is_wcag_failure": bool(criteria),
            "nodes": len(v.get("nodes") or []),
            "examples": examples,
        })

    violations.sort(key=lambda v: (not v["is_wcag_failure"],
                                   _IMPACT_ORDER.index(v["impact"]) if v["impact"] in _IMPACT_ORDER else 9))

    return {
        "url": url,
        "engine": "axe-core " + str((raw.get("testEngine") or {}).get("version", "?")),
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "wcag_failures": sum(1 for v in violations if v["is_wcag_failure"]),
        "best_practice_issues": sum(1 for v in violations if not v["is_wcag_failure"]),
        "incomplete": len(raw.get("incomplete") or []),
        "violations": violations,
    }


def axe_sidecar_path(html_path: str) -> str:
    return os.path.splitext(html_path)[0] + AXE_SIDECAR_SUFFIX


def write_axe_results(html_path: str, summary: dict) -> None:
    with open(axe_sidecar_path(html_path), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)


def load_axe_results(html_path: str) -> Optional[dict]:
    """None when the page was never measured -- which is not "passed"."""
    path = axe_sidecar_path(html_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read axe results at %s: %s", path, exc)
        return None


def format_axe_facts_block(summary: Optional[dict]) -> str:
    """Render axe results as ground truth for the accessibility prompt."""
    lines = ["=== AXE-CORE ACCESSIBILITY FACTS (measured in a real browser on the rendered page "
             "-- do NOT contradict these) ==="]

    if not summary:
        # Never render "not measured" as "no issues": that would hand an
        # unmeasured page a clean bill of accessibility health.
        lines.append(
            "no automated measurement for this page -- judge accessibility from the content "
            "alone, and do NOT claim any WCAG criterion passes or fails on measured grounds"
        )
        lines.append("=== END AXE FACTS -- page content follows below ===")
        return "\n".join(lines)

    lines.append(f"engine: {summary.get('engine')}")
    lines.append(
        f"WCAG success-criterion failures: {summary.get('wcag_failures', 0)} rule(s); "
        f"best-practice issues (NOT WCAG failures): {summary.get('best_practice_issues', 0)} rule(s)"
    )

    for v in summary.get("violations") or []:
        label = f"WCAG {', '.join(v['wcag'])}" if v["is_wcag_failure"] else "best practice, not a WCAG failure"
        lines.append(f"- {v['id']} [{v['impact']}; {label}]: {v['help']} -- {v['nodes']} element(s)")
        for ex in v.get("examples") or []:
            detail = f"    e.g. {ex['target']}"
            c = ex.get("contrast")
            if c:
                detail += (f" -- text {c['foreground']} on {c['background']}, "
                           f"contrast {c['ratio']}:1, required {c['required']}")
            lines.append(detail)

    if not summary.get("violations"):
        lines.append("no automated violations found -- note automated testing covers only part "
                     "of WCAG; keyboard use, meaning and reading order still need judgement")

    if summary.get("incomplete"):
        lines.append(f"{summary['incomplete']} rule(s) could not be decided automatically and need "
                     "manual review")

    lines.append("=== END AXE FACTS -- page content follows below ===")
    return "\n".join(lines)


DEFAULT_BACKFILL_CAP = 100


def backfill_axe(html_dir: str, sitemap_url: str, max_pages: Optional[int] = None) -> dict:
    """
    Measure pages that were scraped before accessibility was requested.

    The audit pipeline skips scraping entirely when HTML already exists --
    the normal case, since several audit types run on the same site -- so an
    accessibility audit on an existing site would otherwise never get axe
    results at all. This visits only pages that have stored HTML and no axe
    sidecar yet.

    Capped (AXE_MAX_PAGES, default 100): unlike measuring during a scrape,
    this loads each page again, ~9 s apiece measured, so 545 pages would be
    over an hour of requests against the client's site nobody asked for.
    Pages past the cap stay "not measured" and are reported as such.

    Pages are matched to files through the scraper's own URL -> filename rule,
    which reproduced all 157 filenames it was checked against.
    """
    import random
    import time

    from core.web_scraper import create_driver, fetch_sitemap_urls, safe_filename_stem

    cap = max_pages if max_pages is not None else int(os.getenv("AXE_MAX_PAGES", DEFAULT_BACKFILL_CAP))

    todo = []
    for entry in fetch_sitemap_urls(sitemap_url):
        html_path = os.path.join(html_dir, safe_filename_stem(entry.url) + ".html")
        if os.path.exists(html_path) and not os.path.exists(axe_sidecar_path(html_path)):
            todo.append((entry.url, html_path))

    result = {"candidates": len(todo), "measured": 0, "failed": 0,
              "skipped_over_cap": max(0, len(todo) - cap)}
    todo = todo[:cap]
    if not todo:
        return result

    driver = create_driver()
    try:
        for url, html_path in todo:
            try:
                driver.get(url)
                raw = run_axe(driver)
            except Exception as exc:
                logger.warning("axe backfill could not load %s: %s", url, exc)
                raw = None
            if raw is None:
                result["failed"] += 1
            else:
                write_axe_results(html_path, summarize(raw, url=url))
                result["measured"] += 1
            # Same politeness as the scraper's own delay between pages.
            time.sleep(random.uniform(1.0, 2.0))
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return result
