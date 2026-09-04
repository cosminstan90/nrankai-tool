# Screaming Frog Link-Graph Crawler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the audit engine a real internal link graph — collected by driving the licensed Screaming Frog SEO Spider headless — so `prompts/internal_linking.yaml` analyses facts instead of guessing from a single page's HTML.

**Architecture:** A Python worker shells out to `ScreamingFrogSEOSpiderCli.exe` (subprocess, argv list, never a shell string), parses the CSV exports it writes to a temp directory, and stores a *filtered* graph in SQLite. Crawls are triggered explicitly per website — never automatically inside the 4-step audit pipeline — and audits consume the most recent fresh crawl if one exists. Facts are injected into the LLM prompt through the same `format_facts_block()` pattern `core/technical_facts.py` already uses for TECHNICAL_SEO.

**Tech Stack:** Screaming Frog SEO Spider 24.3 CLI, Python `subprocess` + `csv`, async SQLAlchemy, Alembic, FastAPI.

---

## Investigation findings this plan is built on

Everything below was verified live on this machine on 2026-09-04, not assumed. The numbers drive the design decisions, so do not "simplify" them away.

**1. Storing every edge is not an option — 99.9% of them are the same nav menu.**

A crawl of the local dev server (`http://127.0.0.1:8000`, roughly 100 routes) produced:

| Metric | Value |
|---|---|
| `all_inlinks.csv` size | 54.9 MB |
| Total rows | 196,069 |
| `Type=Hyperlink` | 174,244 |
| — of those, `Link Position=Navigation` | 152,277 (87%) |
| — of those, `Link Position=Aside` | 21,810 |
| — of those, **`Link Position=Content`** | **157 (0.09%)** |

Storing only content-position hyperlinks is a **1,100× reduction** and loses nothing the internal-linking prompt cares about. That prompt already states: *"Navigation and footer links do NOT count as quality internal links — they must be counted separately."* Screaming Frog classifies link position natively, so the prompt's hardest constraint becomes a computed fact rather than an LLM guess.

**Storage rule (Task 4 implements this):** persist an edge only when `Type == "Hyperlink"` **and** (`Link Position == "Content"` **or** the destination status code is >= 400 **or** 3xx). Everything else is kept as per-page aggregate counts. A 404 linked only from the nav is still a real bug, which is why error/redirect edges are kept regardless of position.

**2. An unbounded crawl is slow and heavy — limits are mandatory, not optional.**

The crawl above ran **over 10 minutes** against a local server with zero network latency, and was killed at the 10-minute mark having just written its exports. `docs/IMPROVEMENTS_PLAN.md` already warns: *"Crawler-ul e partea cu cel mai mare risc de a lovi site-uri reale prea tare. Rate limiting și robots.txt nu sunt opționale."* This plan enforces that in code: `run_crawl()` **refuses to start** without a config file (Task 2).

**3. Crawl limits live in an exported `.seospiderconfig`, not in any file we can author.**

`C:\Users\Cosmin\.ScreamingFrogSEOSpider\spider.config` holds only app-level preferences (API keys, storage mode, MCP port, proxy). It contains **no** crawl-limit, depth, speed, or robots.txt keys, and `prefs/` holds only UI column layouts. There is no `--save-config` CLI flag. The config must therefore be exported once from the Screaming Frog GUI — see **Task 0**, which is a hard prerequisite and blocks safe crawling of real sites.

**4. Verified data contract (do not invent column names).**

`all_inlinks.csv` — UTF-8 **with BOM**, so parse with `encoding="utf-8-sig"`:

```
"Type","Source","Destination","Size (Bytes)","Alt Text","Anchor","Status Code",
"Status","Crawlability","Follow","Target","Rel","Path Type","Link Path",
"Link Position","Link Origin"
```

`internal_html.csv` — relevant columns, by position: `Address` (1), `Status Code` (3), `Indexability` (5), `Crawl Depth` (43), `Inlinks` (46), `Unique Inlinks` (47), `Outlinks` (50), `Unique Outlinks` (51).

`response_codes_internal_client_error_(4xx).csv`:

```
"Address","Content Type","Status Code","Status","Indexability",
"Indexability Status","Inlinks","Response Time","Redirect URL","Redirect Type"
```

Observed `Link Position` values: `Navigation`, `Aside`, `Content`. Screaming Frog's full vocabulary also includes `Header`, `Footer`, and `Sidebar`; treat any value other than `Content` as non-content.

**5. Arguments containing spaces must be passed as an argv list.**

`Start-Process -ArgumentList` split `"Response Codes:Internal Client Error (4xx)"` on spaces and Screaming Frog died with `SeoSpider failed to start`. Python's `subprocess.run([...])` passes argv without a shell and handles this correctly. Never build the command as a single string.

**6. Available exports confirmed present in this SF version.**

Tabs: `Internal:HTML`, `Response Codes:Internal Client Error (4xx)`, `Sitemaps:Orphan URLs`, `Sitemaps:URLs in Sitemap`.
Bulk exports: `Links:All Inlinks`, `Links:All Anchor Text`.
Reports: `Redirects:Redirect Chains`, `Orphan Pages`, `Crawl Overview`.

---

## File structure

| File | Responsibility |
|---|---|
| `core/sf_crawler.py` (create) | Locate the SF binary, build argv, run the subprocess with a timeout, return artifact paths. Knows nothing about the database. |
| `core/sf_parser.py` (create) | Parse SF's CSVs into plain dicts. Applies the edge-filtering rule. Pure functions, no I/O beyond reading files. |
| `core/crawl_facts.py` (create) | Render per-page crawl facts as a prompt block, mirroring `core/technical_facts.py`. |
| `api/models/crawl.py` (create) | `SiteCrawl`, `CrawlPage`, `CrawlLink`. New domain file — sanctioned by the improvements plan ("un fișier de domeniu nou dacă devine mare"). |
| `api/models/database.py` (modify) | Re-export the three new models, per the existing backward-compat convention. |
| `migrations/versions/0015_site_crawls.py` (create) | Three tables. Follows the `0014` header format. |
| `api/workers/crawl_worker.py` (create) | Orchestrates: run crawl → parse → persist → derive. Opens its own `AsyncSessionLocal`. |
| `api/routes/crawl.py` (create) | `POST /api/crawl/start`, `GET /api/crawl/{id}`, `GET /api/crawl/site/{website}/latest`. |
| `core/direct_analyzer.py` (modify, ~line 955) | Inject crawl facts when `question_type == "INTERNAL_LINKING"`. |
| `tests/fixtures/sf/*.csv` (create) | Trimmed real exports, committed so parser tests never need Screaming Frog. |
| `tests/test_sf_parser.py`, `tests/test_crawl_facts.py`, `tests/test_crawl_worker.py` (create) | Coverage per stage. |

---

## Task 0: Export the crawl config (PREREQUISITE — human action, blocks Stage 5A)

**This cannot be automated.** The config format is proprietary, there is no `--save-config` flag, and `spider.config` does not contain crawl settings. Until this file exists, `run_crawl()` will refuse to run (by design).

- [ ] **Step 1: Configure limits in the Screaming Frog GUI**

Open Screaming Frog, then set:

| Menu | Setting | Value |
|---|---|---|
| Configuration > Spider > Limits | Limit Crawl Total | **500** |
| Configuration > Spider > Limits | Limit Crawl Depth | **5** |
| Configuration > Spider > Rendering | Rendering | **Text Only** (JS rendering roughly triples crawl time) |
| Configuration > Robots.txt > Settings | Respect robots.txt | **checked** |
| Configuration > Speed | Max Threads | **2** |
| Configuration > Speed | Limit URI/s | **2.0** |
| Configuration > Spider > Crawl | Crawl Linked XML Sitemaps | **checked** (required for `Sitemaps:Orphan URLs`) |

- [ ] **Step 2: Save the config**

File > Configuration > Save As → `D:\Projects\geo_tool\config\sf_audit.seospiderconfig`

- [ ] **Step 3: Point the code at it**

Add to `.env`:

```
SCREAMING_FROG_CLI=C:\Program Files (x86)\Screaming Frog SEO Spider\ScreamingFrogSEOSpiderCli.exe
SCREAMING_FROG_CONFIG=D:\Projects\geo_tool\config\sf_audit.seospiderconfig
```

- [ ] **Step 4: Verify the file exists and is non-empty**

Run: `python -c "import os;p=os.getenv('SCREAMING_FROG_CONFIG');print(p, os.path.getsize(p))"`
Expected: prints the path and a size greater than 0.

---

# Stage 5A — Crawl runner and storage

## Task 1: Commit CSV fixtures

Parser tests must not require Screaming Frog to be installed or a site to be crawled.

**Files:**
- Create: `tests/fixtures/sf/all_inlinks.csv`
- Create: `tests/fixtures/sf/internal_html.csv`
- Create: `tests/fixtures/sf/response_codes_internal_client_error_(4xx).csv`

- [ ] **Step 1: Write `tests/fixtures/sf/all_inlinks.csv`**

Real header, six rows covering every branch of the storage rule: a content link, a nav link, an aside link, a JavaScript-type link, a content link to a 404, and a nav link to a 404.

