"""Capped one-off X API v2 backfill job (Spec 02).

Collects X posts around an event's T0 under a hard read cap and writes
unmodified payloads directly to BigQuery raw_items while persisting step
cursors and atomic read totals in Firestore.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from verifact_core.bq import write_raw_items
from verifact_core.config import EventConfig, load_event
from verifact_core.cursors import add_reads, get_cursor, get_reads, set_cursor
from verifact_core.models import RawItem

STEPS: tuple[str, ...] = ("thread", "quotes", "official", "keywords")
SOURCE_NAME = "x"
CAPTURED_BY: Literal["backfill"] = "backfill"
TWEET_FIELDS = (
    "created_at,lang,author_id,conversation_id,referenced_tweets,entities,public_metrics"
)

MAX_PAGE_RESULTS = 100
SEARCH_MIN_RESULTS = 10
LOOKUP_MIN_RESULTS = 1
MAX_QUERY_LENGTH = 512

MAX_SERVER_ERROR_RETRIES = 3
MAX_RATE_LIMIT_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 15.0

RECENT_SEARCH_LOOKBACK = timedelta(days=7)
RECENT_SEARCH_START_BUFFER = timedelta(minutes=10)
RECENT_SEARCH_END_BUFFER = timedelta(seconds=30)

StopReason = Literal["completed", "cap_reached", "error"]


class BackfillConfigError(ValueError):
    """Raised when configuration or required runtime settings refuse job start."""


class XApiError(RuntimeError):
    """Raised on unrecoverable X API HTTP or transport failure."""


class _XStepAuthError(RuntimeError):
    """Internal signal to stop the current step on HTTP 401/403 and move on."""

    def __init__(self, status_code: int, endpoint: str, detail: str) -> None:
        super().__init__(f"HTTP {status_code} on {endpoint}: {detail}")
        self.status_code = status_code
        self.endpoint = endpoint
        self.detail = detail


class _DynamicStdoutHandler(logging.Handler):
    """Logging handler that writes each formatted line to current sys.stdout."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            self.handleError(record)


def _configure_logger(level: str = "INFO") -> logging.Logger:
    log = logging.getLogger("verifact.x_backfill")
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


