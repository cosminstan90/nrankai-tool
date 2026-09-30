"""
JS-rendering visibility gap for AI crawlers -- Pasul 12 of
docs/superpowers/plans/2026-09-30-next-steps.md.

GPTBot, ClaudeBot and PerplexityBot fetch raw HTML and do not execute
JavaScript at all. Verified against Vercel's own traffic analysis (569M
GPTBot requests, 370M ClaudeBot requests over one month, published 2026):
both crawlers *download* JS files (11.50% and 23.84% of requests
respectively) but never run them --
https://vercel.com/blog/the-rise-of-the-ai-crawler
Official docs from OpenAI/Anthropic/Perplexity don't state this explicitly
(checked before writing this module); the Vercel study is the cited source.

Content that exists only after client-side JavaScript runs is invisible to
these three, however well it's written. This measures the gap between a
plain HTTP fetch with a bot User-Agent and the Selenium-rendered capture
core/web_scraper.py already saves -- the same page, seen two ways.

A page never captured this way is reported as unmeasured, never as fully
visible: absence of a rawtext sidecar must not read as "no gap found".
"""
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from bs4 import BeautifulSoup

from core.html2llm_converter import extract_content

logger = logging.getLogger(__name__)

RAW_TEXT_SIDECAR_SUFFIX = ".rawtext.json"

# One real bot UA per company, matching what each publishes for site owners
# to allow/block in robots.txt.
GPTBOT_USER_AGENT = "GPTBot"
CLAUDEBOT_USER_AGENT = "ClaudeBot"
PERPLEXITYBOT_USER_AGENT = "PerplexityBot"
DEFAULT_BOT_USER_AGENT = GPTBOT_USER_AGENT

FETCH_TIMEOUT_S = 15

_PRICE_OR_FIGURE_RE = re.compile(
    r"(\d[\d.,]*\s?(?:%|lei|ron|eur|€|\$|usd)\b)|(\b(?:%|lei|ron|eur|€|\$|usd)\s?\d[\d.,]*)",
    re.IGNORECASE,
)


def raw_text_sidecar_path(html_path: str) -> str:
    """Where a page's raw-vs-rendered comparison lives, given its rendered HTML file path."""
    return os.path.splitext(html_path)[0] + RAW_TEXT_SIDECAR_SUFFIX


def load_js_visibility_facts(html_path: str) -> Optional[dict]:
    """
    Read a page's captured JS-visibility facts, or None when it was never measured.

    None means "not measured", never "fully visible" -- a page scraped before
    this existed, or one where the bot-UA fetch failed outright, must not be
    read as having no visibility gap.
    """
    path = raw_text_sidecar_path(html_path)
    if not os.path.exists(path):
        return None
    try:
        import json
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read JS-visibility facts at %s: %s", path, exc)
        return None