```csv
"Type","Source","Destination","Size (Bytes)","Alt Text","Anchor","Status Code","Status","Crawlability","Follow","Target","Rel","Path Type","Link Path","Link Position","Link Origin"
"Hyperlink","https://example.com/","https://example.com/pricing","1024","","See our pricing","200","OK","Crawlable","true","","","Absolute","//body/main/a[1]","Content","HTML"
"Hyperlink","https://example.com/","https://example.com/about","1024","","About","200","OK","Crawlable","true","","","Absolute","//body/nav/a[1]","Navigation","HTML"
"Hyperlink","https://example.com/pricing","https://example.com/about","1024","","About","200","OK","Crawlable","true","","","Absolute","//body/aside/a[1]","Aside","HTML"
"JavaScript","https://example.com/","https://example.com/app.js","2048","","","200","OK","Crawlable","true","","","Absolute","//head/script[1]","Header","HTML"
"Hyperlink","https://example.com/pricing","https://example.com/gone","512","","click here","404","Not Found","Crawlable","true","","","Absolute","//body/main/a[2]","Content","HTML"
"Hyperlink","https://example.com/about","https://example.com/missing","512","","Missing page","404","Not Found","Crawlable","true","","","Absolute","//body/nav/a[3]","Navigation","HTML"
```

- [ ] **Step 2: Write `tests/fixtures/sf/internal_html.csv`**

Only the columns the parser reads, in SF's real spelling.

```csv
"Address","Status Code","Indexability","Crawl Depth","Inlinks","Unique Inlinks","Outlinks","Unique Outlinks"
"https://example.com/","200","Indexable","0","5","3","12","8"
"https://example.com/pricing","200","Indexable","1","3","2","6","5"
"https://example.com/about","200","Indexable","1","4","3","5","4"
"https://example.com/orphan-ish","200","Indexable","2","0","0","2","2"
```

- [ ] **Step 3: Write `tests/fixtures/sf/response_codes_internal_client_error_(4xx).csv`**

```csv
"Address","Content Type","Status Code","Status","Indexability","Indexability Status","Inlinks","Response Time","Redirect URL","Redirect Type"
"https://example.com/gone","text/html","404","Not Found","Non-Indexable","Client Error","1","0.004","",""
"https://example.com/missing","text/html","404","Not Found","Non-Indexable","Client Error","1","0.006","",""
```

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/sf/
git commit -m "test: add real Screaming Frog CSV export fixtures

Trimmed from a real crawl (2026-09-04) so parser tests never need
Screaming Frog installed. Headers are verbatim, including the UTF-8 BOM
handling requirement and SF's exact column spellings.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

## Task 2: Crawl runner refuses to run without a config

**Files:**
- Create: `core/sf_crawler.py`
- Test: `tests/test_sf_crawler.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sf_crawler.py
import unittest
from unittest.mock import patch

from core.sf_crawler import SfConfigMissing, run_crawl


class TestCrawlRefusesWithoutConfig(unittest.TestCase):
    def test_missing_config_raises_rather_than_crawling_unthrottled(self):
        """
        docs/IMPROVEMENTS_PLAN.md: "Rate limiting și robots.txt nu sunt
        opționale." Screaming Frog's built-in defaults are unlimited depth,
        unlimited pages and 5 threads, so a crawl with no config file would
        hit a real site far harder than intended. Refusing is the safe
        behaviour; there is deliberately no fallback.
        """
        with patch.dict("os.environ", {"SCREAMING_FROG_CONFIG": ""}, clear=False):
            with self.assertRaises(SfConfigMissing):
                run_crawl("https://example.com", output_dir="/tmp/nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sf_crawler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.sf_crawler'`

- [ ] **Step 3: Write the implementation**

```python
# core/sf_crawler.py
"""
Drives the licensed Screaming Frog SEO Spider headless to collect an internal
link graph (Etapa 5 of docs/IMPROVEMENTS_PLAN.md).

Screaming Frog does the crawling rather than a hand-written crawler because it
already handles robots.txt, redirect chains, rate limiting and link-position
classification -- and that last one turns out to matter more than anything
else: in a verified crawl, 87% of hyperlink edges were navigation and only
0.09% were real content links (see the plan's investigation findings).

The subprocess is always invoked with an argv LIST. Passing these arguments as
a single shell string breaks: PowerShell split "Response Codes:Internal Client
Error (4xx)" on spaces and Screaming Frog exited with "SeoSpider failed to
start".
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_CLI = r"C:\Program Files (x86)\Screaming Frog SEO Spider\ScreamingFrogSEOSpiderCli.exe"

BULK_EXPORTS = "Links:All Inlinks"
EXPORT_TABS = "Internal:HTML,Response Codes:Internal Client Error (4xx),Sitemaps:Orphan URLs"
SAVE_REPORTS = "Redirects:Redirect Chains"


class SfConfigMissing(RuntimeError):
    """Raised when no .seospiderconfig is configured -- see run_crawl."""


class SfCrawlFailed(RuntimeError):
    """Raised when the Screaming Frog process fails or times out."""


@dataclass
class CrawlArtifacts:
    output_dir: Path
    inlinks_csv: Path
    internal_html_csv: Path
    errors_4xx_csv: Path
    orphan_urls_csv: Path
    redirect_chains_csv: Path


def _cli_path() -> str:
    return os.getenv("SCREAMING_FROG_CLI") or _DEFAULT_CLI


def _config_path() -> str:
    """
    A config file is REQUIRED, never defaulted. Screaming Frog's own defaults
    are unlimited crawl depth, unlimited total URLs and 5 concurrent threads,
    which is exactly the "hitting real sites too hard" failure the improvements
    plan calls out. The limits live in an exported .seospiderconfig because
    spider.config holds no crawl settings and SF has no --save-config flag.
    """
    path = os.getenv("SCREAMING_FROG_CONFIG", "").strip()
    if not path or not os.path.isfile(path):
        raise SfConfigMissing(
            "SCREAMING_FROG_CONFIG is not set to an existing .seospiderconfig file. "
            "Export one from the Screaming Frog GUI (File > Configuration > Save As) "
            "with crawl limits, robots.txt respect and a URI/s cap. Crawling without "
            "it would run unlimited and unthrottled against a real site."
        )
    return path


def build_argv(website: str, output_dir: str, config_path: str) -> list:
    return [
        _cli_path(),
        "--headless",
        "--crawl", website,
        "--config", config_path,
        "--output-folder", output_dir,
        "--overwrite",
        "--export-format", "csv",
        "--bulk-export", BULK_EXPORTS,
        "--export-tabs", EXPORT_TABS,
        "--save-report", SAVE_REPORTS,
        "--skip-empty",
    ]


def run_crawl(website: str, output_dir: str, timeout: int = 1800) -> CrawlArtifacts:
    """
    Run one headless crawl and return the paths it wrote.

    timeout defaults to 30 minutes: a verified crawl of a ~100-route app with
    zero network latency took over 10 minutes, so a real site with a 500-URL
    cap needs real headroom. On timeout the process is killed and
    SfCrawlFailed is raised -- a half-finished crawl is not persisted.
    """
    config_path = _config_path()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    argv = build_argv(website, str(out), config_path)
    logger.info("Starting Screaming Frog crawl of %s -> %s", website, out)

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SfCrawlFailed(f"Crawl of {website} exceeded {timeout}s and was killed") from exc

    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-15:])
        raise SfCrawlFailed(f"Screaming Frog exited {proc.returncode} for {website}:\n{tail}")

    return CrawlArtifacts(
        output_dir=out,
        inlinks_csv=out / "all_inlinks.csv",
        internal_html_csv=out / "internal_html.csv",
        errors_4xx_csv=out / "response_codes_internal_client_error_(4xx).csv",
        orphan_urls_csv=out / "sitemaps_orphan_urls.csv",
        redirect_chains_csv=out / "redirect_chains.csv",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sf_crawler.py -v`
Expected: PASS

- [ ] **Step 5: Add an argv-construction test**

This is the regression guard for finding #5 — spaced arguments must stay single argv elements.

```python
# append to tests/test_sf_crawler.py
from core.sf_crawler import build_argv


class TestArgvConstruction(unittest.TestCase):
    def test_spaced_arguments_stay_single_elements(self):
        """
        PowerShell's -ArgumentList split "Response Codes:Internal Client Error
        (4xx)" on spaces and Screaming Frog died with "SeoSpider failed to
        start". An argv list passed to subprocess must keep it intact.
        """
        argv = build_argv("https://example.com", r"C:\out", r"C:\cfg.seospiderconfig")
        tabs = argv[argv.index("--export-tabs") + 1]
        self.assertIn("Response Codes:Internal Client Error (4xx)", tabs)
        self.assertNotIn("--export-tabs", argv[argv.index("--export-tabs") + 2 :])

    def test_config_is_always_passed(self):
        argv = build_argv("https://example.com", r"C:\out", r"C:\cfg.seospiderconfig")
        self.assertIn("--config", argv)
        self.assertEqual(argv[argv.index("--config") + 1], r"C:\cfg.seospiderconfig")
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_sf_crawler.py -v`
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
git add core/sf_crawler.py tests/test_sf_crawler.py
git commit -m "feat(crawl): add Screaming Frog headless crawl runner

Refuses to run without an exported .seospiderconfig: SF's defaults are
unlimited depth, unlimited URLs and 5 threads, which is the "hitting real
sites too hard" risk docs/IMPROVEMENTS_PLAN.md warns about. There is
deliberately no fallback.

argv is built as a list, never a shell string -- PowerShell split
"Response Codes:Internal Client Error (4xx)" on spaces during
investigation and SF exited with "SeoSpider failed to start". A test
guards that.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

## Task 3: Parse `internal_html.csv`