class BackfillSettings(BaseSettings):
    """Environment settings for the x-backfill job."""

    model_config = SettingsConfigDict(extra="ignore")

    x_bearer_token: str | None = Field(default=None, validation_alias="X_BEARER_TOKEN")
    x_api_base_url: str = Field(default="https://api.x.com", validation_alias="X_API_BASE_URL")
    http_timeout_seconds: float = Field(
        default=30.0, validation_alias="X_HTTP_TIMEOUT_SECONDS", gt=0
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


class PlannedRequest(BaseModel):
    """Immutable description of one planned X API request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: str
    method: str = "GET"
    path: str
    params: dict[str, str]


class BackfillSummary(BaseModel):
    """Final run summary mirrored in the x_backfill_done log line."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    run_id: str
    reads_by_step: dict[str, int]
    reads_total: int
    rows_written: int
    rows_rejected: int
    stop_reason: StopReason


@dataclass(frozen=True)
class _StepSubRequest:
    kind: Literal["lookup", "search", "quotes"]
    path: str
    ids: tuple[str, ...] = ()
    query: str | None = None
    sort_order: str | None = None


@dataclass
class _RunState:
    reads_by_step: dict[str, int]
    reads_total: int
    remaining_budget: int
    rows_written: int = 0
    rows_rejected: int = 0
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


def compute_search_bounds(config: EventConfig, now: datetime) -> tuple[datetime, datetime]:
    """Compute X recent search [start_time, end_time] clamped to the 7-day window."""
    now_utc = now.astimezone(timezone.utc)
    t0_utc = config.t0.utc.astimezone(timezone.utc)
    start_time = max(
        t0_utc - timedelta(hours=config.window.pre_hours),
        now_utc - RECENT_SEARCH_LOOKBACK + RECENT_SEARCH_START_BUFFER,
    )
    end_time = min(
        t0_utc + timedelta(hours=config.window.post_hours),
        now_utc - RECENT_SEARCH_END_BUFFER,
    )
    return start_time, end_time


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
    """Pack OR-joined clauses into one or more X search queries within max_length."""
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
            raise BackfillConfigError(
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


def build_keyword_queries(
    config: EventConfig, max_length: int = MAX_QUERY_LENGTH
) -> list[str]:
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
            _pack_or_clauses(
                context_clauses, prefix=org_prefix, suffix=suffix, max_length=max_length
            )
        )
    return queries


def _build_step_subrequests(config: EventConfig, step: str) -> list[_StepSubRequest]:
    x_cfg = config.sources.x
    if step == "thread":
        ids = _dedupe_preserve_order(
            list(config.official.posts) + (list(x_cfg.thread_ids) if x_cfg else [])
        )
        if not ids:
            return []
        return [
            _StepSubRequest(kind="lookup", path="/2/tweets", ids=tuple(ids)),
            *[
                _StepSubRequest(
                    kind="search", path="/2/tweets/search/recent", query=f"conversation_id:{pid}"
                )
                for pid in ids
            ],
        ]
    if step == "quotes":
        return [
            _StepSubRequest(kind="quotes", path=f"/2/tweets/{pid}/quote_tweets")
            for pid in _dedupe_preserve_order(list(config.official.posts))
        ]
    if step == "official":
        handles = _dedupe_preserve_order([a.lstrip("@") for a in config.official.accounts])
        return [
            _StepSubRequest(kind="search", path="/2/tweets/search/recent", query=f"from:{handle}")
            for handle in handles
        ]
    if step == "keywords":
        return [
            _StepSubRequest(
                kind="search", path="/2/tweets/search/recent", query=q, sort_order="relevancy"
            )
            for q in build_keyword_queries(config)
        ]
    raise BackfillConfigError(f"Unknown step: {step!r}")


def build_step_plan(
    config: EventConfig, now: datetime, selected_step: str | None = None
) -> list[PlannedRequest]:
    """Return the ordered list of planned X API requests for dry-run inspection."""
    start_dt, end_dt = compute_search_bounds(config, now)
    start_iso, end_iso = _format_utc_iso(start_dt), _format_utc_iso(end_dt)

    planned: list[PlannedRequest] = []
    for step in (selected_step,) if selected_step else STEPS:
        for sub in _build_step_subrequests(config, step):
            params: dict[str, str] = {"tweet.fields": TWEET_FIELDS}
            if sub.kind == "lookup":
                params["ids"] = ",".join(sub.ids)
            elif sub.kind == "search":
                params.update({"query": sub.query or "", "start_time": start_iso, "end_time": end_iso})
                if sub.sort_order:
                    params["sort_order"] = sub.sort_order
            planned.append(PlannedRequest(step=step, path=sub.path, params=params))
    return planned


def _validate_startup(
    config: EventConfig,
    settings: BackfillSettings,
    *,
    limit: int | None,
    dry_run: bool,
    step: str | None,
) -> int:
    if step is not None and step not in STEPS:
        raise BackfillConfigError(f"Invalid --step {step!r}; must be one of {', '.join(STEPS)}")
    if limit is not None and limit <= 0:
        raise BackfillConfigError("--limit must be a positive integer")

    x_cfg = config.sources.x
    if x_cfg is None or not x_cfg.enabled:
        raise BackfillConfigError("sources.x.enabled must be true to run x-backfill")
    if x_cfg.max_posts is None or x_cfg.max_posts <= 0:
        raise BackfillConfigError("sources.x.max_posts is required and must be > 0")
    if not dry_run and (not settings.x_bearer_token or not settings.x_bearer_token.strip()):
        raise BackfillConfigError(
            "X_BEARER_TOKEN environment variable is required unless --dry-run is set"
        )
    return x_cfg.max_posts


def _parse_resume_cursor(raw_cursor: str | None) -> tuple[int, str | None, int]:
    """Decode stored step cursor into (sub_index, page_token, id_offset)."""
    if not raw_cursor:
        return 0, None, 0
    try:
        parsed = json.loads(raw_cursor)
    except json.JSONDecodeError:
        return 0, raw_cursor, 0
    if isinstance(parsed, dict) and "sub_index" in parsed:
        return int(parsed.get("sub_index", 0)), parsed.get("next_token"), int(parsed.get("id_offset", 0))
    return 0, raw_cursor, 0


def _encode_resume_cursor(
    *,
    sub_index: int,
    next_token: str | None,
    newest_id: str | None,
    total_subs: int,
    id_offset: int = 0,
) -> str | None:
    if total_subs == 1 and id_offset == 0:
        return next_token or newest_id
    return json.dumps(
        {"sub_index": sub_index, "next_token": next_token, "newest_id": newest_id, "id_offset": id_offset},
        separators=(",", ":"),
    )


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


def _emit_done_log(summary: BackfillSummary, *, dry_run: bool = False) -> None:
    fields: dict[str, Any] = summary.model_dump()
    if dry_run:
        fields["dry_run"] = True
    _log_json(logging.INFO, "x_backfill_done", **fields)


def _read_existing_reads_for_dry_run(event_id: str, firestore_client: Any | None) -> int:
    if firestore_client is not None:
        return get_reads(event_id, SOURCE_NAME, client=firestore_client)
    return 0


def _make_summary(
    event_id: str, run_id: str, state: _RunState, stop_reason: StopReason
) -> BackfillSummary:
    return BackfillSummary(
        event_id=event_id,
        run_id=run_id,
        reads_by_step=dict(state.reads_by_step),
        reads_total=state.reads_total,
        rows_written=state.rows_written,
        rows_rejected=state.rows_rejected,
        stop_reason=stop_reason,
    )


@dataclass
class _BackfillRunner:
    """Stateful executor for a single capped x-backfill run."""

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
        step: str,
        path: str,
        server_retries: int,
        rate_limit_retries: int,
    ) -> tuple[int, int]:
        status = response.status_code
        if status == 429:
            if rate_limit_retries >= MAX_RATE_LIMIT_RETRIES:
                raise XApiError(
                    f"X API rate limit persisted after {MAX_RATE_LIMIT_RETRIES} retries on {path}"
                )
            wait_s = _compute_rate_limit_wait(response, self.now_fn, rate_limit_retries)
            rate_limit_retries += 1
            _log_json(
                logging.WARNING,
                "x_rate_limited",
                step=step,
                path=path,
                wait_seconds=wait_s,
                attempt=rate_limit_retries,
            )
            self.sleep_fn(wait_s)
            return server_retries, rate_limit_retries

        if status in (401, 403):
            detail = response.text[:300]
            _log_json(
                logging.ERROR, "x_step_auth_error", step=step, path=path, status_code=status, detail=detail
            )
            raise _XStepAuthError(status_code=status, endpoint=path, detail=detail)

        if 500 <= status < 600 and server_retries < MAX_SERVER_ERROR_RETRIES:
            server_retries += 1
            self._retry_sleep(
                "x_server_error_retry", server_retries, step=step, path=path, status_code=status
            )
            return server_retries, rate_limit_retries

        raise XApiError(f"X API HTTP {status} on {path}: {response.text[:300]}")

    def _get_json(self, step: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        server_retries = 0
        rate_limit_retries = 0

        while True:
            try:
                response = self.http_client.get(path, params=params, headers=headers)
            except httpx.HTTPError as exc:
                if server_retries < MAX_SERVER_ERROR_RETRIES:
                    server_retries += 1
                    self._retry_sleep(
                        "x_transport_error_retry", server_retries, step=step, path=path, error=str(exc)
                    )
                    continue
                raise XApiError(f"X API transport error on {path}: {exc}") from exc

            if response.status_code == 200:
                body = response.json()
                if not isinstance(body, dict):
                    raise XApiError(f"Unexpected non-object JSON from X API on {path}")
                return body

            server_retries, rate_limit_retries = self._handle_non_200(
                response,
                step=step,
                path=path,
                server_retries=server_retries,
                rate_limit_retries=rate_limit_retries,
            )

    def _record_posts(self, step: str, posts: list[Any]) -> str | None:
        valid = [p for p in posts if isinstance(p, dict) and p.get("id")]
        count = len(valid)
        if count == 0:
            return None

        self.state.reads_total = add_reads(
            self.config.event_id, SOURCE_NAME, count, client=self.firestore_client
        )
        self.state.reads_by_step[step] += count
        self.state.remaining_budget -= count

        collected_at = self.now_fn().astimezone(timezone.utc)
        rows = [
            RawItem(
                event_id=self.config.event_id,
                source=SOURCE_NAME,
                source_id=str(post["id"]),
                query_id=step,
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
        return str(valid[0]["id"])

    def _save_cursor(self, step: str, value: str | None, *, step_done: bool) -> None:
        set_cursor(
            self.config.event_id,
            SOURCE_NAME,
            step,
            value,
            step_done=step_done,
            updated_at=self.now_fn().astimezone(timezone.utc),
            client=self.firestore_client,
        )

    def _run_lookup(
        self, step: str, sub: _StepSubRequest, sub_idx: int, total_subs: int, start_offset: int
    ) -> tuple[bool, str | None]:
        offset = start_offset
        last_newest: str | None = None
        while offset < len(sub.ids):
            if self.state.remaining_budget < LOOKUP_MIN_RESULTS:
                self.state.cap_reached = True
                return False, last_newest

            size = min(MAX_PAGE_RESULTS, self.state.remaining_budget, len(sub.ids) - offset)
            chunk = sub.ids[offset : offset + size]
            body = self._get_json(step, sub.path, {"ids": ",".join(chunk), "tweet.fields": TWEET_FIELDS})
            last_newest = self._record_posts(step, body.get("data") or []) or last_newest

            offset += len(chunk)
            done_sub = offset >= len(sub.ids)
            cursor_val = _encode_resume_cursor(
                sub_index=sub_idx + 1 if done_sub else sub_idx,
                next_token=None,
                newest_id=last_newest,
                total_subs=total_subs,
                id_offset=0 if done_sub else offset,
            )
            self._save_cursor(step, cursor_val, step_done=False)

            if self.state.remaining_budget <= 0:
                if not done_sub or sub_idx + 1 < total_subs:
                    self.state.cap_reached = True
                    return False, last_newest
                return True, last_newest
        return True, last_newest

    def _build_page_params(
        self, sub: _StepSubRequest, max_results: int, page_token: str | None
    ) -> dict[str, str]:
        params: dict[str, str] = {"tweet.fields": TWEET_FIELDS, "max_results": str(max_results)}
        if sub.kind == "search":
            params.update(
                {"query": sub.query or "", "start_time": self.start_iso, "end_time": self.end_iso}
            )
            if sub.sort_order:
                params["sort_order"] = sub.sort_order
            if page_token:
                params["next_token"] = page_token
        elif sub.kind == "quotes" and page_token:
            params["pagination_token"] = page_token
        return params

    def _run_paginated(
        self,
        step: str,
        sub: _StepSubRequest,
        sub_idx: int,
        total_subs: int,
        initial_token: str | None,
    ) -> tuple[bool, str | None]:
        if sub.kind == "search" and not self.search_window_valid:
            _log_json(
                logging.WARNING,
                "x_search_window_empty",
                step=step,
                start_time=self.start_iso,
                end_time=self.end_iso,
            )
            return True, None

        page_token = initial_token
        last_newest: str | None = None
        while True:
            if self.state.remaining_budget < SEARCH_MIN_RESULTS:
                self.state.cap_reached = True
                return False, last_newest

            max_results = min(MAX_PAGE_RESULTS, self.state.remaining_budget)
            body = self._get_json(step, sub.path, self._build_page_params(sub, max_results, page_token))
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            newest_from_posts = self._record_posts(step, body.get("data") or [])
            newest = meta.get("newest_id") or newest_from_posts
            last_newest = str(newest) if newest else last_newest

            page_token = str(meta["next_token"]) if meta.get("next_token") else None
            cursor_val = _encode_resume_cursor(
                sub_index=sub_idx if page_token else sub_idx + 1,
                next_token=page_token,
                newest_id=last_newest,
                total_subs=total_subs,
            )
            self._save_cursor(step, cursor_val, step_done=False)

            if not page_token:
                return True, last_newest
            if self.state.remaining_budget <= 0:
                self.state.cap_reached = True
                return False, last_newest

    def run_step(self, step: str) -> None:
        """Execute one backfill step, skipping if step_done is already true."""
        existing = get_cursor(self.config.event_id, SOURCE_NAME, step, client=self.firestore_client)
        if existing is not None and existing.step_done:
            _log_json(logging.INFO, "x_step_skipped", event_id=self.config.event_id, step=step)
            return

        subs = _build_step_subrequests(self.config, step)
        if not subs:
            self._save_cursor(step, None, step_done=True)
            return

        start_sub, resume_token, start_offset = _parse_resume_cursor(
            existing.cursor if existing is not None else None
        )
        last_newest: str | None = None
        try:
            for idx in range(start_sub, len(subs)):
                sub = subs[idx]
                if sub.kind == "lookup":
                    done, newest = self._run_lookup(
                        step, sub, idx, len(subs), start_offset if idx == start_sub else 0
                    )
                else:
                    done, newest = self._run_paginated(
                        step, sub, idx, len(subs), resume_token if idx == start_sub else None
                    )
                last_newest = newest or last_newest
                if not done:
                    return
        except _XStepAuthError:
            return

        self._save_cursor(step, last_newest, step_done=True)

    def run_all(self, steps_to_run: tuple[str, ...]) -> None:
        """Execute selected steps in strict order until done or budget exhausted."""
        for idx, step_name in enumerate(steps_to_run):
            if self.state.remaining_budget <= 0:
                cur = get_cursor(
                    self.config.event_id, SOURCE_NAME, step_name, client=self.firestore_client
                )
                if cur is None or not cur.step_done:
                    self.state.cap_reached = True
                    break
                continue
            self.run_step(step_name)
            if self.state.cap_reached:
                break
            if self.state.remaining_budget <= 0 and idx + 1 < len(steps_to_run):
                self.state.cap_reached = True
                break


def _run_dry_mode(
    *,
    config: EventConfig,
    run_id: str,
    cap: int,
    existing_reads: int,
    start_iso: str,
    end_iso: str,
    now: datetime,
    step: str | None,
) -> BackfillSummary:
    budget = cap - existing_reads
    planned = build_step_plan(config, now, selected_step=step)
    _log_json(
        logging.INFO,
        "x_backfill_dry_run_plan",
        event_id=config.event_id,
        run_id=run_id,
        cap=cap,
        existing_reads=existing_reads,
        budget=max(0, budget),
        start_time=start_iso,
        end_time=end_iso,
        requests=[r.model_dump() for r in planned],
    )
    stop_reason: StopReason = "cap_reached" if budget <= 0 else "completed"
    summary = BackfillSummary(
        event_id=config.event_id,
        run_id=run_id,
        reads_by_step={s: 0 for s in STEPS},
        reads_total=existing_reads,
        rows_written=0,
        rows_rejected=0,
        stop_reason=stop_reason,
    )
    _emit_done_log(summary, dry_run=True)
    return summary


def run_backfill(
    event_path: str | Path,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    step: str | None = None,
    http_client: httpx.Client | None = None,
    bq_client: Any | None = None,
    firestore_client: Any | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    settings: BackfillSettings | None = None,
    run_id: str | None = None,
) -> BackfillSummary:
    """Run the capped X backfill job for the given event configuration."""
    effective_settings = settings or BackfillSettings()
    _configure_logger(effective_settings.log_level)

    config = load_event(event_path)
    max_posts = _validate_startup(
        config, effective_settings, limit=limit, dry_run=dry_run, step=step
    )
    clock = now_fn or (lambda: datetime.now(timezone.utc))
    sleeper = sleep_fn or time.sleep
    effective_run_id = run_id or f"x-backfill-{uuid.uuid4().hex[:12]}"
    now = clock()
    start_dt, end_dt = compute_search_bounds(config, now)
    start_iso, end_iso = _format_utc_iso(start_dt), _format_utc_iso(end_dt)

    cap = min(max_posts, limit) if limit is not None else max_posts
    existing_reads = (
        _read_existing_reads_for_dry_run(config.event_id, firestore_client)
        if dry_run
        else get_reads(config.event_id, SOURCE_NAME, client=firestore_client)
    )
    if dry_run:
        return _run_dry_mode(
            config=config,
            run_id=effective_run_id,
            cap=cap,
            existing_reads=existing_reads,
            start_iso=start_iso,
            end_iso=end_iso,
            now=now,
            step=step,
        )

    state = _RunState(
        reads_by_step={s: 0 for s in STEPS},
        reads_total=existing_reads,
        remaining_budget=cap - existing_reads,
    )
    if state.remaining_budget <= 0:
        summary = _make_summary(config.event_id, effective_run_id, state, "cap_reached")
        _emit_done_log(summary)
        return summary

    owns_client = http_client is None
    client = http_client or httpx.Client(
        base_url=effective_settings.x_api_base_url,
        timeout=effective_settings.http_timeout_seconds,
    )
    runner = _BackfillRunner(
        config=config,
        run_id=effective_run_id,
        start_iso=start_iso,
        end_iso=end_iso,
        search_window_valid=start_dt < end_dt,
        bearer_token=(effective_settings.x_bearer_token or "").strip(),
        state=state,
        http_client=client,
        bq_client=bq_client,
        firestore_client=firestore_client,
        now_fn=clock,
        sleep_fn=sleeper,
    )
    try:
        runner.run_all((step,) if step else STEPS)
    except Exception:
        _emit_done_log(_make_summary(config.event_id, effective_run_id, state, "error"))
        raise
    finally:
        if owns_client:
            client.close()

    stop_reason: StopReason = "cap_reached" if state.cap_reached else "completed"
    summary = _make_summary(config.event_id, effective_run_id, state, stop_reason)
    _emit_done_log(summary)
    return summary


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.x_backfill.main",
        description="Capped X API v2 backfill job for a configured VeriFact event.",
    )
    parser.add_argument("--event", required=True, help="Path to event YAML config file.")
    parser.add_argument("--limit", type=int, default=None, help="Optional read cap override.")
    parser.add_argument("--dry-run", action="store_true", help="Plan requests without HTTP/writes.")
    parser.add_argument("--step", choices=STEPS, default=None, help="Optional single step to run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint; returns 0 on completion or cap_reached, non-zero on error."""
    args = _build_arg_parser().parse_args(argv)
    try:
        run_backfill(
            event_path=args.event, limit=args.limit, dry_run=args.dry_run, step=args.step
        )
    except BackfillConfigError as exc:
        _log_json(logging.ERROR, "x_backfill_refused", reason=str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        _log_json(logging.ERROR, "x_backfill_failed", reason=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
