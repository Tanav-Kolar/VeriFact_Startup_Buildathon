"""Shared query builder and time-bound utilities for collectors (Spec 04)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict

from verifact_core.config import EventConfig

SUPPORTED_PLATFORMS: frozenset[str] = frozenset({"hn", "youtube", "gdelt", "x"})
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG_PREFIX_LEN = 40


class Query(BaseModel):
    """Immutable query descriptor shared across collectors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    text: str


class TimeBounds(BaseModel):
    """Effective UTC collection bounds clamped to [t0 - pre_hours, t0 + post_hours]."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    window_start: datetime
    window_end: datetime
    start: datetime
    end: datetime
    should_run: bool


def _slugify_query(text: str) -> str:
    """Produce a deterministic, collision-free kebab-case slug for query text."""
    normalized = text.strip().lower()
    prefix = _NON_ALNUM_RE.sub("-", normalized).strip("-")[:_MAX_SLUG_PREFIX_LEN].rstrip("-")
    short_hash = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{short_hash}" if prefix else f"q-{short_hash}"


def _format_and_term(term: str) -> str:
    """Quote multi-word terms unless already quoted."""
    cleaned = term.strip()
    if not cleaned:
        return ""
    if " " in cleaned and '"' not in cleaned:
        return f'"{cleaned}"'
    return cleaned


def _combine_and(left: str, right: str, platform: str) -> str:
    """Combine two terms as an AND expression in the target platform's syntax."""
    a = _format_and_term(left)
    b = _format_and_term(right)
    if not a or not b:
        return a or b
    # All supported platforms (hn Algolia, youtube Data API v3, gdelt DOC 2.0, x v2)
    # interpret whitespace-separated terms/phrases as logical AND.
    _ = platform
    return f"{a} {b}"


def _collect_org_terms(cfg: EventConfig) -> list[str]:
    """Return deduplicated org canonical names and aliases from config."""
    terms: list[str] = []
    seen_lower: set[str] = set()
    for org_name, aliases in cfg.entities.org.items():
        for candidate in (org_name, *aliases):
            cleaned = candidate.strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key not in seen_lower:
                seen_lower.add(key)
                terms.append(cleaned)
    return terms


def _matches_exclude(text: str, exclude_terms: list[str]) -> bool:
    """Return True if any configured exclude term appears in text (case-insensitive)."""
    text_lower = text.lower()
    return any(ex.strip().lower() in text_lower for ex in exclude_terms if ex.strip())


def build_queries(cfg: EventConfig, platform: str) -> list[Query]:
    """Build platform queries strictly from event config values."""
    platform_norm = platform.strip().lower()
    if platform_norm not in SUPPORTED_PLATFORMS:
        raise ValueError(
            f"Unsupported platform {platform!r}; expected one of {sorted(SUPPORTED_PLATFORMS)}"
        )

    candidates: list[str] = []
    for core_term in cfg.keywords.core:
        cleaned = core_term.strip()
        if cleaned:
            candidates.append(cleaned)

    org_terms = _collect_org_terms(cfg)
    for context_term in cfg.keywords.context:
        cleaned_ctx = context_term.strip()
        if not cleaned_ctx:
            continue
        for org_term in org_terms:
            combined = _combine_and(org_term, cleaned_ctx, platform_norm)
            if combined:
                candidates.append(combined)

    queries: list[Query] = []
    seen_text: set[str] = set()
    for text in candidates:
        if _matches_exclude(text, cfg.keywords.exclude):
            continue
        norm_key = text.lower()
        if norm_key in seen_text:
            continue
        seen_text.add(norm_key)
        queries.append(Query(query_id=_slugify_query(text), text=text))

    return queries


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp string into a timezone-aware UTC datetime."""
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_iso_utc(dt: datetime) -> str:
    """Format a UTC datetime as ISO-8601 with trailing Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_time_bounds(
    cfg: EventConfig,
    *,
    cursor_iso: str | None = None,
    since_iso: str | None = None,
    now: datetime | None = None,
) -> TimeBounds:
    """Compute effective [start, end] bounds for a collector run."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_start = cfg.t0.utc - timedelta(hours=cfg.window.pre_hours)
    window_end = cfg.t0.utc + timedelta(hours=cfg.window.post_hours)

    if current > window_end and not cfg.window.live:
        return TimeBounds(
            window_start=window_start,
            window_end=window_end,
            start=window_end,
            end=window_end,
            should_run=False,
        )

    effective_end = min(current, window_end)

    if since_iso:
        effective_start = max(parse_iso_utc(since_iso), window_start)
    elif cursor_iso:
        effective_start = max(parse_iso_utc(cursor_iso), window_start)
    else:
        effective_start = window_start

    effective_start = min(effective_start, effective_end)
    return TimeBounds(
        window_start=window_start,
        window_end=window_end,
        start=effective_start,
        end=effective_end,
        should_run=True,
    )