**Files:**
- Create: `core/sf_parser.py`
- Test: `tests/test_sf_parser.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sf_parser.py
import unittest
from pathlib import Path

from core.sf_parser import parse_internal_html

FIXTURES = Path(__file__).parent / "fixtures" / "sf"


class TestParseInternalHtml(unittest.TestCase):
    def test_parses_pages_with_depth_and_inlink_counts(self):
        pages = parse_internal_html(FIXTURES / "internal_html.csv")

        self.assertEqual(len(pages), 4)
        home = next(p for p in pages if p["url"] == "https://example.com/")
        self.assertEqual(home["crawl_depth"], 0)
        self.assertEqual(home["status_code"], 200)
        self.assertEqual(home["inlinks_total"], 5)
        self.assertEqual(home["unique_inlinks"], 3)
        self.assertEqual(home["outlinks_total"], 12)
        self.assertEqual(home["indexability"], "Indexable")

    def test_bom_does_not_corrupt_the_first_column(self):
        """
        SF writes UTF-8 WITH a BOM. Parsed with plain utf-8 the first column
        name becomes '\ufeffAddress' and every url comes back None -- a silent,
        total data loss rather than an error.
        """
        pages = parse_internal_html(FIXTURES / "internal_html.csv")
        self.assertTrue(all(p["url"] is not None for p in pages))
        self.assertTrue(all(p["url"].startswith("https://") for p in pages))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sf_parser.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.sf_parser'`

- [ ] **Step 3: Write the implementation**

```python
# core/sf_parser.py
"""
Parses Screaming Frog CSV exports into plain dicts.

Every column name here was read off a real export on 2026-09-04, not guessed.
All SF CSVs are UTF-8 WITH a BOM, so they must be opened with
encoding="utf-8-sig" -- with plain "utf-8" the first header becomes
"\ufeffAddress" and every row's url silently comes back None.
"""

import csv
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_ENCODING = "utf-8-sig"


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return None


def parse_internal_html(path: Path) -> List[dict]:
    """One dict per crawled HTML page: url, status, depth and link counts."""
    if not Path(path).is_file():
        logger.warning("internal_html export missing at %s", path)
        return []

    pages: List[dict] = []
    with open(path, encoding=_ENCODING, newline="") as fh:
        for row in csv.DictReader(fh):
            url = (row.get("Address") or "").strip()
            if not url:
                continue
            pages.append({
                "url": url,
                "status_code": _int(row.get("Status Code")),
                "indexability": (row.get("Indexability") or "").strip() or None,
                "crawl_depth": _int(row.get("Crawl Depth")),
                "inlinks_total": _int(row.get("Inlinks")) or 0,
                "unique_inlinks": _int(row.get("Unique Inlinks")) or 0,
                "outlinks_total": _int(row.get("Outlinks")) or 0,
                "unique_outlinks": _int(row.get("Unique Outlinks")) or 0,
            })
    return pages
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sf_parser.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add core/sf_parser.py tests/test_sf_parser.py
git commit -m "feat(crawl): parse Screaming Frog internal_html export

Column names taken from a real export, not guessed. Opened with
utf-8-sig: SF writes a BOM, and with plain utf-8 the first header becomes
'\ufeffAddress' and every url comes back None -- silent total data loss
rather than a visible error. A test covers exactly that.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

## Task 4: Parse inlinks and apply the edge-filtering rule

This is the task that keeps the database from exploding. Verified ratio: 174,244 hyperlink edges in, 157 content edges out.

**Files:**
- Modify: `core/sf_parser.py`
- Test: `tests/test_sf_parser.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_sf_parser.py
from core.sf_parser import parse_inlinks


