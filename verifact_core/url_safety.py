"""URL safety, social platform blocklist, and URL normalization (Spec 05 / P5.1)."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Non-negotiable blocklist: never scrape social platforms (P5.1, PRD IR-8)
SOCIAL_BLOCKLIST: frozenset[str] = frozenset({
    "x.com",
    "twitter.com",
    "reddit.com",
    "linkedin.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "t.me",
    "news.ycombinator.com",
})

# Standard query parameters used solely for analytics/tracking that should be stripped
# for deterministic URL deduplication and hashing
TRACKING_PARAMS: frozenset[str] = frozenset({
    "gclid",
    "fbclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "yclid",
    "_hsenc",
    "_hsmi",
})


def extract_host(url: str) -> str:
    """Extract lowercase hostname from a URL, stripping port and handling schemeless inputs."""
    cleaned = url.strip()
    if not cleaned:
        return ""
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"
    try:
        parts = urlsplit(cleaned)
        netloc = parts.netloc.lower()
        if "@" in netloc:
            netloc = netloc.split("@", 1)[1]
        if ":" in netloc:
            netloc = netloc.split(":", 1)[0]
        return netloc
    except Exception:  # noqa: BLE001
        return ""


def is_social_url(url: str) -> bool:
    """Return True if the URL's host is, or is under, any blocked social platform (P5.1)."""
    host = extract_host(url)
    if not host:
        return False

    for blocked in SOCIAL_BLOCKLIST:
        if host == blocked or host.endswith(f".{blocked}"):
            return True
    return False


def normalize_url(url: str) -> str:
    """Normalize a URL for deduplication:

    - lowercase scheme and host
    - strip fragment (#...)
    - remove tracking query parameters (utm_*, fbclid, etc.)
    - sort remaining query parameters
    - strip trailing slash if path is otherwise empty
    """
    cleaned = url.strip()
    if not cleaned:
        return ""
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"

    parsed = urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if "@" in netloc:
        userinfo, hostport = netloc.split("@", 1)
        if ":" in hostport:
            h, p = hostport.split(":", 1)
            if (scheme == "http" and p == "80") or (scheme == "https" and p == "443"):
                hostport = h
        netloc = f"{userinfo}@{hostport}"
    else:
        if ":" in netloc:
            h, p = netloc.split(":", 1)
            if (scheme == "http" and p == "80") or (scheme == "https" and p == "443"):
                netloc = h

    path = parsed.path
    if path == "/":
        path = ""

    # Filter out tracking query parameters
    query_params: list[tuple[str, str]] = []
    if parsed.query:
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            k_lower = k.lower()
            if k_lower.startswith("utm_") or k_lower in TRACKING_PARAMS:
                continue
            query_params.append((k, v))
        query_params.sort(key=lambda item: (item[0], item[1]))

    normalized_query = urlencode(query_params)
    # Fragment is dropped entirely
    return urlunsplit((scheme, netloc, path, normalized_query, ""))


def url_hash(url: str) -> str:
    """Return the SHA-1 hex digest of the normalized URL (matching GDELT source_id)."""
    normalized = normalize_url(url)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()
