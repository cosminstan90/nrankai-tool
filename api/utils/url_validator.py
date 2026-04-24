"""Shared URL and path safety utilities."""
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse

BLOCKED_HOSTS = {
    "localhost", "0.0.0.0", "metadata.google.internal",
    "169.254.169.254", "::1",
}


def validate_external_url(url: str, field_name: str = "url") -> str:
    """
    Raises ValueError if URL points to private/internal network.
    Call this before any outbound HTTP request to a user-supplied URL.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field_name} must use http or https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"{field_name} has no hostname")
    if host in BLOCKED_HOSTS:
        raise ValueError(f"{field_name} must not point to internal hosts")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"{field_name} must not point to private/reserved IP ranges")
    except ValueError as e:
        # If our own ValueError, re-raise it
        if field_name in str(e) or "must not" in str(e) or "must use" in str(e) or "no hostname" in str(e):
            raise
        # Otherwise it's a domain name (not an IP) — allowed
    return url


def sanitize_website_for_path(website: str) -> str:
    """
    Convert a website/domain string to a safe directory name.
    Raises ValueError if result is empty or looks like path traversal.
    """
    # Strip scheme
    clean = re.sub(r'^https?://', '', website.lower().strip())
    # Remove path, query, fragment — keep only host
    clean = clean.split('/')[0].split('?')[0].split('#')[0]
    # Allow only alphanumeric, dots, dashes
    clean = re.sub(r'[^a-z0-9.\-]', '_', clean)
    clean = clean.strip('._-')
    if not clean or len(clean) < 3:
        raise ValueError(f"Invalid website for directory name: {website!r}")
    if '..' in clean:
        raise ValueError(f"Path traversal detected in website: {website!r}")
    return clean


def safe_work_dir(website: str, base_dir: Path) -> Path:
    """
    Build a safe subdirectory path under base_dir for the given website.
    Raises ValueError if the resolved path escapes base_dir.
    """
    safe_name = sanitize_website_for_path(website)
    target = (base_dir / safe_name).resolve()
    base_resolved = base_dir.resolve()
    sep = '\\' if '\\' in str(base_resolved) else '/'
    if not str(target).startswith(str(base_resolved) + sep):
        raise ValueError(f"Path traversal detected: resolved path escapes base_dir")
    return target