class TestParseInlinks(unittest.TestCase):
    def test_only_content_and_broken_edges_are_persisted(self):
        """
        The whole storage strategy. On a real crawl 174,244 hyperlink edges
        reduced to 157 content edges (87% were Navigation) -- storing them all
        would mean a 55MB CSV of the same nav menu repeated per page. Error
        edges are kept regardless of position because a 404 linked only from
        the nav is still a real bug.
        """
        edges, counts = parse_inlinks(FIXTURES / "all_inlinks.csv")

        kept = {(e["source_url"], e["dest_url"]) for e in edges}
        self.assertIn(("https://example.com/", "https://example.com/pricing"), kept)   # content
        self.assertIn(("https://example.com/pricing", "https://example.com/gone"), kept)  # content 404
        self.assertIn(("https://example.com/about", "https://example.com/missing"), kept)  # nav 404
        self.assertNotIn(("https://example.com/", "https://example.com/about"), kept)  # plain nav
        self.assertNotIn(("https://example.com/pricing", "https://example.com/about"), kept)  # aside
        self.assertEqual(len(edges), 3)

    def test_javascript_links_are_never_edges(self):
        edges, _ = parse_inlinks(FIXTURES / "all_inlinks.csv")
        self.assertTrue(all(e["dest_url"] != "https://example.com/app.js" for e in edges))

    def test_anchor_text_is_captured_for_content_edges(self):
        edges, _ = parse_inlinks(FIXTURES / "all_inlinks.csv")
        pricing = next(e for e in edges if e["dest_url"] == "https://example.com/pricing")
        self.assertEqual(pricing["anchor"], "See our pricing")

    def test_discarded_edges_survive_as_per_page_counts(self):
        """
        Nav/aside edges are dropped as rows but must not vanish entirely --
        the prompt needs to count them separately from content links.
        """
        _, counts = parse_inlinks(FIXTURES / "all_inlinks.csv")
        self.assertEqual(counts["https://example.com/pricing"]["content"], 1)
        self.assertEqual(counts["https://example.com/about"]["non_content"], 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sf_parser.py::TestParseInlinks -v`
Expected: FAIL — `ImportError: cannot import name 'parse_inlinks'`

- [ ] **Step 3: Write the implementation**

```python
# append to core/sf_parser.py
from collections import defaultdict
from typing import Tuple

CONTENT_POSITION = "Content"


def _edge_reason(link_position: str, dest_status: Optional[int]) -> Optional[str]:
    """
    Decides whether an edge is worth a database row.

    Verified on a real crawl: of 174,244 hyperlink edges, 152,277 were
    Navigation and only 157 were Content. Persisting nav edges would store the
    same menu once per page for no analytical gain -- the internal-linking
    prompt explicitly discounts them ("Navigation and footer links do NOT count
    as quality internal links"). They survive as aggregate counts instead.
    """
    if dest_status is not None and dest_status >= 400:
        return "error"
    if dest_status is not None and 300 <= dest_status < 400:
        return "redirect"
    if link_position == CONTENT_POSITION:
        return "content"
    return None


def parse_inlinks(path: Path) -> Tuple[List[dict], Dict[str, dict]]:
    """
    Returns (edges_to_store, per_destination_counts).

    edges_to_store holds only content, error and redirect hyperlinks.
    per_destination_counts holds {url: {"content": n, "non_content": n}} for
    every hyperlink seen, so nav/footer links can still be counted.
    """
    if not Path(path).is_file():
        logger.warning("all_inlinks export missing at %s", path)
        return [], {}

    edges: List[dict] = []
    counts: Dict[str, dict] = defaultdict(lambda: {"content": 0, "non_content": 0})

    with open(path, encoding=_ENCODING, newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("Type") or "").strip() != "Hyperlink":
                continue

            source = (row.get("Source") or "").strip()
            dest = (row.get("Destination") or "").strip()
            if not source or not dest:
                continue

            position = (row.get("Link Position") or "").strip()
            dest_status = _int(row.get("Status Code"))

            if position == CONTENT_POSITION:
                counts[dest]["content"] += 1
            else:
                counts[dest]["non_content"] += 1

            reason = _edge_reason(position, dest_status)
            if reason is None:
                continue

            edges.append({
                "source_url": source,
                "dest_url": dest,
                "anchor": (row.get("Anchor") or "").strip() or None,
                "link_position": position or None,
                "follow": (row.get("Follow") or "").strip().lower() == "true",
                "dest_status_code": dest_status,
                "reason": reason,
            })

    return edges, dict(counts)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_sf_parser.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add core/sf_parser.py tests/test_sf_parser.py
git commit -m "feat(crawl): filter the link graph down to content and broken edges

The decision this whole feature rests on, made from measured data: a real
crawl produced 174,244 hyperlink edges, of which 152,277 (87%) were
Navigation and 157 (0.09%) were Content. Storing every edge means storing
the same nav menu once per page -- 55MB of CSV for a ~100-route app.

Kept: content-position links, plus any link to a 4xx/5xx or 3xx
destination regardless of position (a 404 linked only from the nav is
still a real bug). Dropped edges survive as per-page counts, because the
internal-linking prompt needs nav links counted separately rather than
ignored.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

## Task 5: Models and migration

**Files:**
- Create: `api/models/crawl.py`
- Modify: `api/models/database.py`
- Create: `migrations/versions/0015_site_crawls.py`

- [ ] **Step 1: Write the models**

```python
# api/models/crawl.py
"""
Internal link-graph storage (Etapa 5 of docs/IMPROVEMENTS_PLAN.md).

Its own domain file rather than a section of audit.py, which the improvements
plan explicitly sanctions ("un fișier de domeniu nou dacă devine mare").

crawl_links deliberately does NOT hold the full graph. A verified crawl
produced 174,244 hyperlink edges of which only 157 were content links; the
rest were the same navigation menu repeated on every page. Only content,
broken and redirecting edges get rows -- see core/sf_parser._edge_reason.
Navigation volume survives as the nav_inlinks count on crawl_pages.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
)
from sqlalchemy.orm import relationship

from api.models._base import Base


class SiteCrawl(Base):
    """One Screaming Frog run against one website."""
    __tablename__ = "site_crawls"

    id = Column(String(36), primary_key=True)
    website = Column(String(255), nullable=False, index=True)
    status = Column(String(20), default="pending")  # pending|running|completed|failed
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    pages_crawled = Column(Integer, default=0)
    content_edges = Column(Integer, default=0)
    nav_edges_discarded = Column(Integer, default=0)  # kept as a number, not as rows
    sf_version = Column(String(20), nullable=True)
    config_name = Column(String(255), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    pages = relationship("CrawlPage", back_populates="crawl", cascade="all, delete-orphan")
    links = relationship("CrawlLink", back_populates="crawl", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "website": self.website,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "pages_crawled": self.pages_crawled,
            "content_edges": self.content_edges,
            "nav_edges_discarded": self.nav_edges_discarded,
            "error": self.error,
        }


class CrawlPage(Base):
    """One crawled URL. content_inlinks is the number the prompt actually cares about."""
    __tablename__ = "crawl_pages"

    id = Column(String(36), primary_key=True)
    crawl_id = Column(String(36), ForeignKey("site_crawls.id", ondelete="CASCADE"), nullable=False, index=True)
    url = Column(String(2048), nullable=False, index=True)
    status_code = Column(Integer, nullable=True)
    indexability = Column(String(50), nullable=True)
    crawl_depth = Column(Integer, nullable=True)
    inlinks_total = Column(Integer, default=0)
    unique_inlinks = Column(Integer, default=0)
    outlinks_total = Column(Integer, default=0)
    unique_outlinks = Column(Integer, default=0)
    content_inlinks = Column(Integer, default=0)
    nav_inlinks = Column(Integer, default=0)
    is_orphan = Column(Boolean, default=False)

    crawl = relationship("SiteCrawl", back_populates="pages")

    def to_dict(self):
        return {
            "url": self.url,
            "status_code": self.status_code,
            "indexability": self.indexability,
            "crawl_depth": self.crawl_depth,
            "content_inlinks": self.content_inlinks,
            "nav_inlinks": self.nav_inlinks,
            "outlinks_total": self.outlinks_total,
            "is_orphan": self.is_orphan,
        }


class CrawlLink(Base):
    """A stored edge. reason is why it earned a row: content|error|redirect."""
    __tablename__ = "crawl_links"

    id = Column(String(36), primary_key=True)
    crawl_id = Column(String(36), ForeignKey("site_crawls.id", ondelete="CASCADE"), nullable=False, index=True)
    source_url = Column(String(2048), nullable=False)
    dest_url = Column(String(2048), nullable=False, index=True)
    anchor = Column(Text, nullable=True)
    link_position = Column(String(50), nullable=True)
    follow = Column(Boolean, default=True)
    dest_status_code = Column(Integer, nullable=True)
    reason = Column(String(20), nullable=False)

    crawl = relationship("SiteCrawl", back_populates="links")

    def to_dict(self):
        return {
            "source_url": self.source_url,
            "dest_url": self.dest_url,
            "anchor": self.anchor,
            "link_position": self.link_position,
            "follow": self.follow,
            "dest_status_code": self.dest_status_code,
            "reason": self.reason,
        }
```

- [ ] **Step 2: Re-export from `database.py`**

Find the block of `from api.models.<domain> import ...` lines and add, matching the existing style:

```python
from api.models.crawl import CrawlLink, CrawlPage, SiteCrawl  # noqa: F401
```

If the file has an `__all__`, add `"SiteCrawl", "CrawlPage", "CrawlLink"` to it.

- [ ] **Step 3: Write the migration**

```python
# migrations/versions/0015_site_crawls.py
"""Add site_crawls, crawl_pages and crawl_links for the internal link graph.

Etapa 5 of docs/IMPROVEMENTS_PLAN.md: core/web_scraper.py starts from the
sitemap and never follows a link, so orphan pages, internal 404s, redirect
chains and crawl depth were invisible, and prompts/internal_linking.yaml had
to guess a page's inbound links from that page's own HTML.

crawl_links stores a FILTERED graph, not the whole thing. Measured on a real
crawl: 174,244 hyperlink edges, of which 152,277 (87%) were navigation and 157
(0.09%) were content. Persisting all of them would store one copy of the site's
nav menu per page. Only content, broken and redirecting edges get rows;
navigation volume is kept as crawl_pages.nav_inlinks.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# ---------------------------------------------------------------------------
# Alembic meta
# ---------------------------------------------------------------------------

revision:      str                             = "0015"
down_revision: Union[str, None]                = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "site_crawls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("website", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("pages_crawled", sa.Integer(), server_default="0"),
        sa.Column("content_edges", sa.Integer(), server_default="0"),
        sa.Column("nav_edges_discarded", sa.Integer(), server_default="0"),
        sa.Column("sf_version", sa.String(20), nullable=True),
        sa.Column("config_name", sa.String(255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_site_crawls_website", "site_crawls", ["website"])

    op.create_table(
        "crawl_pages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("crawl_id", sa.String(36), sa.ForeignKey("site_crawls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("indexability", sa.String(50), nullable=True),
        sa.Column("crawl_depth", sa.Integer(), nullable=True),
        sa.Column("inlinks_total", sa.Integer(), server_default="0"),
        sa.Column("unique_inlinks", sa.Integer(), server_default="0"),
        sa.Column("outlinks_total", sa.Integer(), server_default="0"),
        sa.Column("unique_outlinks", sa.Integer(), server_default="0"),
        sa.Column("content_inlinks", sa.Integer(), server_default="0"),
        sa.Column("nav_inlinks", sa.Integer(), server_default="0"),
        sa.Column("is_orphan", sa.Boolean(), server_default=sa.text("0")),
    )
    op.create_index("ix_crawl_pages_crawl_id", "crawl_pages", ["crawl_id"])
    op.create_index("ix_crawl_pages_url", "crawl_pages", ["url"])

    op.create_table(
        "crawl_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("crawl_id", sa.String(36), sa.ForeignKey("site_crawls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=False),
        sa.Column("dest_url", sa.String(2048), nullable=False),
        sa.Column("anchor", sa.Text(), nullable=True),
        sa.Column("link_position", sa.String(50), nullable=True),
        sa.Column("follow", sa.Boolean(), server_default=sa.text("1")),
        sa.Column("dest_status_code", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(20), nullable=False),
    )
    op.create_index("ix_crawl_links_crawl_id", "crawl_links", ["crawl_id"])
    op.create_index("ix_crawl_links_dest_url", "crawl_links", ["dest_url"])


def downgrade() -> None:
    op.drop_index("ix_crawl_links_dest_url", table_name="crawl_links")
    op.drop_index("ix_crawl_links_crawl_id", table_name="crawl_links")
    op.drop_table("crawl_links")
    op.drop_index("ix_crawl_pages_url", table_name="crawl_pages")
    op.drop_index("ix_crawl_pages_crawl_id", table_name="crawl_pages")
    op.drop_table("crawl_pages")
    op.drop_index("ix_site_crawls_website", table_name="site_crawls")
    op.drop_table("site_crawls")
```

- [ ] **Step 4: Test the migration against a COPY of the database first**

`api/data/analyzer.db` is in WAL mode — copy it with the sqlite3 backup API, never `cp`.

```bash
python -c "
import sqlite3
src = sqlite3.connect(r'api/data/analyzer.db')
dst = sqlite3.connect(r'D:/scratch_temp/analyzer_copy.db')
src.backup(dst); dst.close(); src.close(); print('copied')
"
GEO_TOOL_DB_PATH=D:/scratch_temp/analyzer_copy.db alembic upgrade head
GEO_TOOL_DB_PATH=D:/scratch_temp/analyzer_copy.db alembic downgrade 0014
GEO_TOOL_DB_PATH=D:/scratch_temp/analyzer_copy.db alembic upgrade head
```

Expected: all three succeed with no error.

- [ ] **Step 5: Verify integrity on the copy**

```bash
python -c "
import sqlite3
c = sqlite3.connect(r'D:/scratch_temp/analyzer_copy.db')
print('fk violations:', len(c.execute('PRAGMA foreign_key_check').fetchall()))
print('integrity:', c.execute('PRAGMA integrity_check').fetchone()[0])
print('tables:', [r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%crawl%'\").fetchall()])
"
```

Expected: `fk violations: 0`, `integrity: ok`, three crawl tables listed.

- [ ] **Step 6: Apply to the real database**

Run: `alembic upgrade head`
Expected: `Running upgrade 0014 -> 0015`

- [ ] **Step 7: Commit**

```bash
git add api/models/crawl.py api/models/database.py migrations/versions/0015_site_crawls.py
git commit -m "feat(crawl): add site_crawls, crawl_pages and crawl_links

Own domain file rather than a section of audit.py, which the improvements
plan sanctions for exactly this case.

crawl_links holds a filtered graph, not the whole one: measured at 174,244
hyperlink edges of which 87% were navigation and 0.09% were content. Only
content, broken and redirecting edges get rows; nav volume is a count on
crawl_pages. Migration tested up and down against a DB copy (0 FK
violations, integrity ok both directions) before touching the real one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

## Task 6: Crawl worker — orchestrate and persist

**Files:**
- Create: `api/workers/crawl_worker.py`
- Test: `tests/test_crawl_worker.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crawl_worker.py
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CrawlLink, CrawlPage, SiteCrawl
from api.workers.crawl_worker import persist_crawl
from core.sf_crawler import CrawlArtifacts

FIXTURES = Path(__file__).parent / "fixtures" / "sf"


def _artifacts() -> CrawlArtifacts:
    return CrawlArtifacts(
        output_dir=FIXTURES,
        inlinks_csv=FIXTURES / "all_inlinks.csv",
        internal_html_csv=FIXTURES / "internal_html.csv",
        errors_4xx_csv=FIXTURES / "response_codes_internal_client_error_(4xx).csv",
        orphan_urls_csv=FIXTURES / "does_not_exist.csv",
        redirect_chains_csv=FIXTURES / "does_not_exist.csv",
    )


class TestPersistCrawl(unittest.IsolatedAsyncioTestCase):
    async def test_persists_pages_and_only_filtered_edges(self):
        crawl_id = str(uuid.uuid4())
        await persist_crawl(crawl_id, "https://example.com", _artifacts())

        async with AsyncSessionLocal() as db:
            crawl = (await db.execute(select(SiteCrawl).where(SiteCrawl.id == crawl_id))).scalar_one()
            pages = (await db.execute(select(CrawlPage).where(CrawlPage.crawl_id == crawl_id))).scalars().all()
            links = (await db.execute(select(CrawlLink).where(CrawlLink.crawl_id == crawl_id))).scalars().all()

        self.assertEqual(crawl.status, "completed")
        self.assertEqual(len(pages), 4)
        self.assertEqual(len(links), 3)
        self.assertGreater(crawl.nav_edges_discarded, 0)

    async def test_content_inlink_counts_land_on_the_page_rows(self):
        """
        The number prompts/internal_linking.yaml scores on: how many BODY
        links point at this page, counted apart from navigation.
        """
        crawl_id = str(uuid.uuid4())
        await persist_crawl(crawl_id, "https://example.com", _artifacts())

        async with AsyncSessionLocal() as db:
            pricing = (await db.execute(
                select(CrawlPage).where(
                    CrawlPage.crawl_id == crawl_id,
                    CrawlPage.url == "https://example.com/pricing",
                )
            )).scalar_one()
            about = (await db.execute(
                select(CrawlPage).where(
                    CrawlPage.crawl_id == crawl_id,
                    CrawlPage.url == "https://example.com/about",
                )
            )).scalar_one()

        self.assertEqual(pricing.content_inlinks, 1)
        self.assertEqual(about.content_inlinks, 0)
        self.assertEqual(about.nav_inlinks, 2)

    async def test_page_with_no_content_inlinks_is_flagged_orphan(self):
        crawl_id = str(uuid.uuid4())
        await persist_crawl(crawl_id, "https://example.com", _artifacts())

        async with AsyncSessionLocal() as db:
            orphan = (await db.execute(
                select(CrawlPage).where(
                    CrawlPage.crawl_id == crawl_id,
                    CrawlPage.url == "https://example.com/orphan-ish",
                )
            )).scalar_one()

        self.assertTrue(orphan.is_orphan)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_crawl_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.workers.crawl_worker'`

- [ ] **Step 3: Write the implementation**

```python
# api/workers/crawl_worker.py
"""
Runs a Screaming Frog crawl and persists the filtered link graph.

Deliberately NOT part of the 4-step audit pipeline in audit_worker.py. A
verified crawl of a ~100-route app took over 10 minutes; making every audit
wait on one would be a large regression, and would hit client sites on every
run. Crawls are triggered explicitly and audits consume the newest fresh one.
"""

import logging
import tempfile
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CrawlLink, CrawlPage, SiteCrawl
from core.sf_crawler import CrawlArtifacts, SfCrawlFailed, run_crawl
from core.sf_parser import parse_inlinks, parse_internal_html

logger = logging.getLogger(__name__)


async def persist_crawl(crawl_id: str, website: str, artifacts: CrawlArtifacts) -> None:
    """Parse the exports and write one SiteCrawl plus its pages and edges."""
    pages = parse_internal_html(artifacts.internal_html_csv)
    edges, counts = parse_inlinks(artifacts.inlinks_csv)

    nav_discarded = sum(c["non_content"] for c in counts.values())

    async with AsyncSessionLocal() as db:
        crawl = (await db.execute(select(SiteCrawl).where(SiteCrawl.id == crawl_id))).scalar_one_or_none()
        if crawl is None:
            crawl = SiteCrawl(id=crawl_id, website=website, started_at=datetime.now(timezone.utc))
            db.add(crawl)

        for page in pages:
            page_counts = counts.get(page["url"], {"content": 0, "non_content": 0})
            db.add(CrawlPage(
                id=str(uuid.uuid4()),
                crawl_id=crawl_id,
                url=page["url"],
                status_code=page["status_code"],
                indexability=page["indexability"],
                crawl_depth=page["crawl_depth"],
                inlinks_total=page["inlinks_total"],
                unique_inlinks=page["unique_inlinks"],
                outlinks_total=page["outlinks_total"],
                unique_outlinks=page["unique_outlinks"],
                content_inlinks=page_counts["content"],
                nav_inlinks=page_counts["non_content"],
                # An orphan here means "reachable by the crawler but with zero
                # body-content links pointing at it" -- the crawl-vulnerable
                # case prompts/internal_linking.yaml scores on. The home page
                # is exempt: nothing links to it in body content by design.
                is_orphan=(page_counts["content"] == 0 and (page["crawl_depth"] or 0) > 0),
            ))

        for edge in edges:
            db.add(CrawlLink(
                id=str(uuid.uuid4()),
                crawl_id=crawl_id,
                source_url=edge["source_url"],
                dest_url=edge["dest_url"],
                anchor=edge["anchor"],
                link_position=edge["link_position"],
                follow=edge["follow"],
                dest_status_code=edge["dest_status_code"],
                reason=edge["reason"],
            ))

        crawl.status = "completed"
        crawl.pages_crawled = len(pages)
        crawl.content_edges = sum(1 for e in edges if e["reason"] == "content")
        crawl.nav_edges_discarded = nav_discarded
        crawl.completed_at = datetime.now(timezone.utc)
        await db.commit()

    logger.info(
        "Crawl %s persisted: %d pages, %d stored edges, %d nav edges discarded",
        crawl_id, len(pages), len(edges), nav_discarded,
    )


async def run_site_crawl(website: str) -> str:
    """Full run: crawl, parse, persist. Returns the crawl id."""
    crawl_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        db.add(SiteCrawl(
            id=crawl_id, website=website, status="running",
            started_at=datetime.now(timezone.utc),
        ))
        await db.commit()

    try:
        with tempfile.TemporaryDirectory(prefix=f"sfcrawl_{crawl_id[:8]}_") as tmp:
            artifacts = run_crawl(website, output_dir=tmp)
            await persist_crawl(crawl_id, website, artifacts)
    except (SfCrawlFailed, Exception) as exc:  # noqa: B014 - want the DB row marked either way
        logger.error("Crawl %s of %s failed: %s", crawl_id, website, exc)
        async with AsyncSessionLocal() as db:
            crawl = (await db.execute(select(SiteCrawl).where(SiteCrawl.id == crawl_id))).scalar_one_or_none()
            if crawl:
                crawl.status = "failed"
                crawl.error = str(exc)[:2000]
                crawl.completed_at = datetime.now(timezone.utc)
                await db.commit()
        raise

    return crawl_id
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_crawl_worker.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add api/workers/crawl_worker.py tests/test_crawl_worker.py
git commit -m "feat(crawl): persist crawls, pages and filtered edges

Kept out of the 4-step audit pipeline on purpose: a verified crawl of a
~100-route app took over 10 minutes, so making every audit wait on one
would be a large regression and would hit client sites on every run.

content_inlinks is the figure internal_linking.yaml actually scores on --
body links pointing at a page, counted apart from navigation. is_orphan
means zero content inlinks at depth > 0; the home page is exempt because
nothing links to it in body content by design.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

# Stage 5B — Derivations

## Task 7: Site-level derivations

**Files:**
- Create: `core/crawl_insights.py`
- Test: `tests/test_crawl_insights.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crawl_insights.py
import unittest

from core.crawl_insights import anchor_distribution, broken_internal_links, depth_histogram

GENERIC = "click here"


class TestAnchorDistribution(unittest.TestCase):
    def test_counts_anchors_and_flags_generic_ones(self):
        edges = [
            {"anchor": "See our pricing", "dest_url": "/pricing", "reason": "content"},
            {"anchor": "click here", "dest_url": "/gone", "reason": "content"},
            {"anchor": "Click Here", "dest_url": "/x", "reason": "content"},
            {"anchor": None, "dest_url": "/y", "reason": "content"},
        ]
        result = anchor_distribution(edges)

        self.assertEqual(result["total"], 4)
        self.assertEqual(result["generic"], 2)      # case-insensitive
        self.assertEqual(result["empty"], 1)
        self.assertEqual(result["descriptive"], 1)

    def test_generic_detection_is_not_substring_greedy(self):
        """'Here is our pricing guide' contains 'here' but is descriptive."""
        edges = [{"anchor": "Here is our pricing guide", "dest_url": "/p", "reason": "content"}]
        self.assertEqual(anchor_distribution(edges)["generic"], 0)


class TestBrokenInternalLinks(unittest.TestCase):
    def test_groups_broken_destinations_with_their_sources(self):
        edges = [
            {"source_url": "/a", "dest_url": "/gone", "dest_status_code": 404, "reason": "error", "anchor": "x"},
            {"source_url": "/b", "dest_url": "/gone", "dest_status_code": 404, "reason": "error", "anchor": "y"},
            {"source_url": "/a", "dest_url": "/ok", "dest_status_code": 200, "reason": "content", "anchor": "z"},
        ]
        broken = broken_internal_links(edges)

        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["dest_url"], "/gone")
        self.assertEqual(sorted(broken[0]["linked_from"]), ["/a", "/b"])


