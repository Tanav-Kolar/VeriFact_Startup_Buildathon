"""GDELT DOC 2.0 collector Cloud Run Job (Spec 04)."""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import logging
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from verifact_core.bq import write_raw_items
from verifact_core.config import EventConfig, load_event
from verifact_core.cursors import get_cursor, set_cursor
from verifact_core.models import RawItem, WriteResult
from verifact_core.queries import (
    TimeBounds,
    build_queries,
    compute_time_bounds,
    format_iso_utc,
)

logger = logging.getLogger("collect_gdelt")

GDELT_DOC_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
GOOGLE_NEWS_RSS_ENDPOINT = "https://news.google.com/rss/search"
SOURCE_NAME = "gdelt"
MAX_RECORDS = 250
MIN_SPLIT_WINDOW = timedelta(minutes=15)
RATE_LIMIT_SECONDS = 5.0
HTTP_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3

_TRACKING_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "twclid",
        "yclid",
        "_hsenc",
        "_hsmi",
        "mkt_tok",
        "ref",
        "ref_src",
        "ref_url",
        "ocid",
        "ncid",
    }
)

WriterFn = Callable[..., WriteResult]


class RateLimiter:
    """Shared minimum-interval rate limiter with injectable clock and sleeper."""

    def __init__(
        self,
        *,
        min_interval: float = RATE_LIMIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = min_interval
        self._clock = clock
        self._sleeper = sleeper
        self._last_call: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last_call is not None:
            elapsed = now - self._last_call
            remaining = self._min_interval - elapsed
            if remaining > 0:
                self._sleeper(remaining)
        self._last_call = self._clock()


def _log_json(level: int, event: str, **fields: Any) -> None:
    payload = {"event": event, "service": "collect-gdelt", **fields}
    logger.log(level, json.dumps(payload, default=str))


def _is_tracking_param(name: str) -> bool:
    lower = name.strip().lower()
    return lower.startswith("utm_") or lower in _TRACKING_PARAM_NAMES


def normalize_url(url: str) -> str:
    """Normalize URL: lowercase scheme and host, strip tracking params and fragment."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    kept_params = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    kept_params.sort()
    query = urlencode(kept_params, doseq=True)
    return urlunsplit((scheme, netloc, parts.path, query, ""))


def url_sha1(url: str) -> str:
    """Return SHA-1 hex digest of the normalized URL."""
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()


def _format_gdelt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def _parse_seendate(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_within_bounds(dt: datetime | None, bounds: TimeBounds) -> bool:
    if dt is None:
        return False
    return bounds.window_start <= dt <= bounds.window_end


def _fetch_gdelt_window(
    client: httpx.Client,
    limiter: RateLimiter,
    query_text: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict[str, Any]]:
    params = {
        "query": query_text,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": MAX_RECORDS,
        "sort": "DateAsc",
        "startdatetime": _format_gdelt_dt(start_dt),
        "enddatetime": _format_gdelt_dt(end_dt),
    }
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        limiter.wait()
        try:
            resp = client.get(GDELT_DOC_ENDPOINT, params=params, timeout=HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            if not resp.text.strip():
                return []
            data = resp.json()
            if isinstance(data, dict):
                articles = data.get("articles") or []
                return [a for a in articles if isinstance(a, dict)]
            return []
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            _log_json(
                logging.WARNING,
                "gdelt_http_retry",
                attempt=attempt + 1,
                error=str(exc),
            )
    raise RuntimeError(f"GDELT DOC 2.0 request failed after {MAX_RETRIES} attempts: {last_exc}") from last_exc


def _collect_window_recursive(
    cfg: EventConfig,
    query_id: str,
    query_text: str,
    start_dt: datetime,
    end_dt: datetime,
    bounds: TimeBounds,
    *,
    run_id: str,
    collected_at: datetime,
    http_client: httpx.Client,
    limiter: RateLimiter,
    firestore_client: Any | None,
    writer: WriterFn,
) -> int:
    articles = _fetch_gdelt_window(http_client, limiter, query_text, start_dt, end_dt)

    if len(articles) >= MAX_RECORDS:
        duration = end_dt - start_dt
        if duration > MIN_SPLIT_WINDOW:
            half_seconds = int(duration.total_seconds() // 2)
            mid_dt = start_dt + timedelta(seconds=half_seconds)
            if mid_dt > start_dt and mid_dt < end_dt:
                left = _collect_window_recursive(
                    cfg,
                    query_id,
                    query_text,
                    start_dt,
                    mid_dt,
                    bounds,
                    run_id=run_id,
                    collected_at=collected_at,
                    http_client=http_client,
                    limiter=limiter,
                    firestore_client=firestore_client,
                    writer=writer,
                )
                right = _collect_window_recursive(
                    cfg,
                    query_id,
                    query_text,
                    mid_dt,
                    end_dt,
                    bounds,
                    run_id=run_id,
                    collected_at=collected_at,
                    http_client=http_client,
                    limiter=limiter,
                    firestore_client=firestore_client,
                    writer=writer,
                )
                return left + right
        _log_json(
            logging.WARNING,
            "gdelt_window_saturated",
            event_id=cfg.event_id,
            query_id=query_id,
            start=format_iso_utc(start_dt),
            end=format_iso_utc(end_dt),
            count=len(articles),
        )

    rows: list[RawItem] = []
    for article in articles:
        url = article.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        seen_dt = _parse_seendate(article.get("seendate"))
        if not _is_within_bounds(seen_dt, bounds):
            continue
        if seen_dt is not None and seen_dt < bounds.start:
            continue
        rows.append(
            RawItem(
                event_id=cfg.event_id,
                source=SOURCE_NAME,
                source_id=url_sha1(url),
                query_id=query_id,
                run_id=run_id,
                captured_by="api",
                collected_at=collected_at,
                payload=article,
            )
        )

    written = 0
    if rows:
        res = writer(rows, run_id=run_id)
        written = res.written

    set_cursor(
        cfg.event_id,
        SOURCE_NAME,
        query_id,
        format_iso_utc(end_dt),
        step_done=end_dt >= bounds.end,
        updated_at=collected_at,
        client=firestore_client,
    )
    return written


def _parse_rss_pubdate(raw: str | None) -> datetime | None:
    if not raw or not raw.strip():
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _collect_google_news_rss(
    cfg: EventConfig,
    *,
    run_id: str,
    collected_at: datetime,
    bounds: TimeBounds,
    http_client: httpx.Client,
    writer: WriterFn,
) -> int:
    """Fetch public Google News RSS articles for configured core/context queries."""
    rss_queries = build_queries(cfg, SOURCE_NAME)[:6]
    seen_hashes: set[str] = set()
    rows: list[RawItem] = []

    for q in rss_queries:
        params = {"q": q.text, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
        try:
            resp = http_client.get(GOOGLE_NEWS_RSS_ENDPOINT, params=params)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
        except (httpx.HTTPError, ET.ParseError) as exc:
            _log_json(logging.WARNING, "google_news_rss_error", query=q.text, error=str(exc))
            continue

        for item in root.findall(".//item"):
            link = (item.findtext("link") or "").strip()
            title = (item.findtext("title") or "").strip()
            if not link or not title:
                continue
            pub_dt = _parse_rss_pubdate(item.findtext("pubDate"))
            if pub_dt is None or not (bounds.window_start <= pub_dt <= bounds.window_end):
                continue
            source_el = item.find("source")
            source_url = (source_el.attrib.get("url") if source_el is not None else "") or ""
            source_name = (source_el.text or "").strip() if source_el is not None else ""
            domain = urlsplit(source_url).netloc.lower() if source_url else urlsplit(link).netloc.lower()
            src_hash = url_sha1(link)
            if src_hash in seen_hashes:
                continue
            seen_hashes.add(src_hash)
            payload = {
                "url": link,
                "title": title,
                "domain": domain,
                "source_name": source_name,
                "seendate": pub_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                "language": "English",
                "via": "google_news_rss",
            }
            rows.append(
                RawItem(
                    event_id=cfg.event_id,
                    source=SOURCE_NAME,
                    source_id=src_hash,
                    query_id=f"rss:{q.query_id}",
                    run_id=run_id,
                    captured_by="api",
                    collected_at=collected_at,
                    payload=payload,
                )
            )

    if not rows:
        return 0
    result = writer(rows, run_id=run_id)
    return result.written


def run_collector(
    cfg: EventConfig,
    *,
    dry_run: bool = False,
    since_iso: str | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
    http_client: httpx.Client | None = None,
    firestore_client: Any | None = None,
    writer: WriterFn = write_raw_items,
    rate_limiter: RateLimiter | None = None,
    rss_only: bool = False,
) -> dict[str, Any]:
    """Run the GDELT DOC 2.0 (and optional Google News RSS) collector for one configured event."""
    if cfg.sources.news is None or not cfg.sources.news.enabled or (
        not cfg.sources.news.gdelt and not cfg.sources.news.google_news_rss
    ):
        _log_json(logging.INFO, "collector_disabled", event_id=cfg.event_id, source=SOURCE_NAME)
        return {"written": 0, "queries": 0, "skipped": True}

    current_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    active_run_id = run_id or f"gdelt-{uuid.uuid4().hex[:12]}"
    queries = build_queries(cfg, SOURCE_NAME)

    if dry_run:
        bounds = compute_time_bounds(cfg, since_iso=since_iso, now=current_now)
        summary = {
            "event": "dry_run",
            "service": "collect-gdelt",
            "event_id": cfg.event_id,
            "should_run": bounds.should_run,
            "start": format_iso_utc(bounds.start),
            "end": format_iso_utc(bounds.end),
            "window_start": format_iso_utc(bounds.window_start),
            "window_end": format_iso_utc(bounds.window_end),
            "queries": [{"query_id": q.query_id, "text": q.text} for q in queries],
        }
        sys.stdout.write(json.dumps(summary) + "\n")
        return {"written": 0, "queries": len(queries), "dry_run": True}

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    limiter = rate_limiter or RateLimiter()
    total_written = 0

    try:
        if rss_only and cfg.sources.news.google_news_rss:
            bounds = compute_time_bounds(cfg, since_iso=since_iso, now=current_now)
            if bounds.should_run and bounds.start < bounds.end:
                total_written += _collect_google_news_rss(
                    cfg,
                    run_id=active_run_id,
                    collected_at=current_now,
                    bounds=bounds,
                    http_client=client,
                    writer=writer,
                )
        else:
            for query in queries:
                cursor_obj = get_cursor(cfg.event_id, SOURCE_NAME, query.query_id, client=firestore_client)
                cursor_iso = cursor_obj.cursor if cursor_obj is not None else None
                bounds = compute_time_bounds(
                    cfg, cursor_iso=cursor_iso, since_iso=since_iso, now=current_now
                )
                if not bounds.should_run or bounds.start >= bounds.end:
                    continue

                total_written += _collect_window_recursive(
                    cfg,
                    query.query_id,
                    query.text,
                    bounds.start,
                    bounds.end,
                    bounds,
                    run_id=active_run_id,
                    collected_at=current_now,
                    http_client=client,
                    limiter=limiter,
                    firestore_client=firestore_client,
                    writer=writer,
                )
    finally:
        if owns_client:
            client.close()

    _log_json(
        logging.INFO,
        "collector_complete",
        event_id=cfg.event_id,
        run_id=active_run_id,
        written=total_written,
        queries=len(queries),
    )
    return {"written": total_written, "queries": len(queries), "run_id": active_run_id}


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="GDELT DOC 2.0 & Google News RSS collector")
    parser.add_argument("--event", required=True, help="Path to event YAML config")
    parser.add_argument("--dry-run", action="store_true", help="Print queries and bounds without API calls")
    parser.add_argument("--since", default=None, help="Override start timestamp (ISO-8601 UTC)")
    parser.add_argument("--rss-only", action="store_true", help="Collect via Google News RSS only")
    args = parser.parse_args(argv)

    cfg = load_event(args.event)
    run_collector(cfg, dry_run=args.dry_run, since_iso=args.since, rss_only=args.rss_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
