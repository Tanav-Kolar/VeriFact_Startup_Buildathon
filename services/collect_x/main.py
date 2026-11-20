"""Scheduled live X API v2 collector job (collect-x).

Incrementally polls recent X posts for a configured event using per-query
since_id cursors stored in Firestore collector_cursors, shares the global
reads_total cap with x-backfill via verifact_core.cursors, and appends
unmodified payloads directly to BigQuery raw_items.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from verifact_core.bq import write_raw_items
from verifact_core.config import EventConfig, load_event
from verifact_core.cursors import add_reads, get_cursor, get_reads, set_cursor
from verifact_core.models import Cursor, RawItem

SOURCE_NAME = "x"
CAPTURED_BY: Literal["api"] = "api"
SEARCH_PATH = "/2/tweets/search/recent"
TWEET_FIELDS = (
    "created_at,lang,author_id,conversation_id,referenced_tweets,entities,public_metrics"
)

MAX_PAGE_RESULTS = 100
SEARCH_MIN_RESULTS = 10
MAX_QUERY_LENGTH = 512
MAX_SERVER_ERROR_RETRIES = 3
MAX_RATE_LIMIT_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 15.0

RECENT_SEARCH_LOOKBACK = timedelta(days=7)
RECENT_SEARCH_START_BUFFER = timedelta(minutes=10)
RECENT_SEARCH_END_BUFFER = timedelta(seconds=30)

StopReason = Literal["completed", "cap_reached", "window_closed", "error"]
QueryCategory = Literal["thread", "official", "keywords"]


class CollectXConfigError(ValueError):
    """Raised when event configuration or runtime settings refuse job start."""


class XApiError(RuntimeError):
    """Raised on unrecoverable X API HTTP or transport failure."""


class _XQueryAuthError(RuntimeError):
    """Internal signal to skip the current query on HTTP 401/403 and continue."""

    def __init__(self, status_code: int, query_id: str, detail: str) -> None:
        super().__init__(f"HTTP {status_code} on query {query_id}: {detail}")
        self.status_code = status_code
        self.query_id = query_id
        self.detail = detail


class _DynamicStdoutHandler(logging.Handler):
    """Logging handler writing formatted lines to the active sys.stdout."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            self.handleError(record)


def _configure_logger(level: str = "INFO") -> logging.Logger:
    log = logging.getLogger("verifact.collect_x")
    log.setLevel(level.upper())
    if not any(isinstance(h, _DynamicStdoutHandler) for h in log.handlers):
        handler = _DynamicStdoutHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
    return log


logger = _configure_logger()


def _log_json(level: int, event: str, **fields: Any) -> None:
    payload: dict[str, Any] = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **fields,
    }
    logger.log(level, json.dumps(payload, ensure_ascii=False))