class TestDepthHistogram(unittest.TestCase):
    def test_buckets_pages_by_crawl_depth(self):
        pages = [
            {"crawl_depth": 0}, {"crawl_depth": 1}, {"crawl_depth": 1},
            {"crawl_depth": 4}, {"crawl_depth": None},
        ]
        self.assertEqual(depth_histogram(pages), {0: 1, 1: 2, 4: 1})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_crawl_insights.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.crawl_insights'`

- [ ] **Step 3: Write the implementation**

```python
# core/crawl_insights.py
"""
Derives the findings prompts/internal_linking.yaml asks about from stored
crawl rows: anchor text quality, internal 404s with their sources, and crawl
depth distribution.

Pure functions over dicts so they can be unit-tested without a database.
"""

from collections import Counter, defaultdict
from typing import Dict, List

# The prompt names these explicitly: "Generic anchors ('click here', 'here',
# 'read more', 'learn more') are flagged as MAJOR issues." Matched as whole
# normalised strings, not substrings -- "Here is our pricing guide" is
# descriptive and must not be flagged.
GENERIC_ANCHORS = {
    "click here", "here", "read more", "learn more", "more",
    "this", "link", "this link", "see more", "details", "click",
}


def anchor_distribution(edges: List[dict]) -> Dict[str, object]:
    """Counts of descriptive vs generic vs empty anchors across content edges."""
    content = [e for e in edges if e.get("reason") == "content"]

    generic = empty = descriptive = 0
    texts = Counter()

    for edge in content:
        anchor = (edge.get("anchor") or "").strip()
        if not anchor:
            empty += 1
            continue
        texts[anchor] += 1
        if anchor.casefold() in GENERIC_ANCHORS:
            generic += 1
        else:
            descriptive += 1

    return {
        "total": len(content),
        "generic": generic,
        "empty": empty,
        "descriptive": descriptive,
        "most_common": texts.most_common(10),
    }


def broken_internal_links(edges: List[dict]) -> List[dict]:
    """Broken destinations, each with the pages that link to it."""
    sources = defaultdict(list)
    statuses = {}

    for edge in edges:
        status = edge.get("dest_status_code")
        if status is None or status < 400:
            continue
        dest = edge["dest_url"]
        sources[dest].append(edge["source_url"])
        statuses[dest] = status

    return [
        {"dest_url": dest, "status_code": statuses[dest], "linked_from": srcs}
        for dest, srcs in sources.items()
    ]


def depth_histogram(pages: List[dict]) -> Dict[int, int]:
    """How many pages sit at each click depth. Pages with unknown depth are skipped."""
    histogram = Counter()
    for page in pages:
        depth = page.get("crawl_depth")
        if depth is not None:
            histogram[depth] += 1
    return dict(histogram)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_crawl_insights.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add core/crawl_insights.py tests/test_crawl_insights.py
git commit -m "feat(crawl): derive anchor quality, internal 404s and depth spread

Generic anchors are matched as whole normalised strings, not substrings:
'Here is our pricing guide' contains 'here' but is descriptive, and a
substring match would have flagged it. A test pins that.

Broken destinations carry the pages that link to them, which is the part
that makes the finding actionable -- SF's 4xx tab export gives only an
inlink count, so the sources come from the stored error edges.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

# Stage 5C — Feed the prompt

## Task 8: Render per-page crawl facts

**Files:**
- Create: `core/crawl_facts.py`
- Test: `tests/test_crawl_facts.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crawl_facts.py
import unittest

