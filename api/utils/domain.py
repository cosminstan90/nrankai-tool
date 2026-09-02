"""Shared domain-string helpers."""


def strip_www(domain: str) -> str:
    """Remove a leading "www." prefix (case-insensitively).

    NOT equivalent to ``domain.lstrip("www.")``, which strips a character
    *set* ({'w', '.'}) rather than a literal prefix -- silently corrupting
    any domain that merely starts with the letter "w" but isn't "www.":
    "wework.com" -> "ework.com", "webflow.com" -> "ebflow.com",
    "weebly.com" -> "eebly.com", "wetransfer.com" -> "etransfer.com". This
    exact bug was duplicated across ~10 files in this codebase wherever a
    domain needed www-normalisation before comparison.
    """
    if domain.lower().startswith("www."):
        return domain[4:]
    return domain