def write_js_visibility_facts(html_path: str, facts: dict) -> None:
    import json
    with open(raw_text_sidecar_path(html_path), "w", encoding="utf-8") as fh:
        json.dump(facts, fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Element-presence checks -- each looks at markup, not extracted text, since
# extract_content() strips the tags these depend on.
# ---------------------------------------------------------------------------

def _first_h1_text(html: str) -> Optional[str]:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("h1")
    if not tag:
        return None
    text = tag.get_text(strip=True)
    return text or None


def _has_json_ld(html: str) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    return any(
        (script.string or "").strip()
        for script in soup.find_all("script", type="application/ld+json")
    )


def _looks_like_faq(text: str) -> bool:
    """3+ question marks each starting a new line/sentence -- a cheap, not
    perfect, proxy for a visible Q&A block. Good enough to compare presence
    between two extractions of the SAME underlying content, which is all
    this needs: not an absolute FAQ detector."""
    if not text:
        return False
    question_lines = [line for line in re.split(r"[\n.]", text) if line.strip().endswith("?")]
    return len(question_lines) >= 3


def _numeric_signals(text: str) -> List[str]:
    """Prices/percentages/figures -- the kind of concrete detail GEO content
    should lead with, and exactly the kind buried in client-rendered widgets
    (rate calculators, pricing tables) on many real sites."""
    if not text:
        return []
    return [m.group(0) for m in _PRICE_OR_FIGURE_RE.finditer(text)]


def _first_paragraph(text: str, min_len: int = 40) -> Optional[str]:
    """First line of real substance -- skips short nav/heading-only lines."""
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= min_len:
            return line
    return None


@dataclass
class BotFetchResult:
    """What a plain HTTP fetch with a bot User-Agent actually got."""
    status_code: Optional[int]
    html: str = ""
    error: Optional[str] = None

    @property
    def blocked_or_errored(self) -> bool:
        return self.error is not None or (self.status_code is not None and self.status_code >= 400)


async def fetch_as_bot(url: str, user_agent: str = DEFAULT_BOT_USER_AGENT) -> BotFetchResult:
    """
    One plain HTTP GET with a bot User-Agent -- exactly what GPTBot/ClaudeBot/
    PerplexityBot do (a single request, no retry, no JS). Never raises.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": user_agent})
        return BotFetchResult(status_code=resp.status_code, html=resp.text if resp.status_code == 200 else "")
    except Exception as exc:   # network errors of every httpx/httpcore flavour
        logger.warning("Bot-UA fetch failed for %s: %s", url, exc)
        return BotFetchResult(status_code=None, error=str(exc)[:300])


def compare_visibility(rendered_html: str, raw_html: str,
                       bot_status_code: Optional[int] = None,
                       bot_error: Optional[str] = None) -> dict:
    """
    Pure comparison: what a real browser (rendered_html, from
    core/web_scraper.py's Selenium capture) shows vs what a plain bot-UA
    fetch (raw_html) actually returns. No network access, no file I/O --
    everything above this handles fetching; this only compares two strings.
    """
    rendered_text = extract_content(rendered_html) if rendered_html else ""
    raw_text = extract_content(raw_html) if raw_html else ""

    rendered_words = len(rendered_text.split())
    raw_words = len(raw_text.split())
    # None (not 0 or 1) when the rendered page itself has no words to compare
    # against -- a ratio needs a real denominator.
    visibility_ratio = round(raw_words / rendered_words, 3) if rendered_words else None

    missing: List[dict] = []

    rendered_h1 = _first_h1_text(rendered_html)
    if rendered_h1 and not _first_h1_text(raw_html):
        missing.append({"element": "h1", "detail": f'"{rendered_h1}" is present when rendered, absent from the raw fetch'})

    if _has_json_ld(rendered_html) and not _has_json_ld(raw_html):
        missing.append({"element": "json_ld", "detail": "structured data is present when rendered, absent from the raw fetch"})

    first_para = _first_paragraph(rendered_text)
    if first_para and first_para[:60].lower() not in raw_text.lower():
        missing.append({"element": "first_paragraph", "detail": "the page's opening content is absent from the raw fetch"})

    rendered_numbers = _numeric_signals(rendered_text)
    if rendered_numbers and not _numeric_signals(raw_text):
        missing.append({
            "element": "prices_or_figures",
            "detail": f"{len(rendered_numbers)} numeric/price mention(s) when rendered, none in the raw fetch",
        })

    if _looks_like_faq(rendered_text) and not _looks_like_faq(raw_text):
        missing.append({"element": "faq", "detail": "FAQ-style Q&A content is present when rendered, absent from the raw fetch"})

    return {
        "measured": True,
        "rendered_word_count": rendered_words,
        "raw_word_count": raw_words,
        "text_visibility_ratio": visibility_ratio,
        "missing_elements": missing,
        "bot_status_code": bot_status_code,
        "bot_blocked_or_errored": bool(bot_error) or (bot_status_code is not None and bot_status_code >= 400),
        "bot_fetch_error": bot_error,
    }


async def measure_page(url: str, rendered_html: str, user_agent: str = DEFAULT_BOT_USER_AGENT) -> dict:
    """Fetch as a bot and compare against an already-rendered capture. The one function most callers need."""
    bot = await fetch_as_bot(url, user_agent=user_agent)
    return compare_visibility(rendered_html, bot.html, bot_status_code=bot.status_code, bot_error=bot.error)


DEFAULT_BACKFILL_CAP = 200   # plain HTTP GETs, not Selenium loads -- cheap, but still a real site's bandwidth


async def backfill_js_visibility(html_dir: str, sitemap_url: str, max_pages: Optional[int] = None,
                                 concurrency: int = 5) -> dict:
    """
    Measure JS-rendering visibility for pages that were scraped before this
    existed. Needs no browser: the scraper's own stored HTML *is* the
    rendered capture (Selenium already ran when it was saved) -- this only
    needs one plain HTTP GET per page for the raw side.

    Capped (JS_VISIBILITY_MAX_PAGES, default 200): unlike axe's Selenium
    backfill this is cheap per page, but still real requests against a real
    site nobody explicitly asked for. Pages past the cap stay unmeasured and
    are reported as such, same convention as core/axe_runner.py's backfill.
    """
    import asyncio

    from core.web_scraper import fetch_sitemap_urls, safe_filename_stem

    cap = max_pages if max_pages is not None else int(os.getenv("JS_VISIBILITY_MAX_PAGES", DEFAULT_BACKFILL_CAP))

    todo = []
    for entry in fetch_sitemap_urls(sitemap_url):
        html_path = os.path.join(html_dir, safe_filename_stem(entry.url) + ".html")
        if os.path.exists(html_path) and not os.path.exists(raw_text_sidecar_path(html_path)):
            todo.append((entry.url, html_path))

    result = {"candidates": len(todo), "measured": 0, "failed": 0,
             "skipped_over_cap": max(0, len(todo) - cap)}
    todo = todo[:cap]
    if not todo:
        return result

    semaphore = asyncio.Semaphore(concurrency)

    async def _one(url: str, html_path: str) -> bool:
        async with semaphore:
            try:
                with open(html_path, "r", encoding="utf-8", errors="replace") as fh:
                    rendered_html = fh.read()
            except OSError as exc:
                logger.warning("JS-visibility backfill could not read %s: %s", html_path, exc)
                return False
            try:
                facts = await measure_page(url, rendered_html)
            except Exception as exc:
                logger.warning("JS-visibility backfill failed for %s: %s", url, exc)
                return False
            write_js_visibility_facts(html_path, facts)
            return True

    outcomes = await asyncio.gather(*(_one(url, path) for url, path in todo))
    result["measured"] = sum(outcomes)
    result["failed"] = len(outcomes) - result["measured"]
    return result


def format_js_visibility_facts_block(facts: Optional[dict]) -> str:
    """Render a page's raw-vs-rendered comparison as ground truth for the GEO prompts."""
    lines = ["=== AI-CRAWLER VISIBILITY FACTS (measured: a plain HTTP fetch with GPTBot's own "
             "User-Agent, compared against this page rendered in a real browser -- "
             "do NOT contradict these) ==="]

    if not facts or not facts.get("measured"):
        # Never render "not measured" as "fully visible": that would hand an
        # unmeasured page a clean bill of AI-crawler visibility.
        lines.append(
            "no automated measurement for this page -- do NOT claim this content is or "
            "isn't visible to AI crawlers on measured grounds; judge only from the text "
            "content itself"
        )
        lines.append("=== END AI-CRAWLER VISIBILITY FACTS -- page content follows below ===")
        return "\n".join(lines)

    if facts.get("bot_blocked_or_errored"):
        code = facts.get("bot_status_code")
        detail = f"HTTP {code}" if code else (facts.get("bot_fetch_error") or "request failed")
        lines.append(
            f"CRITICAL: a plain bot-UA fetch of this page did not succeed ({detail}) -- "
            "this page may be entirely inaccessible to GPTBot/ClaudeBot/PerplexityBot, "
            "which matters more than any content-visibility gap below"
        )

    ratio = facts.get("text_visibility_ratio")
    rendered_wc = facts.get("rendered_word_count", 0)
    raw_wc = facts.get("raw_word_count", 0)
    if ratio is None:
        lines.append(f"rendered page: {rendered_wc} words; raw fetch: {raw_wc} words (ratio unavailable)")
    else:
        lines.append(
            f"rendered page: {rendered_wc} words; raw bot-UA fetch: {raw_wc} words "
            f"({ratio:.0%} of rendered content is visible to a non-rendering AI crawler)"
        )

    missing = facts.get("missing_elements") or []
    if missing:
        lines.append(f"{len(missing)} element(s) present when rendered but MISSING from the raw fetch "
                     "AI crawlers actually receive:")
        for m in missing:
            lines.append(f"- {m['element']}: {m['detail']}")
    elif ratio is not None:
        lines.append("no specific elements (H1, structured data, opening paragraph, prices/figures, "
                     "FAQ content) were found missing from the raw fetch")

    lines.append("=== END AI-CRAWLER VISIBILITY FACTS -- page content follows below ===")
    return "\n".join(lines)