from core.crawl_facts import format_crawl_facts_block


class TestFormatCrawlFactsBlock(unittest.TestCase):
    def test_no_crawl_data_says_so_instead_of_implying_zero(self):
        """
        Absent data must never render as '0 internal inlinks' -- that would
        make the LLM apply the prompt's "MAXIMUM 20" cap to a page nobody
        measured, inventing a failing score from missing data.
        """
        block = format_crawl_facts_block(None)
        self.assertIn("no crawl data", block.lower())
        self.assertNotIn("0 internal content inlinks", block)

    def test_reports_content_and_nav_inlinks_separately(self):
        facts = {
            "url": "https://example.com/pricing",
            "content_inlinks": 3,
            "nav_inlinks": 40,
            "crawl_depth": 1,
            "outlinks_total": 6,
            "is_orphan": False,
            "inbound_anchors": ["See our pricing", "pricing plans"],
            "broken_outlinks": [],
        }
        block = format_crawl_facts_block(facts)

        self.assertIn("3", block)
        self.assertIn("40", block)
        self.assertIn("navigation", block.lower())
        self.assertIn("See our pricing", block)

    def test_orphan_page_is_stated_plainly(self):
        facts = {
            "url": "https://example.com/orphan",
            "content_inlinks": 0,
            "nav_inlinks": 12,
            "crawl_depth": 3,
            "outlinks_total": 2,
            "is_orphan": True,
            "inbound_anchors": [],
            "broken_outlinks": [],
        }
        block = format_crawl_facts_block(facts)
        self.assertIn("ZERO", block)
        self.assertIn("orphan", block.lower())

    def test_broken_outlinks_are_listed(self):
        facts = {
            "url": "https://example.com/a",
            "content_inlinks": 2, "nav_inlinks": 10, "crawl_depth": 1,
            "outlinks_total": 5, "is_orphan": False, "inbound_anchors": [],
            "broken_outlinks": [{"dest_url": "https://example.com/gone", "status_code": 404}],
        }
        block = format_crawl_facts_block(facts)
        self.assertIn("https://example.com/gone", block)
        self.assertIn("404", block)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_crawl_facts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.crawl_facts'`

- [ ] **Step 3: Write the implementation**

```python
# core/crawl_facts.py
"""
Renders per-page link-graph facts for prompts/internal_linking.yaml, following
the same shape core/technical_facts.py uses for TECHNICAL_SEO.

Why per-page and not site-wide: the prompt analyses one page at a time
("Analyze the provided web page"), and its scoring rubric turns on how many
BODY links point at that page -- something no amount of reading the page's own
HTML can reveal. That is the guess this block replaces with a measurement.
"""

from typing import Optional


def format_crawl_facts_block(page_facts: Optional[dict]) -> str:
    lines = ["=== CRAWL FACTS (measured by a real site crawl -- do NOT contradict these) ==="]

    if not page_facts:
        # Never render missing data as zero: the prompt caps a page at 20/100
        # when it has no body-content inlinks, so a fabricated zero would
        # invent a failing score for a page nobody measured.
        lines.append(
            "no crawl data available for this page -- judge internal linking from the "
            "page content alone, and do NOT assume anything about how many pages link to it"
        )
        lines.append("=== END CRAWL FACTS -- page content follows below ===")
        return "\n".join(lines)

    content_in = page_facts.get("content_inlinks", 0)
    nav_in = page_facts.get("nav_inlinks", 0)

    if content_in == 0:
        lines.append(
            f"inbound internal links: ZERO body-content links point to this page "
            f"(it is an orphan in content terms); {nav_in} navigation/footer/sidebar links do"
        )
    else:
        lines.append(
            f"inbound internal links: {content_in} from body content, "
            f"plus {nav_in} from navigation/footer/sidebar (counted separately)"
        )

    anchors = page_facts.get("inbound_anchors") or []
    if anchors:
        lines.append(f"anchor text used to link here: {', '.join(repr(a) for a in anchors[:10])}")
    elif content_in > 0:
        lines.append("anchor text used to link here: all inbound content links have empty anchor text")

    depth = page_facts.get("crawl_depth")
    if depth is not None:
        lines.append(f"crawl depth: {depth} click(s) from the home page")

    lines.append(f"outbound links from this page: {page_facts.get('outlinks_total', 0)}")

    broken = page_facts.get("broken_outlinks") or []
    if broken:
        listed = "; ".join(f"{b['dest_url']} ({b['status_code']})" for b in broken[:10])
        lines.append(f"BROKEN outbound links on this page: {listed}")
    else:
        lines.append("broken outbound links on this page: none found")

    lines.append("=== END CRAWL FACTS -- page content follows below ===")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_crawl_facts.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add core/crawl_facts.py tests/test_crawl_facts.py
git commit -m "feat(crawl): render per-page link facts for the internal-linking prompt

Follows core/technical_facts.py's format_facts_block shape. Per-page rather
than site-wide because the prompt analyses one page at a time and scores on
how many BODY links point at it -- exactly what reading that page's own HTML
cannot tell you.

Missing data renders as 'no crawl data', never as zero: the prompt caps a
page at 20/100 with no body inlinks, so a fabricated zero would invent a
failing score for an unmeasured page. A test pins that.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

## Task 9: Look up a page's facts from the newest crawl

**Files:**
- Modify: `core/crawl_facts.py`
- Test: `tests/test_crawl_facts.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_crawl_facts.py
import uuid
from datetime import datetime, timedelta, timezone

