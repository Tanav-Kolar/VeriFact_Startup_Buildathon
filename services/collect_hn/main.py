"""Hacker News Algolia collector Cloud Run Job (Spec 04)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

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
    parse_iso_utc,
)

logger = logging.getLogger("collect_hn")

HN_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
HITS_PER_PAGE = 100
SOURCE_NAME = "hn"
HTTP_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3

WriterFn = Callable[..., WriteResult]


def _log_json(level: int, event: str, **fields: Any) -> None:
    payload = {"event": event, "service": "collect-hn", **fields}
    logger.log(level, json.dumps(payload, default=str))


def _parse_hn_created_at(item: dict[str, Any]) -> datetime | None:
    raw_i = item.get("created_at_i")
    if isinstance(raw_i, (int, float)):
        return datetime.fromtimestamp(float(raw_i), tz=timezone.utc)
    raw_iso = item.get("created_at")
    if isinstance(raw_iso, str) and raw_iso.strip():
        try:
            return parse_iso_utc(raw_iso)
        except ValueError:
            return None
    return None


def _is_within_bounds(dt: datetime | None, bounds: TimeBounds) -> bool:
    if dt is None:
        return False
    return bounds.window_start <= dt <= bounds.window_end


def _is_story_hit(hit: dict[str, Any]) -> bool:
    tags = hit.get("_tags")
    if isinstance(tags, list) and "story" in tags:
        return True
    if hit.get("type") == "story":
        return True
    return hit.get("parent_id") is None and hit.get("title") is not None


def _flatten_comment_tree(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Recursively collect comment objects from an Algolia /items/<id> tree."""
    comments: list[dict[str, Any]] = []
    for child in node.get("children") or []:
        if not isinstance(child, dict):
            continue
        if child.get("id") is not None and child.get("type", "comment") == "comment":
            comments.append(child)
        comments.extend(_flatten_comment_tree(child))
    return comments


