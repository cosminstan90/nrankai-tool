"""
DataForSEO location codes -- the single source of truth.

Every code below was checked against DataForSEO's own reference lists
(/v3/serp/google/locations and /v3/keywords_data/google_ads/locations, both
free to call) on 2026-09-29. Before this module existed the codebase carried
its own codes in four places and Romania was wrong in all of them:

  * SerpIQ and ClusterIQ used 2040, labelled "Romania" -- 2040 is AUSTRIA.
    SerpIQ's UI dropdown sent 2040 for the "🇷🇴 România" option, so every
    SerpIQ analysis ranked against google.at.
  * keyword_research used 1037, which exists on neither list.
  * The AI Overview check defaulted to 2840 (United States), so Romanian
    queries for a .ro site would have been checked against Google US.

Every other country in keyword_research's presets was already correct.
Romania is the one that matters most here -- it is the primary market -- and
it was the one that was wrong everywhere. Import from this module rather than
writing a number inline.
"""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class Location:
    key: str
    location_code: int
    language_code: str
    language_name: str
    country_name: str


# Verified 2026-09-29 against both DataForSEO reference lists.
LOCATIONS = {
    "RO": Location("RO", 2642, "ro", "Romanian", "Romania"),
    "US": Location("US", 2840, "en", "English (US)", "United States"),
    "UK": Location("UK", 2826, "en", "English (UK)", "United Kingdom"),
    "DE": Location("DE", 2276, "de", "German", "Germany"),
    "FR": Location("FR", 2250, "fr", "French", "France"),
    "IT": Location("IT", 2380, "it", "Italian", "Italy"),
    "ES": Location("ES", 2724, "es", "Spanish", "Spain"),
    "PL": Location("PL", 2616, "pl", "Polish", "Poland"),
    "NL": Location("NL", 2528, "nl", "Dutch", "Netherlands"),
    "BG": Location("BG", 2100, "bg", "Bulgarian", "Bulgaria"),
}

DEFAULT_KEY = "RO"

# Country-code TLDs that identify a market unambiguously. Generic TLDs (.com,
# .net, .org, .io ...) say nothing about where a site competes, so they are
# deliberately absent -- falling back is better than guessing.
_TLD_TO_KEY = {
    "ro": "RO",
    "uk": "UK",   # covers .co.uk / .org.uk via the last label
    "de": "DE",
    "fr": "FR",
    "it": "IT",
    "es": "ES",
    "pl": "PL",
    "nl": "NL",
    "bg": "BG",
    "us": "US",
}

# Only used when the TLD is generic. Matches CitationTracker.language values.
_LANGUAGE_TO_KEY = {
    "romanian": "RO",
    "german": "DE",
    "french": "FR",
    "italian": "IT",
    "spanish": "ES",
    "polish": "PL",
    "dutch": "NL",
    "bulgarian": "BG",
}


def get(key: str) -> Location:
    """Look up a preset by key, e.g. get("RO"). Raises KeyError on unknown keys."""
    return LOCATIONS[key.upper()]


def _tld_key(website: str) -> Optional[str]:
    host = urlparse(website if "://" in website else f"https://{website}").hostname or ""
    tld = host.rsplit(".", 1)[-1].lower() if "." in host else ""
    return _TLD_TO_KEY.get(tld)


def resolve_serp_location(website: str,
                          language: Optional[str] = None,
                          override: Optional[str] = None) -> Location:
    """
    Pick the market a site's rankings should be checked in.

    Order: explicit override, then country-code TLD, then language, then US.

    The TLD outranks the language on purpose. The one real tracker in this
    database is ing.ro with language="English" -- the column's default, never
    changed -- while every one of its tracking queries is Romanian. Trusting
    the language field would have checked a Romanian bank's Romanian queries
    against Google US.

    The override exists for the case neither signal can catch: a site on a
    generic TLD (a Romanian brand on .com) competing in one specific market.
    """
    if override and override.upper() in LOCATIONS:
        return LOCATIONS[override.upper()]

    by_tld = _tld_key(website or "")
    if by_tld:
        return LOCATIONS[by_tld]

    by_language = _LANGUAGE_TO_KEY.get((language or "").strip().lower())
    if by_language:
        return LOCATIONS[by_language]

    return LOCATIONS["US"]