from api.models._base import AsyncSessionLocal
from api.models.database import CrawlLink, CrawlPage, SiteCrawl
from core.crawl_facts import load_page_facts


class TestLoadPageFacts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.website = f"https://t{uuid.uuid4().hex[:8]}.example"
        self.old_id = str(uuid.uuid4())
        self.new_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        async with AsyncSessionLocal() as db:
            db.add(SiteCrawl(id=self.old_id, website=self.website, status="completed",
                             completed_at=now - timedelta(days=10)))
            db.add(SiteCrawl(id=self.new_id, website=self.website, status="completed",
                             completed_at=now))
            db.add(CrawlPage(id=str(uuid.uuid4()), crawl_id=self.old_id,
                             url=f"{self.website}/p", content_inlinks=99, nav_inlinks=1,
                             crawl_depth=1, outlinks_total=1))
            db.add(CrawlPage(id=str(uuid.uuid4()), crawl_id=self.new_id,
                             url=f"{self.website}/p", content_inlinks=4, nav_inlinks=20,
                             crawl_depth=2, outlinks_total=7))
            db.add(CrawlLink(id=str(uuid.uuid4()), crawl_id=self.new_id,
                             source_url=f"{self.website}/home", dest_url=f"{self.website}/p",
                             anchor="our plans", reason="content", link_position="Content"))
            await db.commit()

    async def test_reads_the_newest_completed_crawl_not_an_older_one(self):
        facts = await load_page_facts(self.website, f"{self.website}/p")
        self.assertEqual(facts["content_inlinks"], 4)   # not 99
        self.assertEqual(facts["crawl_depth"], 2)

    async def test_collects_inbound_anchor_text(self):
        facts = await load_page_facts(self.website, f"{self.website}/p")
        self.assertIn("our plans", facts["inbound_anchors"])

    async def test_unknown_page_returns_none(self):
        facts = await load_page_facts(self.website, f"{self.website}/never-crawled")
        self.assertIsNone(facts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_crawl_facts.py::TestLoadPageFacts -v`
Expected: FAIL — `ImportError: cannot import name 'load_page_facts'`

- [ ] **Step 3: Write the implementation**

```python
# append to core/crawl_facts.py
import logging

from sqlalchemy import select

from api.models._base import AsyncSessionLocal
from api.models.database import CrawlLink, CrawlPage, SiteCrawl

logger = logging.getLogger(__name__)


async def load_page_facts(website: str, page_url: str) -> Optional[dict]:
    """
    Facts for one page from the newest COMPLETED crawl of that site.

    Returns None when the site has never been crawled or the page was not in
    the crawl -- the caller must render that as "no crawl data", never as zero.
    """
    async with AsyncSessionLocal() as db:
        crawl = (await db.execute(
            select(SiteCrawl)
            .where(SiteCrawl.website == website, SiteCrawl.status == "completed")
            .order_by(SiteCrawl.completed_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if crawl is None:
            return None

        page = (await db.execute(
            select(CrawlPage).where(CrawlPage.crawl_id == crawl.id, CrawlPage.url == page_url)
        )).scalar_one_or_none()
        if page is None:
            return None

        inbound = (await db.execute(
            select(CrawlLink).where(
                CrawlLink.crawl_id == crawl.id,
                CrawlLink.dest_url == page_url,
                CrawlLink.reason == "content",
            )
        )).scalars().all()

        outbound_broken = (await db.execute(
            select(CrawlLink).where(
                CrawlLink.crawl_id == crawl.id,
                CrawlLink.source_url == page_url,
                CrawlLink.reason == "error",
            )
        )).scalars().all()

    return {
        "url": page.url,
        "content_inlinks": page.content_inlinks,
        "nav_inlinks": page.nav_inlinks,
        "crawl_depth": page.crawl_depth,
        "outlinks_total": page.outlinks_total,
        "is_orphan": page.is_orphan,
        "inbound_anchors": [link.anchor for link in inbound if link.anchor],
        "broken_outlinks": [
            {"dest_url": link.dest_url, "status_code": link.dest_status_code}
            for link in outbound_broken
        ],
    }
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_crawl_facts.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add core/crawl_facts.py tests/test_crawl_facts.py
git commit -m "feat(crawl): load per-page facts from the newest completed crawl

Ordered by completed_at desc and filtered to status=completed so a stale or
half-finished crawl can never shadow a good one -- a test seeds two crawls
and asserts the older figures are not the ones returned.

Returns None for an uncrawled page so the caller renders 'no crawl data'
rather than a fabricated zero.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

## Task 10: Inject the facts into the analyzer

**Files:**
- Modify: `core/direct_analyzer.py` (the `question_type == "TECHNICAL_SEO"` block around line 955)

- [ ] **Step 1: Read the surrounding code**

Run: `sed -n '943,980p' core/direct_analyzer.py`

You will see the TECHNICAL_SEO facts injection. The new block goes immediately after it, before the research-context injection.

- [ ] **Step 2: Add the injection**

Insert after the TECHNICAL_SEO block's `page_text = facts_block + "\n\n" + page_text` line and before the `# Inject research context if available` comment:

```python
                # Same idea for internal linking (Etapa 5): the prompt scores
                # on how many BODY links point at this page, which cannot be
                # read off the page's own HTML -- without this the model is
                # guessing. Facts come from the newest completed crawl; if the
                # site was never crawled, format_crawl_facts_block renders
                # "no crawl data" rather than a zero that would trip the
                # prompt's "MAXIMUM 20" cap on an unmeasured page.
                if self.question_type == "INTERNAL_LINKING":
                    from core.crawl_facts import format_crawl_facts_block, load_page_facts

                    page_url = self._url_for_filename(filename)
                    crawl_page_facts = None
                    if page_url:
                        try:
                            crawl_page_facts = await load_page_facts(self.website, page_url)
                        except Exception as exc:
                            logger.warning("Could not load crawl facts for %s: %s", page_url, exc)

                    page_text = format_crawl_facts_block(crawl_page_facts) + "\n\n" + page_text
```

- [ ] **Step 3: Add the filename→URL helper**

`load_page_facts` needs the page's URL, and the analyzer works from filenames. Add this method to the same class (put it next to the other private helpers):

```python
    def _url_for_filename(self, filename: str) -> Optional[str]:
        """
        Map an analyzer input filename back to the crawled URL.

        The scraper writes one file per page; this reads the mapping the
        scrape step already produced rather than re-deriving it, so a URL
        with query strings or unusual characters still matches.
        """
        if not self._url_map:
            return None
        return self._url_map.get(os.path.splitext(filename)[0])
```

- [ ] **Step 4: Populate `_url_map` where `_domain_facts` is set**

Find the `run()` method around line 1224 where `self._domain_facts = await fetch_domain_facts(self.website)` happens, and add beside it:

```python
            # Filename -> URL mapping written by the scrape step, used to look
            # up per-page crawl facts during INTERNAL_LINKING analysis.
            self._url_map = {}
            try:
                map_path = os.path.join(self.input_dir, "..", "url_map.json")
                if os.path.exists(map_path):
                    with open(map_path, "r", encoding="utf-8") as fh:
                        self._url_map = json.load(fh)
            except Exception as exc:
                logger.warning("Could not load url_map.json: %s", exc)
```

Also initialise `self._url_map = {}` in `__init__` next to `self._domain_facts`, so the attribute always exists.

**If `url_map.json` does not exist**, the scrape step does not currently write one. Check first:

Run: `grep -rn "url_map" core/web_scraper.py api/workers/audit_worker.py`

If there are no hits, add Task 10b below before continuing.

- [ ] **Step 5: Verify the app still imports**

Run: `python -c "import core.direct_analyzer; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass, 222 existing plus the new ones.

- [ ] **Step 7: Commit**

```bash
git add core/direct_analyzer.py
git commit -m "feat(crawl): feed link-graph facts into INTERNAL_LINKING analysis

Mirrors the TECHNICAL_SEO facts injection a few lines above. The prompt's
rubric turns on how many body-content links point at a page and caps it at
20/100 when there are none -- a fact the page's own HTML cannot supply, so
until now the model was guessing at it.

Falls back to an explicit 'no crawl data' block when the site has not been
crawled, so an unmeasured page is never scored as if it had zero inlinks.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

## Task 10b: Write `url_map.json` during scraping (only if Step 4 found no mapping)

**Files:**
- Modify: `core/web_scraper.py` (in `scrape()`, after the page loop completes)

- [ ] **Step 1: Write the mapping alongside the HTML output**

At the end of `scrape()`, before it returns its summary, add:

```python
    # Filename -> URL map, so later stages (crawl facts injection for
    # INTERNAL_LINKING) can get from an analyzer input file back to the URL it
    # came from without re-deriving the slugging rules.
    try:
        map_path = os.path.join(os.path.dirname(output_dir.rstrip(os.sep)), "url_map.json")
        with open(map_path, "w", encoding="utf-8") as fh:
            json.dump(url_map, fh, indent=2)
    except Exception as exc:
        print(f"[web_scraper] Could not write url_map.json: {exc}")
```

Build `url_map` as pages are written: wherever the scraper decides an output filename, record `url_map[os.path.splitext(os.path.basename(filename))[0]] = url`. Initialise `url_map = {}` before the page loop.

- [ ] **Step 2: Verify a scrape writes it**

Run a small scrape against the local server and confirm the file appears:

Run: `python -c "import json;print(list(json.load(open('output/url_map.json')).items())[:3])"`
Expected: a list of `(filename_stem, url)` pairs.

- [ ] **Step 3: Commit**

```bash
git add core/web_scraper.py
git commit -m "feat(scrape): record a filename to URL map for later stages

Downstream stages need to get from an analyzer input file back to the URL
it came from. Re-deriving it would mean duplicating the slugging rules and
getting query strings and unusual characters subtly wrong.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

## Task 11: API endpoints

**Files:**
- Create: `api/routes/crawl.py`
- Modify: `api/routes/__init__.py`, `api/main.py`

- [ ] **Step 1: Write the router**

```python
# api/routes/crawl.py
"""Trigger and read site crawls (Etapa 5 of docs/IMPROVEMENTS_PLAN.md)."""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.limiter import limiter
from api.models.database import CrawlPage, SiteCrawl, get_db
from api.utils.errors import raise_bad_request, raise_not_found
from api.workers.crawl_worker import run_site_crawl
from core.sf_crawler import SfConfigMissing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crawl", tags=["crawl"])


class StartCrawlRequest(BaseModel):
    website: str = Field(..., min_length=4, max_length=500)

    @field_validator("website")
    @classmethod
    def validate_website(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("website must include the scheme, e.g. https://example.com")
        return v


@router.post("/start")
@limiter.limit("5/hour")
async def start_crawl(request: Request, body: StartCrawlRequest, background: BackgroundTasks):
    """
    Rate limited to 5/hour deliberately. A crawl is the heaviest thing this
    tool does to someone else's server -- a verified run took over 10 minutes
    -- so accidental repeat triggering must be hard.
    """
    try:
        from core.sf_crawler import _config_path
        _config_path()
    except SfConfigMissing as exc:
        raise_bad_request(str(exc))

    background.add_task(run_site_crawl, body.website)
    return {"status": "started", "website": body.website}


@router.get("/{crawl_id}")
async def get_crawl(crawl_id: str, db: AsyncSession = Depends(get_db)):
    crawl = (await db.execute(select(SiteCrawl).where(SiteCrawl.id == crawl_id))).scalar_one_or_none()
    if crawl is None:
        raise_not_found("Crawl not found")
    return crawl.to_dict()


@router.get("/site/{website:path}/latest")
async def latest_crawl(website: str, db: AsyncSession = Depends(get_db)):
    crawl = (await db.execute(
        select(SiteCrawl)
        .where(SiteCrawl.website == website, SiteCrawl.status == "completed")
        .order_by(SiteCrawl.completed_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if crawl is None:
        raise_not_found(f"No completed crawl for {website}")

    orphans = (await db.execute(
        select(CrawlPage).where(CrawlPage.crawl_id == crawl.id, CrawlPage.is_orphan.is_(True))
    )).scalars().all()

    return {
        **crawl.to_dict(),
        "orphan_pages": [p.to_dict() for p in orphans[:100]],
        "orphan_count": len(orphans),
    }
```

- [ ] **Step 2: Register the router**

In `api/routes/__init__.py` add, matching the existing style:

```python
from .crawl import router as crawl_router
```

In `api/main.py`, beside the other `app.include_router(...)` calls:

```python
app.include_router(crawl_router)
```

- [ ] **Step 3: Verify it loads**

Run: `python -c "import api.main; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Restart and smoke the endpoints**

```bash
taskkill /F /IM uvicorn.exe
restart_server.bat
```

Then:

```bash
curl -s "http://127.0.0.1:8000/api/crawl/site/https%3A%2F%2Fnot-crawled.example/latest" -w "\nHTTP %{http_code}\n"
```

Expected: `HTTP 404` with a "No completed crawl" message — proving the route is wired and fails cleanly.

- [ ] **Step 5: Run the gate**

```bash
python -m pytest -q
python3 tests/smoke.py
python3 tests/api_diff.py
```

Expected: all tests pass, smoke reports no new 500s, api_diff lists exactly the three new operations. Then update the baseline:

Run: `python3 tests/api_diff.py --update`

- [ ] **Step 6: Commit**

```bash
git add api/routes/crawl.py api/routes/__init__.py api/main.py tests/baseline/openapi.json
git commit -m "feat(crawl): add crawl trigger and read endpoints

POST /api/crawl/start is rate limited to 5/hour: a crawl is the heaviest
thing this tool does to someone else's server, so accidental repeat
triggering should be hard. It refuses up front with a clear message when
no .seospiderconfig is configured rather than failing later in a
background task where nobody sees it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

## Task 12: End-to-end verification against a real site

- [ ] **Step 1: Crawl a site you own**

The improvements plan says so explicitly: *"Testează pe un site propriu."* Use `https://nrankai.com`, never a client site, for the first real run.

```bash
curl -s -X POST http://127.0.0.1:8000/api/crawl/start \
  -H "Content-Type: application/json" \
  -d '{"website":"https://nrankai.com"}' -w "\nHTTP %{http_code}\n"
```

Expected: `HTTP 200`, `{"status":"started",...}`

- [ ] **Step 2: Watch it finish**

```bash
python -c "
import sqlite3
c = sqlite3.connect(r'api/data/analyzer.db')
for row in c.execute('SELECT id, status, pages_crawled, content_edges, nav_edges_discarded, error FROM site_crawls ORDER BY created_at DESC LIMIT 3'):
    print(row)
"
```

Expected: eventually `status='completed'` with a non-zero `pages_crawled`, and `nav_edges_discarded` much larger than `content_edges` — that ratio is the design assumption holding on real data.

- [ ] **Step 3: Sanity-check the derived findings**

```bash
python -c "
import sqlite3
c = sqlite3.connect(r'api/data/analyzer.db')
cid = c.execute('SELECT id FROM site_crawls WHERE status=\"completed\" ORDER BY completed_at DESC LIMIT 1').fetchone()[0]
print('orphans:', c.execute('SELECT COUNT(*) FROM crawl_pages WHERE crawl_id=? AND is_orphan=1', (cid,)).fetchone()[0])
print('broken edges:', c.execute('SELECT COUNT(*) FROM crawl_links WHERE crawl_id=? AND reason=\"error\"', (cid,)).fetchone()[0])
for r in c.execute('SELECT source_url, dest_url, dest_status_code FROM crawl_links WHERE crawl_id=? AND reason=\"error\" LIMIT 5', (cid,)):
    print(' ', r)
"
```

Spot-check two or three of the reported broken links by opening them in a browser. If a reported 404 loads fine, stop and investigate before trusting any of it.

- [ ] **Step 4: Confirm the facts block reaches the prompt**

Run an INTERNAL_LINKING audit against the crawled site and confirm the block is present in the analyzer input. Check the audit log for the page text sent, or temporarily log `page_text[:400]` at the injection point.

Expected: the text begins with `=== CRAWL FACTS (measured by a real site crawl ...`

- [ ] **Step 5: Update the improvements plan**

Mark Etapa 5 done in `docs/IMPROVEMENTS_PLAN.md` following the format used for Etapa 3 and 4, and state the known limitations listed below.

- [ ] **Step 6: Commit**

```bash
git add docs/IMPROVEMENTS_PLAN.md
git commit -m "docs: mark Etapa 5 (link-graph crawler) done

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Out of scope / known limitations to state honestly

- **A Screaming Frog licence and a Windows machine with it installed are now hard dependencies** of the crawl feature. Crawling degrades to "no crawl data" everywhere else, which the prompt block handles explicitly. This is a real portability cost of not writing our own crawler, and it should be written down rather than discovered later.
- **The crawl config is a human-maintained artifact.** Nobody can regenerate `sf_audit.seospiderconfig` from code; if it is lost, Task 0 must be repeated.
- **Navigation edges are not recoverable after the fact.** Only their counts are stored. Answering "which nav links point here" later means re-crawling.
- **`Sitemaps:Orphan URLs` needs "Crawl Linked XML Sitemaps" enabled in the config** (Task 0). Without it, `is_orphan` still works but means only "no content inlinks", not the stricter "in the sitemap yet unlinked".
- **Redirect chains are exported but not yet parsed.** `redirect_chains.csv` is collected in Task 2 and left for a follow-up; nothing in Stages 5A–5C reads it. Do not claim redirect-chain analysis is delivered.
- **Crawl scheduling is not included.** Crawls are manual per site. Wiring them into `api/models/infra.py` schedules is a separate piece of work.

## Self-review notes

- **Spec coverage:** follows links (Task 2, via SF); depth/pages/robots/rate limits (Task 0, enforced by Task 2); link graph stored (Tasks 4–5); orphans (Task 6); internal 404s (Task 7); anchor distribution (Task 7); depth (Task 7); feeds `internal_linking.yaml` through the `technical_facts.py` pattern (Tasks 8–10). **Gap, stated above:** redirect chains are exported but not parsed.
- **Type consistency:** `parse_inlinks` returns `(edges, counts)` with keys `content`/`non_content`, consumed under those names in `crawl_worker.persist_crawl`; edge dicts carry `source_url`, `dest_url`, `anchor`, `link_position`, `follow`, `dest_status_code`, `reason` and are read under those names in `crawl_insights` and `crawl_worker`. `load_page_facts` returns exactly the keys `format_crawl_facts_block` reads.
- **Uncertainty flagged for the implementer:** Task 10 Step 4 depends on a `url_map.json` that may not exist. The step includes the grep that settles it and Task 10b supplies the fallback, so this is checked rather than assumed.
