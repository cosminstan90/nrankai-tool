"""
Drives the licensed Screaming Frog SEO Spider headless to collect an internal
link graph (Etapa 5 of docs/IMPROVEMENTS_PLAN.md).

core/web_scraper.py starts from the sitemap and never follows a link, so
orphan pages, internal 404s, redirect chains and crawl depth were invisible,
and prompts/internal_linking.yaml had to guess a page's inbound links from
that page's own HTML.

Screaming Frog does the crawling rather than a hand-written crawler because it
already handles robots.txt, redirect chains, rate limiting and -- the part
that turned out to matter most -- link-position classification. In a verified
crawl, 87% of hyperlink edges were navigation and only 0.09% were real content
links; see docs/superpowers/plans/2026-09-04-sf-crawler-link-graph.md.

The subprocess is always invoked with an argv LIST. Building the command as a
single shell string breaks: PowerShell split
"Response Codes:Internal Client Error (4xx)" on spaces during development and
Screaming Frog exited with "SeoSpider failed to start".
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CLI = r"C:\Program Files (x86)\Screaming Frog SEO Spider\ScreamingFrogSEOSpiderCli.exe"

BULK_EXPORTS = "Links:All Inlinks"
EXPORT_TABS = "Internal:HTML,Response Codes:Internal Client Error (4xx),Sitemaps:Orphan URLs"
SAVE_REPORTS = "Redirects:Redirect Chains"


class SfConfigMissing(RuntimeError):
    """Raised when no .seospiderconfig is configured -- see _config_path."""


class SfCrawlFailed(RuntimeError):
    """Raised when the Screaming Frog process fails or times out."""


@dataclass
class CrawlArtifacts:
    """Paths Screaming Frog wrote. Files may be absent when --skip-empty applied."""
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
    A config file is REQUIRED and never defaulted.

    Screaming Frog's built-in defaults are unlimited crawl depth, unlimited
    total URLs and 5 concurrent threads -- exactly the "hitting real sites too
    hard" failure docs/IMPROVEMENTS_PLAN.md calls out as non-negotiable. The
    limits live in a GUI-exported .seospiderconfig because spider.config holds
    no crawl settings and SF has no --save-config flag, so there is nothing to
    fall back to that would still be safe.
    """
    path = os.getenv("SCREAMING_FROG_CONFIG", "").strip()
    if not path or not os.path.isfile(path):
        raise SfConfigMissing(
            "SCREAMING_FROG_CONFIG is not set to an existing .seospiderconfig file. "
            "Export one from the Screaming Frog GUI (File > Configuration > Save As) "
            "with crawl limits, robots.txt respect and a URI/s cap set. Crawling "
            "without it would run unlimited and unthrottled against a real site."
        )
    return path


def build_argv(website: str, output_dir: str, config_path: str) -> list:
    """
    Build the argv list. Kept separate from run_crawl so the quoting behaviour
    is testable without launching Screaming Frog.
    """
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
    zero network latency took over 10 minutes, so a real site under a 500-URL
    cap needs real headroom. On timeout the process is killed and SfCrawlFailed
    is raised -- a half-finished crawl is never persisted.
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
    except FileNotFoundError as exc:
        raise SfCrawlFailed(
            f"Screaming Frog CLI not found at {_cli_path()}. Set SCREAMING_FROG_CLI."
        ) from exc

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
