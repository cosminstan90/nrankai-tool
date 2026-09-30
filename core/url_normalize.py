"""
URL normalization for matching the same page across independent sources --
Pasul 10 of docs/superpowers/plans/2026-09-30-next-steps.md.

Page snapshots, GSC history, SERP rank observations and AI citation results
each store a URL string in whatever form the source that produced it used
(http vs https, a trailing slash, a tracking query string). None of that
distinguishes the page for the timeline's purpose, so everything is compared
through this normal form instead of by raw string equality.
"""
from urllib.parse import urlparse

from api.utils.domain import strip_www


def normalize_url(url: str) -> str:
    """
    host + path, lowercased, www-stripped, scheme/query/fragment dropped, no
    trailing slash (except the bare root). "" for a falsy input, so a missing
    URL never accidentally matches another missing URL as if both were "/".
    """
    if not url:
        return ""
    candidate = url if "://" in url else f"https://{url}"
    parsed = urlparse(candidate)
    host = strip_www((parsed.hostname or "")).lower()
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return f"{host}{path}"
