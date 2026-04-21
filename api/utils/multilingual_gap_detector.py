"""
Multilingual Gap Detector (Prompt 36)
=======================================
Checks if key pages exist in the languages used by monitored prompts.
Free — uses only httpx, no paid APIs.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("multilingual_gap_detector")

COMMON_LANG_PATHS = ["en", "ro", "de", "fr", "es", "it", "nl", "pl", "pt", "ru", "hu", "cs", "sk", "bg"]


@dataclass
class MultilingualGapReport:
    target_domain: str
    detected_site_languages: List[str] = field(default_factory=list)
    prompt_languages: List[str] = field(default_factory=list)
    missing_languages: List[str] = field(default_factory=list)
    coverage_score: float = 0.0
    page_gaps: List[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    recommendations: List[str] = field(default_factory=list)
    hreflang_template: str = ""

    def to_dict(self) -> dict:
        return {
            "target_domain":           self.target_domain,
            "detected_site_languages": self.detected_site_languages,
            "prompt_languages":        self.prompt_languages,
            "missing_languages":       self.missing_languages,
            "coverage_score":          self.coverage_score,
            "page_gaps":               self.page_gaps,
            "summary":                 self.summary,
            "recommendations":         self.recommendations,
            "hreflang_template":       self.hreflang_template,
        }


def _extract_hreflang_langs(html: str) -> Set[str]:
    """Extract language codes from <link rel='alternate' hreflang='...'> tags."""
    langs: Set[str] = set()
    for m in re.finditer(r'hreflang=["\']([a-zA-Z\-]+)["\']', html, re.I):
        lang = m.group(1).lower().split("-")[0]  # "en-US" → "en"
        if lang != "x-default":
            langs.add(lang)
    return langs


def _extract_url_lang(url: str, base_domain: str) -> Optional[str]:
    """Try to detect language from URL pattern: /en/, en.domain.com"""
    parsed = urlparse(url)
    # Subdomain pattern: en.domain.com
    sub = parsed.netloc.split(".")[0].lower()
    if len(sub) == 2 and sub in COMMON_LANG_PATHS:
        return sub
    # Path pattern: /en/...
    parts = [p for p in parsed.path.split("/") if p]
    if parts and len(parts[0]) == 2 and parts[0].lower() in COMMON_LANG_PATHS:
        return parts[0].lower()
    return None


def _generate_hreflang(domain: str, languages: List[str]) -> str:
    base = domain.rstrip("/")
    lines = []
    for lang in languages:
        lines.append(f'<link rel="alternate" hreflang="{lang}" href="{base}/{lang}/" />')
    lines.append(f'<link rel="alternate" hreflang="x-default" href="{base}/" />')
    return "\n".join(lines)


async def _check_page_exists(client, url: str) -> bool:
    try:
        r = await client.head(url, timeout=8)
        return r.status_code < 400
    except Exception:
        return False


async def detect_gaps(
    target_domain: str,
    key_pages: List[str],
    prompt_languages: List[str],
    db=None,
) -> MultilingualGapReport:
    """
    Detect multilingual content gaps for the given domain and key pages.
    prompt_languages: list of 2-letter language codes, e.g. ["ro", "en"]
    key_pages: list of relative paths, e.g. ["/", "/services", "/about"]
    """
    import httpx

    report = MultilingualGapReport(
        target_domain=target_domain,
        prompt_languages=prompt_languages,
    )

    base = target_domain.strip().rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}"

    detected_langs: Set[str] = set()

    async with httpx.AsyncClient(
        timeout=12,
        follow_redirects=True,
        headers={"User-agent": "Mozilla/5.0 (compatible; nrankai-bot/1.0)"},
    ) as client:

        # Detect from homepage
        try:
            hp = await client.get(base)
            detected_langs |= _extract_hreflang_langs(hp.text)
            lang_from_url = _extract_url_lang(str(hp.url), base)
            if lang_from_url:
                detected_langs.add(lang_from_url)
        except Exception as exc:
            logger.warning("Homepage fetch failed for %s: %s", base, exc)

        # Try common lang paths if nothing detected
        if not detected_langs:
            tasks = {}
            for lang in COMMON_LANG_PATHS[:6]:  # check top 6 langs
                url = f"{base}/{lang}/"
                tasks[lang] = _check_page_exists(client, url)
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for lang, exists in zip(tasks.keys(), results):
                if exists is True:
                    detected_langs.add(lang)

        # Determine missing languages
        report.detected_site_languages = sorted(detected_langs)
        report.missing_languages = [l for l in prompt_languages if l not in detected_langs]

        # Coverage score
        if not prompt_languages:
            report.coverage_score = 100.0
        else:
            covered = len([l for l in prompt_languages if l in detected_langs])
            report.coverage_score = round(covered / len(prompt_languages) * 100, 1)

        # Page-level gap analysis (max 20 pages)
        page_gaps = []
        for page_path in (key_pages or ["/"])[:20]:
            page_url = urljoin(base + "/", page_path.lstrip("/"))
            available_in: List[str] = []
            missing_in: List[str] = []

            for lang in prompt_languages:
                # Check /lang/path or lang.domain/path
                candidates = [
                    f"{base}/{lang}{page_path if page_path.startswith('/') else '/' + page_path}",
                    f"{base}/{lang}/",
                ]
                found = False
                for cand in candidates[:1]:  # check first candidate only to limit requests
                    if await _check_page_exists(client, cand):
                        found = True
                        break
                if found:
                    available_in.append(lang)
                else:
                    missing_in.append(lang)

            if missing_in:
                # Priority: HIGH for homepage or pricing/services pages in English
                priority = "high" if (page_path in ("/", "/pricing", "/services", "/about") and "en" in missing_in) else "medium"
                page_gaps.append({
                    "url":          page_url,
                    "available_in": available_in,
                    "missing_in":   missing_in,
                    "priority":     priority,
                })

        report.page_gaps = page_gaps

        # Summary
        high_pri = [g for g in page_gaps if g["priority"] == "high"]
        report.summary = {
            "pages_with_gaps":   len(page_gaps),
            "critical_missing":  len(high_pri),
            "revenue_risk":      "high" if high_pri else "low",
        }

        # Recommendations
        recs = []
        for lang in report.missing_languages:
            recs.append(f"Add {lang.upper()} translation — {lang} prompts detected but no {lang.upper()} content found")
        if "en" in report.missing_languages:
            recs.insert(0, "⚠️ No English content detected — AI engines will not cite your site for English queries (highest priority)")
        if not report.detected_site_languages:
            recs.append("No multilingual structure detected — consider adding hreflang tags to your homepage")
        report.recommendations = recs

        # hreflang template
        all_langs = sorted(set(prompt_languages) | detected_langs)
        report.hreflang_template = _generate_hreflang(base, all_langs)

    return report