def _fetch_json_with_retry(
    client: httpx.Client,
    url: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data
            return {}
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            _log_json(
                logging.WARNING,
                "hn_http_retry",
                url=url,
                attempt=attempt + 1,
                error=str(exc),
            )
    raise RuntimeError(f"HN API request failed after {MAX_RETRIES} attempts: {last_exc}") from last_exc


def _collect_story_thread(
    cfg: EventConfig,
    story_id: str,
    bounds: TimeBounds,
    *,
    run_id: str,
    collected_at: datetime,
    http_client: httpx.Client,
    firestore_client: Any | None,
    writer: WriterFn,
) -> int:
    thread_query_id = f"thread:{story_id}"
    existing = get_cursor(cfg.event_id, SOURCE_NAME, thread_query_id, client=firestore_client)
    if existing is not None and existing.step_done:
        return 0

    tree = _fetch_json_with_retry(http_client, f"{HN_ALGOLIA_BASE}/items/{story_id}")
    comments = _flatten_comment_tree(tree)
    rows: list[RawItem] = []
    for comment in comments:
        created_at = _parse_hn_created_at(comment)
        if not _is_within_bounds(created_at, bounds):
            continue
        comment_id = str(comment["id"])
        rows.append(
            RawItem(
                event_id=cfg.event_id,
                source=SOURCE_NAME,
                source_id=comment_id,
                query_id=thread_query_id,
                run_id=run_id,
                captured_by="api",
                collected_at=collected_at,
                payload=comment,
            )
        )

    written = 0
    if rows:
        result = writer(rows, run_id=run_id)
        written = result.written

    set_cursor(
        cfg.event_id,
        SOURCE_NAME,
        thread_query_id,
        story_id,
        step_done=True,
        updated_at=collected_at,
        client=firestore_client,
    )
    return written


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
) -> dict[str, Any]:
    """Run the HN Algolia collector for one configured event."""
    if cfg.sources.hn is None or not cfg.sources.hn.enabled:
        _log_json(logging.INFO, "collector_disabled", event_id=cfg.event_id, source=SOURCE_NAME)
        return {"written": 0, "queries": 0, "skipped": True}

    current_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    active_run_id = run_id or f"hn-{uuid.uuid4().hex[:12]}"
    queries = build_queries(cfg, SOURCE_NAME)

    if dry_run:
        bounds = compute_time_bounds(cfg, since_iso=since_iso, now=current_now)
        summary = {
            "event": "dry_run",
            "service": "collect-hn",
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
    total_written = 0
    seen_story_ids: set[str] = set()

    try:
        for query in queries:
            cursor_obj = get_cursor(cfg.event_id, SOURCE_NAME, query.query_id, client=firestore_client)
            cursor_iso = cursor_obj.cursor if cursor_obj is not None else None
            bounds = compute_time_bounds(
                cfg, cursor_iso=cursor_iso, since_iso=since_iso, now=current_now
            )
            if not bounds.should_run or bounds.start >= bounds.end:
                continue

            from_ts = int(bounds.start.timestamp())
            to_ts = int(bounds.end.timestamp())
            page = 0
            while True:
                params = {
                    "query": query.text,
                    "tags": "(story,comment)",
                    "numericFilters": f"created_at_i>={from_ts},created_at_i<{to_ts}",
                    "hitsPerPage": HITS_PER_PAGE,
                    "page": page,
                }
                data = _fetch_json_with_retry(
                    client, f"{HN_ALGOLIA_BASE}/search_by_date", params=params
                )
                hits = data.get("hits") or []
                if not hits:
                    break

                page_rows: list[RawItem] = []
                new_story_ids: list[str] = []
                for hit in hits:
                    if not isinstance(hit, dict):
                        continue
                    created_at = _parse_hn_created_at(hit)
                    if not _is_within_bounds(created_at, bounds):
                        continue
                    if created_at is not None and created_at < bounds.start:
                        continue
                    source_id = str(hit.get("objectID") or hit.get("id") or "").strip()
                    if not source_id:
                        continue
                    page_rows.append(
                        RawItem(
                            event_id=cfg.event_id,
                            source=SOURCE_NAME,
                            source_id=source_id,
                            query_id=query.query_id,
                            run_id=active_run_id,
                            captured_by="api",
                            collected_at=current_now,
                            payload=hit,
                        )
                    )
                    if _is_story_hit(hit) and source_id not in seen_story_ids:
                        seen_story_ids.add(source_id)
                        new_story_ids.append(source_id)

                if page_rows:
                    res = writer(page_rows, run_id=active_run_id)
                    total_written += res.written

                for story_id in new_story_ids:
                    total_written += _collect_story_thread(
                        cfg,
                        story_id,
                        bounds,
                        run_id=active_run_id,
                        collected_at=current_now,
                        http_client=client,
                        firestore_client=firestore_client,
                        writer=writer,
                    )

                nb_pages = int(data.get("nbPages") or 0)
                is_last_page = (page + 1 >= nb_pages) or (len(hits) < HITS_PER_PAGE)
                set_cursor(
                    cfg.event_id,
                    SOURCE_NAME,
                    query.query_id,
                    format_iso_utc(bounds.end) if is_last_page else format_iso_utc(bounds.start),
                    step_done=is_last_page,
                    updated_at=current_now,
                    client=firestore_client,
                )
                if is_last_page:
                    break
                page += 1

            # Ensure cursor advances even when 0 hits were returned for this window
            set_cursor(
                cfg.event_id,
                SOURCE_NAME,
                query.query_id,
                format_iso_utc(bounds.end),
                step_done=True,
                updated_at=current_now,
                client=firestore_client,
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
    parser = argparse.ArgumentParser(description="Hacker News Algolia collector")
    parser.add_argument("--event", required=True, help="Path to event YAML config")
    parser.add_argument("--dry-run", action="store_true", help="Print queries and bounds without API calls")
    parser.add_argument("--since", default=None, help="Override start timestamp (ISO-8601 UTC)")
    args = parser.parse_args(argv)

    cfg = load_event(args.event)
    run_collector(cfg, dry_run=args.dry_run, since_iso=args.since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