class CollectXSettings(BaseSettings):
    """Environment settings for the scheduled collect-x job."""

    model_config = SettingsConfigDict(extra="ignore")

    x_bearer_token: str | None = Field(default=None, validation_alias="X_BEARER_TOKEN")
    x_api_base_url: str = Field(default="https://api.x.com", validation_alias="X_API_BASE_URL")
    http_timeout_seconds: float = Field(
        default=30.0, validation_alias="X_HTTP_TIMEOUT_SECONDS", gt=0
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


class LiveQuery(BaseModel):
    """Immutable descriptor for one live X search query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    category: QueryCategory
    query: str


class PlannedRequest(BaseModel):
    """Immutable description of one planned X API request for dry-run inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    category: QueryCategory
    method: str = "GET"
    path: str = SEARCH_PATH
    params: dict[str, str]


class CollectXSummary(BaseModel):
    """Final run summary mirrored in the collect_x_done structured log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    run_id: str
    queries_planned: int
    queries_completed: int
    queries_skipped_auth: list[str]
    reads_by_query: dict[str, int]
    reads_this_run: int
    reads_total: int
    rows_written: int
    rows_rejected: int
    stop_reason: StopReason


@dataclass
class _RunState:
    reads_by_query: dict[str, int]
    reads_total: int
    remaining_budget: int
    reads_this_run: int = 0
    rows_written: int = 0
    rows_rejected: int = 0
    queries_completed: int = 0
    queries_skipped_auth: list[str] = field(default_factory=list)
    cap_reached: bool = False


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        item = raw.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _format_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_query(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:12]


def compute_live_window(config: EventConfig, now: datetime) -> tuple[bool, datetime, datetime]:
    """Return (window_open, start_time, end_time) respecting window.live and X 7-day lookback."""
    now_utc = now.astimezone(timezone.utc)
    t0_utc = config.t0.utc.astimezone(timezone.utc)
    window_start = t0_utc - timedelta(hours=config.window.pre_hours)
    window_end = t0_utc + timedelta(hours=config.window.post_hours)

    if now_utc > window_end and not config.window.live:
        return False, window_end, window_end

    start_time = max(window_start, now_utc - RECENT_SEARCH_LOOKBACK + RECENT_SEARCH_START_BUFFER)
    end_limit = now_utc - RECENT_SEARCH_END_BUFFER
    end_time = end_limit if config.window.live else min(window_end, end_limit)
    return True, start_time, end_time


def _format_core_clause(term: str) -> str:
    cleaned = term.strip()
    return f"({cleaned})" if (" " in cleaned or '"' in cleaned) else cleaned


def _format_alias_or_term(term: str) -> str:
    cleaned = term.strip()
    if '"' in cleaned:
        return cleaned
    return f'"{cleaned}"' if " " in cleaned else cleaned


def _format_exclude_clause(terms: list[str]) -> str:
    parts: list[str] = []
    for raw in _dedupe_preserve_order(terms):
        cleaned = raw.lstrip("-").strip()
        if not cleaned:
            continue
        if '"' in cleaned:
            parts.append(f"-{cleaned}")
        elif " " in cleaned:
            parts.append(f'-"{cleaned}"')
        else:
            parts.append(f"-{cleaned}")
    return " ".join(parts)


def _pack_or_clauses(
    clauses: list[str], *, prefix: str, suffix: str, max_length: int
) -> list[str]:
    if not clauses:
        return []

    def _assemble(items: list[str]) -> str:
        body = items[0] if len(items) == 1 else f"({' OR '.join(items)})"
        return " ".join(p for p in (prefix, body, suffix) if p)

    queries: list[str] = []
    current: list[str] = []
    for clause in clauses:
        if len(_assemble([*current, clause])) <= max_length:
            current.append(clause)
            continue
        if current:
            queries.append(_assemble(current))
        single = _assemble([clause])
        if len(single) > max_length:
            raise CollectXConfigError(
                f"Single keyword query exceeds X {max_length}-char limit: {len(single)} chars"
            )
        current = [clause]
    if current:
        queries.append(_assemble(current))
    return queries


def _collect_org_aliases(config: EventConfig) -> list[str]:
    aliases: list[str] = []
    for canonical, alias_list in config.entities.org.items():
        aliases.append(canonical)
        aliases.extend(alias_list)
    return _dedupe_preserve_order(aliases)


def build_keyword_queries(config: EventConfig, max_length: int = MAX_QUERY_LENGTH) -> list[str]:
    """Build X recent search queries from core, context + org aliases, exclude, and -is:retweet."""
    exclude_str = _format_exclude_clause(config.keywords.exclude)
    suffix = f"{exclude_str} -is:retweet".strip()
    core_clauses = [_format_core_clause(t) for t in _dedupe_preserve_order(config.keywords.core)]
    queries = _pack_or_clauses(core_clauses, prefix="", suffix=suffix, max_length=max_length)

    org_aliases = [_format_alias_or_term(a) for a in _collect_org_aliases(config)]
    context_clauses = [
        _format_alias_or_term(t) for t in _dedupe_preserve_order(config.keywords.context)
    ]
    if org_aliases and context_clauses:
        org_prefix = org_aliases[0] if len(org_aliases) == 1 else f"({' OR '.join(org_aliases)})"
        queries.extend(
            _pack_or_clauses(context_clauses, prefix=org_prefix, suffix=suffix, max_length=max_length)
        )
    return queries


def build_live_queries(config: EventConfig, max_length: int = MAX_QUERY_LENGTH) -> list[LiveQuery]:
    """Build ordered live queries (threads, official accounts, keywords) from event config."""
    x_cfg = config.sources.x
    thread_ids = _dedupe_preserve_order(
        list(config.official.posts) + (list(x_cfg.thread_ids) if x_cfg else [])
    )
    handles = _dedupe_preserve_order([a.lstrip("@") for a in config.official.accounts])
    queries: list[LiveQuery] = [
        LiveQuery(query_id=f"live:thread:{pid}", category="thread", query=f"conversation_id:{pid}")
        for pid in thread_ids
    ]
    queries.extend(
        LiveQuery(query_id=f"live:official:{h.lower()}", category="official", query=f"from:{h}")
        for h in handles
    )
    queries.extend(
        LiveQuery(query_id=f"live:keywords:{_hash_query(q)}", category="keywords", query=q)
        for q in build_keyword_queries(config, max_length=max_length)
    )
    return queries


def _usable_since_id(cursor_obj: Cursor | None, now_utc: datetime, query_id: str) -> str | None:
    if cursor_obj is None or not cursor_obj.cursor or not cursor_obj.cursor.strip():
        return None
    cutoff = now_utc - RECENT_SEARCH_LOOKBACK + RECENT_SEARCH_START_BUFFER
    if cursor_obj.updated_at < cutoff:
        _log_json(
            logging.WARNING,
            "collect_x_stale_cursor_fallback",
            query_id=query_id,
            updated_at=_format_utc_iso(cursor_obj.updated_at),
        )
        return None
    return cursor_obj.cursor.strip()


def _max_snowflake_id(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if not current:
        return candidate
    if current.isdigit() and candidate.isdigit():
        return candidate if int(candidate) > int(current) else current
    return candidate


def _extract_newest_id(meta: dict[str, Any], posts: list[dict[str, Any]]) -> str | None:
    best: str | None = str(meta["newest_id"]) if meta.get("newest_id") else None
    for post in posts:
        if post.get("id") is not None:
            best = _max_snowflake_id(best, str(post["id"]))
    return best


def build_live_plan(
    config: EventConfig, now: datetime, *, cursors: dict[str, str | None] | None = None
) -> list[PlannedRequest]:
    """Return the ordered list of planned X API requests for dry-run inspection."""
    _, start_dt, end_dt = compute_live_window(config, now)
    start_iso, end_iso = _format_utc_iso(start_dt), _format_utc_iso(end_dt)
    cursor_map = cursors or {}

    planned: list[PlannedRequest] = []
    for item in build_live_queries(config):
        params: dict[str, str] = {"query": item.query, "tweet.fields": TWEET_FIELDS}
        since_id = cursor_map.get(item.query_id)
        if since_id:
            params["since_id"] = since_id
        else:
            params["start_time"] = start_iso
            params["end_time"] = end_iso
        planned.append(
            PlannedRequest(query_id=item.query_id, category=item.category, params=params)
        )
    return planned


def _validate_startup(
    config: EventConfig, settings: CollectXSettings, *, limit: int | None, dry_run: bool
) -> int:
    if limit is not None and limit <= 0:
        raise CollectXConfigError("--limit must be a positive integer")
    x_cfg = config.sources.x
    if x_cfg is None or not x_cfg.enabled:
        raise CollectXConfigError("sources.x.enabled must be true to run collect-x")
    if x_cfg.max_posts is None or x_cfg.max_posts <= 0:
        raise CollectXConfigError("sources.x.max_posts is required and must be > 0")
    if not dry_run and (not settings.x_bearer_token or not settings.x_bearer_token.strip()):
        raise CollectXConfigError(
            "X_BEARER_TOKEN environment variable is required unless --dry-run is set"
        )
    return x_cfg.max_posts


def _compute_rate_limit_wait(
    response: httpx.Response, now_fn: Callable[[], datetime], attempt: int
) -> float:
    reset_raw = response.headers.get("x-rate-limit-reset")
    if reset_raw is not None:
        try:
            delta = float(reset_raw) - now_fn().timestamp()
            return delta if delta > 0 else 1.0
        except ValueError:
            pass
    retry_after = response.headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(1.0, float(retry_after))
        except ValueError:
            pass
    return DEFAULT_RATE_LIMIT_WAIT_SECONDS * (2**attempt)


def _compute_run_budget(max_posts: int, existing_reads: int, limit: int | None) -> int:
    global_remaining = max(0, max_posts - existing_reads)
    return min(global_remaining, limit) if limit is not None else global_remaining


def _emit_done_log(summary: CollectXSummary, *, dry_run: bool = False) -> None:
    fields: dict[str, Any] = summary.model_dump()
    if dry_run:
        fields["dry_run"] = True
    _log_json(logging.INFO, "collect_x_done", **fields)


def _make_summary(
    event_id: str,
    run_id: str,
    queries_planned: int,
    state: _RunState,
    stop_reason: StopReason,
) -> CollectXSummary:
    return CollectXSummary(
        event_id=event_id,
        run_id=run_id,
        queries_planned=queries_planned,
        queries_completed=state.queries_completed,
        queries_skipped_auth=list(state.queries_skipped_auth),
        reads_by_query=dict(state.reads_by_query),
        reads_this_run=state.reads_this_run,
        reads_total=state.reads_total,
        rows_written=state.rows_written,
        rows_rejected=state.rows_rejected,
        stop_reason=stop_reason,
    )


@dataclass
class _LiveCollectorRunner:
    """Stateful executor for a single incremental collect-x run."""

    config: EventConfig
    run_id: str
    start_iso: str
    end_iso: str
    search_window_valid: bool
    bearer_token: str
    state: _RunState
    http_client: httpx.Client
    bq_client: Any | None
    firestore_client: Any | None
    now_fn: Callable[[], datetime]
    sleep_fn: Callable[[float], None]

    def _retry_sleep(self, event: str, retry: int, **fields: Any) -> None:
        wait_s = BASE_BACKOFF_SECONDS * (2 ** (retry - 1))
        _log_json(logging.WARNING, event, retry=retry, wait_seconds=wait_s, **fields)
        self.sleep_fn(wait_s)

    def _handle_non_200(
        self,
        response: httpx.Response,
        *,
        query_id: str,
        server_retries: int,
        rate_limit_retries: int,
    ) -> tuple[int, int]:
        status = response.status_code
        if status == 429:
            if rate_limit_retries >= MAX_RATE_LIMIT_RETRIES:
                raise XApiError(f"X API rate limit persisted after {MAX_RATE_LIMIT_RETRIES} retries")
            wait_s = _compute_rate_limit_wait(response, self.now_fn, rate_limit_retries)
            rate_limit_retries += 1
            _log_json(
                logging.WARNING,
                "x_rate_limited",
                event_id=self.config.event_id,
                query_id=query_id,
                wait_seconds=wait_s,
                attempt=rate_limit_retries,
            )
            self.sleep_fn(wait_s)
            return server_retries, rate_limit_retries

        if status in (401, 403):
            detail = response.text[:300]
            _log_json(
                logging.ERROR,
                "collect_x_query_auth_error",
                event_id=self.config.event_id,
                query_id=query_id,
                status_code=status,
                detail=detail,
            )
            raise _XQueryAuthError(status_code=status, query_id=query_id, detail=detail)

        if 500 <= status < 600 and server_retries < MAX_SERVER_ERROR_RETRIES:
            server_retries += 1
            self._retry_sleep(
                "x_server_error_retry",
                server_retries,
                event_id=self.config.event_id,
                query_id=query_id,
                status_code=status,
            )
            return server_retries, rate_limit_retries

        raise XApiError(f"X API HTTP {status} on {SEARCH_PATH}: {response.text[:300]}")

    def _get_json(self, query_id: str, params: dict[str, str]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        server_retries = 0
        rate_limit_retries = 0
        while True:
            try:
                response = self.http_client.get(SEARCH_PATH, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if server_retries < MAX_SERVER_ERROR_RETRIES:
                    server_retries += 1
                    self._retry_sleep(
                        "x_transport_error_retry",
                        server_retries,
                        event_id=self.config.event_id,
                        query_id=query_id,
                        error=str(exc),
                    )
                    continue
                raise XApiError(f"X API transport error on {SEARCH_PATH}: {exc}") from exc

            if response.status_code == 200:
                body = response.json()
                if not isinstance(body, dict):
                    raise XApiError(f"Unexpected non-object JSON from X API on {SEARCH_PATH}")
                return body

            server_retries, rate_limit_retries = self._handle_non_200(
                response,
                query_id=query_id,
                server_retries=server_retries,
                rate_limit_retries=rate_limit_retries,
            )

    def _record_posts(self, query_id: str, posts: list[Any]) -> list[dict[str, Any]]:
        valid = [p for p in posts if isinstance(p, dict) and p.get("id")]
        count = len(valid)
        if count == 0:
            return []

        self.state.reads_total = add_reads(
            self.config.event_id, SOURCE_NAME, count, client=self.firestore_client
        )
        self.state.reads_by_query[query_id] = self.state.reads_by_query.get(query_id, 0) + count
        self.state.reads_this_run += count
        self.state.remaining_budget -= count

        collected_at = self.now_fn().astimezone(timezone.utc)
        rows = [
            RawItem(
                event_id=self.config.event_id,
                source=SOURCE_NAME,
                source_id=str(post["id"]),
                query_id=query_id,
                run_id=self.run_id,
                captured_by=CAPTURED_BY,
                collected_at=collected_at,
                payload=copy.deepcopy(post),
            )
            for post in valid
        ]
        res = write_raw_items(rows, run_id=self.run_id, client=self.bq_client)
        self.state.rows_written += res.written
        self.state.rows_rejected += len(res.rejected)
        return valid

    def _build_params(
        self,
        item: LiveQuery,
        *,
        max_results: int,
        since_id: str | None,
        next_token: str | None,
    ) -> dict[str, str]:
        params: dict[str, str] = {
            "query": item.query,
            "tweet.fields": TWEET_FIELDS,
            "max_results": str(max_results),
        }
        if since_id:
            params["since_id"] = since_id
        else:
            params["start_time"] = self.start_iso
            params["end_time"] = self.end_iso
        if next_token:
            params["next_token"] = next_token
        return params

    def _paginate_query(self, item: LiveQuery, since_id: str | None) -> None:
        highest_id = since_id
        saved_id = since_id
        next_token: str | None = None
        while True:
            if self.state.remaining_budget < SEARCH_MIN_RESULTS:
                self.state.cap_reached = True
                return
            max_results = min(MAX_PAGE_RESULTS, self.state.remaining_budget)
            params = self._build_params(
                item, max_results=max_results, since_id=since_id, next_token=next_token
            )
            body = self._get_json(item.query_id, params)
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            valid_posts = self._record_posts(item.query_id, body.get("data") or [])
            highest_id = _max_snowflake_id(highest_id, _extract_newest_id(meta, valid_posts))
            if highest_id and highest_id != saved_id:
                set_cursor(
                    self.config.event_id,
                    SOURCE_NAME,
                    item.query_id,
                    highest_id,
                    step_done=False,
                    updated_at=self.now_fn().astimezone(timezone.utc),
                    client=self.firestore_client,
                )
                saved_id = highest_id
            next_token = str(meta["next_token"]) if meta.get("next_token") else None
            if not next_token:
                self.state.queries_completed += 1
                return
            if self.state.remaining_budget <= 0:
                self.state.cap_reached = True
                return

    def run_query(self, item: LiveQuery) -> None:
        now_utc = self.now_fn().astimezone(timezone.utc)
        cursor_obj = get_cursor(
            self.config.event_id, SOURCE_NAME, item.query_id, client=self.firestore_client
        )
        since_id = _usable_since_id(cursor_obj, now_utc, item.query_id)
        if not since_id and not self.search_window_valid:
            _log_json(
                logging.WARNING,
                "collect_x_search_window_empty",
                event_id=self.config.event_id,
                query_id=item.query_id,
                start_time=self.start_iso,
                end_time=self.end_iso,
            )
            self.state.queries_completed += 1
            return
        try:
            self._paginate_query(item, since_id)
        except _XQueryAuthError:
            self.state.queries_skipped_auth.append(item.query_id)

    def run_all(self, queries: list[LiveQuery]) -> None:
        for item in queries:
            if self.state.remaining_budget < SEARCH_MIN_RESULTS:
                self.state.cap_reached = True
                return
            self.run_query(item)
            if self.state.cap_reached:
                return


def _read_optional_reads(event_id: str, firestore_client: Any | None) -> int:
    if firestore_client is not None:
        return get_reads(event_id, SOURCE_NAME, client=firestore_client)
    return 0


def _read_dry_run_cursors(
    event_id: str, queries: list[LiveQuery], now_utc: datetime, firestore_client: Any | None
) -> dict[str, str | None]:
    if firestore_client is None:
        return {}
    return {
        item.query_id: _usable_since_id(
            get_cursor(event_id, SOURCE_NAME, item.query_id, client=firestore_client),
            now_utc,
            item.query_id,
        )
        for item in queries
    }


def _window_closed_summary(
    event_id: str, run_id: str, queries_planned: int, existing_reads: int, *, dry_run: bool
) -> CollectXSummary:
    summary = CollectXSummary(
        event_id=event_id,
        run_id=run_id,
        queries_planned=queries_planned,
        queries_completed=0,
        queries_skipped_auth=[],
        reads_by_query={},
        reads_this_run=0,
        reads_total=existing_reads,
        rows_written=0,
        rows_rejected=0,
        stop_reason="window_closed",
    )
    _emit_done_log(summary, dry_run=dry_run)
    return summary


def _run_dry_mode(
    *,
    config: EventConfig,
    run_id: str,
    max_posts: int,
    limit: int | None,
    queries: list[LiveQuery],
    start_iso: str,
    end_iso: str,
    now: datetime,
    firestore_client: Any | None,
) -> CollectXSummary:
    existing_reads = _read_optional_reads(config.event_id, firestore_client)
    budget = _compute_run_budget(max_posts, existing_reads, limit)
    cursors = _read_dry_run_cursors(config.event_id, queries, now, firestore_client)
    planned = build_live_plan(config, now, cursors=cursors)
    _log_json(
        logging.INFO,
        "collect_x_dry_run_plan",
        event_id=config.event_id,
        run_id=run_id,
        max_posts=max_posts,
        existing_reads=existing_reads,
        budget=budget,
        start_time=start_iso,
        end_time=end_iso,
        requests=[r.model_dump() for r in planned],
    )
    stop_reason: StopReason = "cap_reached" if budget < SEARCH_MIN_RESULTS else "completed"
    summary = CollectXSummary(
        event_id=config.event_id,
        run_id=run_id,
        queries_planned=len(queries),
        queries_completed=0,
        queries_skipped_auth=[],
        reads_by_query={q.query_id: 0 for q in queries},
        reads_this_run=0,
        reads_total=existing_reads,
        rows_written=0,
        rows_rejected=0,
        stop_reason=stop_reason,
    )
    _emit_done_log(summary, dry_run=True)
    return summary


def _execute_live_run(
    *,
    config: EventConfig,
    settings: CollectXSettings,
    run_id: str,
    queries: list[LiveQuery],
    start_dt: datetime,
    end_dt: datetime,
    state: _RunState,
    http_client: httpx.Client | None,
    bq_client: Any | None,
    firestore_client: Any | None,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
) -> CollectXSummary:
    owns_client = http_client is None
    client = http_client or httpx.Client(
        base_url=settings.x_api_base_url, timeout=settings.http_timeout_seconds
    )
    runner = _LiveCollectorRunner(
        config=config,
        run_id=run_id,
        start_iso=_format_utc_iso(start_dt),
        end_iso=_format_utc_iso(end_dt),
        search_window_valid=start_dt < end_dt,
        bearer_token=(settings.x_bearer_token or "").strip(),
        state=state,
        http_client=client,
        bq_client=bq_client,
        firestore_client=firestore_client,
        now_fn=clock,
        sleep_fn=sleeper,
    )
    try:
        runner.run_all(queries)
    except Exception:
        _emit_done_log(_make_summary(config.event_id, run_id, len(queries), state, "error"))
        raise
    finally:
        if owns_client:
            client.close()

    stop_reason: StopReason = "cap_reached" if state.cap_reached else "completed"
    summary = _make_summary(config.event_id, run_id, len(queries), state, stop_reason)
    _emit_done_log(summary)
    return summary


def run_collect_x(
    event_path: str | Path,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    http_client: httpx.Client | None = None,
    bq_client: Any | None = None,
    firestore_client: Any | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    settings: CollectXSettings | None = None,
    run_id: str | None = None,
) -> CollectXSummary:
    """Run one incremental capped live X collection cycle for an event."""
    effective_settings = settings or CollectXSettings()
    _configure_logger(effective_settings.log_level)

    config = load_event(event_path)
    max_posts = _validate_startup(config, effective_settings, limit=limit, dry_run=dry_run)
    clock = now_fn or (lambda: datetime.now(timezone.utc))
    sleeper = sleep_fn or time.sleep
    effective_run_id = run_id or f"collect-x-{uuid.uuid4().hex[:12]}"
    now = clock().astimezone(timezone.utc)
    queries = build_live_queries(config)

    window_open, start_dt, end_dt = compute_live_window(config, now)
    if not window_open:
        return _window_closed_summary(
            config.event_id,
            effective_run_id,
            len(queries),
            _read_optional_reads(config.event_id, firestore_client),
            dry_run=dry_run,
        )

    if dry_run:
        return _run_dry_mode(
            config=config,
            run_id=effective_run_id,
            max_posts=max_posts,
            limit=limit,
            queries=queries,
            start_iso=_format_utc_iso(start_dt),
            end_iso=_format_utc_iso(end_dt),
            now=now,
            firestore_client=firestore_client,
        )

    existing_reads = get_reads(config.event_id, SOURCE_NAME, client=firestore_client)
    state = _RunState(
        reads_by_query={q.query_id: 0 for q in queries},
        reads_total=existing_reads,
        remaining_budget=_compute_run_budget(max_posts, existing_reads, limit),
    )
    if state.remaining_budget < SEARCH_MIN_RESULTS:
        summary = _make_summary(config.event_id, effective_run_id, len(queries), state, "cap_reached")
        _emit_done_log(summary)
        return summary

    return _execute_live_run(
        config=config,
        settings=effective_settings,
        run_id=effective_run_id,
        queries=queries,
        start_dt=start_dt,
        end_dt=end_dt,
        state=state,
        http_client=http_client,
        bq_client=bq_client,
        firestore_client=firestore_client,
        clock=clock,
        sleeper=sleeper,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m services.collect_x.main",
        description="Scheduled live X API v2 collector for a configured VeriFact event.",
    )
    parser.add_argument("--event", required=True, help="Path to event YAML config file.")
    parser.add_argument("--limit", type=int, default=None, help="Optional per-run read limit.")
    parser.add_argument("--dry-run", action="store_true", help="Plan requests without HTTP/writes.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint; returns 0 on completion/cap_reached/window_closed, 1 on error."""
    args = _build_arg_parser().parse_args(argv)
    try:
        run_collect_x(event_path=args.event, limit=args.limit, dry_run=args.dry_run)
    except CollectXConfigError as exc:
        _log_json(logging.ERROR, "collect_x_refused", reason=str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        _log_json(logging.ERROR, "collect_x_failed", reason=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
