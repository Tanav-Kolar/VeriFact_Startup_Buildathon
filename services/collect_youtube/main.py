"""YouTube Data API v3 collector Cloud Run Job (Spec 04)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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

logger = logging.getLogger("collect_youtube")

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
SOURCE_NAME = "youtube"
VIDEOS_INDEX_QUERY_ID = "videos_index"
COMMENTS_DISABLED_MARKER = "comments_disabled"
BATCH_SIZE = 50
QUOTA_SEARCH = 100
QUOTA_LIST = 1
HTTP_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3

WriterFn = Callable[..., WriteResult]


class YouTubeSettings(BaseSettings):
    """Environment configuration for the YouTube collector."""

    model_config = SettingsConfigDict(extra="ignore")

    youtube_api_key: str = Field(default="", validation_alias="YOUTUBE_API_KEY")
    youtube_searches_per_run: int = Field(
        default=4, ge=0, validation_alias="YOUTUBE_SEARCHES_PER_RUN"
    )


def _log_json(level: int, event: str, **fields: Any) -> None:
    payload = {"event": event, "service": "collect-youtube", **fields}
    logger.log(level, json.dumps(payload, default=str))


def _is_within_bounds(dt: datetime | None, bounds: TimeBounds) -> bool:
    if dt is None:
        return False
    return bounds.window_start <= dt <= bounds.window_end


def _parse_published_at(snippet: dict[str, Any]) -> datetime | None:
    raw = snippet.get("publishedAt")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return parse_iso_utc(raw)
    except ValueError:
        return None


def _is_comments_disabled_403(resp: httpx.Response) -> bool:
    if resp.status_code != 403:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return False
    for detail in err.get("errors") or []:
        if isinstance(detail, dict) and detail.get("reason") == "commentsDisabled":
            return True
    return "commentsDisabled" in str(err.get("message", ""))


def _yt_get_json(
    client: httpx.Client,
    endpoint: str,
    params: dict[str, Any],
    *,
    allow_comments_disabled: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Perform a YouTube Data API v3 GET request with bounded retries.

    Returns (payload_dict, comments_disabled_flag).
    """
    url = f"{YOUTUBE_API_BASE}/{endpoint}"
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
            if allow_comments_disabled and _is_comments_disabled_403(resp):
                return {}, True
            resp.raise_for_status()
            data = resp.json()
            return (data if isinstance(data, dict) else {}), False
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            _log_json(
                logging.WARNING,
                "youtube_http_retry",
                endpoint=endpoint,
                attempt=attempt + 1,
                error=str(exc),
            )
    raise RuntimeError(f"YouTube API request to {endpoint} failed: {last_exc}") from last_exc


def _load_stored_video_ids(event_id: str, firestore_client: Any | None) -> list[str]:
    cursor_obj = get_cursor(event_id, SOURCE_NAME, VIDEOS_INDEX_QUERY_ID, client=firestore_client)
    if cursor_obj is None or not cursor_obj.cursor:
        return []
    try:
        parsed = json.loads(cursor_obj.cursor)
        if isinstance(parsed, list):
            return [str(v) for v in parsed if v]
    except ValueError:
        return []
    return []


def _save_stored_video_ids(
    event_id: str,
    video_ids: list[str],
    *,
    updated_at: datetime,
    firestore_client: Any | None,
) -> None:
    set_cursor(
        event_id,
        SOURCE_NAME,
        VIDEOS_INDEX_QUERY_ID,
        json.dumps(sorted(set(video_ids))),
        step_done=False,
        updated_at=updated_at,
        client=firestore_client,
    )


def _fetch_video_comments(
    cfg: EventConfig,
    video_id: str,
    bounds: TimeBounds,
    *,
    api_key: str,
    run_id: str,
    collected_at: datetime,
    http_client: httpx.Client,
    firestore_client: Any | None,
    writer: WriterFn,
) -> tuple[int, int]:
    """Fetch comment threads for a stored video. Returns (written_count, quota_units)."""
    query_id = f"comments:{video_id}"
    cursor_obj = get_cursor(cfg.event_id, SOURCE_NAME, query_id, client=firestore_client)
    if cursor_obj is not None and cursor_obj.cursor == COMMENTS_DISABLED_MARKER:
        return 0, 0

    cursor_dt: datetime | None = None
    if cursor_obj is not None and cursor_obj.cursor:
        try:
            cursor_dt = parse_iso_utc(cursor_obj.cursor)
        except ValueError:
            cursor_dt = None

    if cursor_dt is not None and cursor_dt >= bounds.end:
        return 0, 0

    written = 0
    quota_used = 0
    page_token: str | None = None

    while True:
        params: dict[str, Any] = {
            "part": "snippet,replies",
            "videoId": video_id,
            "maxResults": 100,
            "order": "time",
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token

        data, disabled = _yt_get_json(
            http_client,
            "commentThreads",
            params,
            allow_comments_disabled=True,
        )
        quota_used += QUOTA_LIST

        if disabled:
            _log_json(
                logging.WARNING,
                "youtube_comments_disabled",
                event_id=cfg.event_id,
                video_id=video_id,
            )
            set_cursor(
                cfg.event_id,
                SOURCE_NAME,
                query_id,
                COMMENTS_DISABLED_MARKER,
                step_done=True,
                updated_at=collected_at,
                client=firestore_client,
            )
            return written, quota_used

        threads = data.get("items") or []
        page_rows: list[RawItem] = []
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            comments_to_process: list[dict[str, Any]] = []
            top = (thread.get("snippet") or {}).get("topLevelComment")
            if isinstance(top, dict):
                comments_to_process.append(top)
            for reply in (thread.get("replies") or {}).get("comments") or []:
                if isinstance(reply, dict):
                    comments_to_process.append(reply)

            for comment in comments_to_process:
                cid = str(comment.get("id") or "").strip()
                snippet = comment.get("snippet") or {}
                if not cid or not isinstance(snippet, dict):
                    continue
                pub_dt = _parse_published_at(snippet)
                if not _is_within_bounds(pub_dt, bounds):
                    continue
                if cursor_dt is not None and pub_dt is not None and pub_dt <= cursor_dt:
                    continue
                page_rows.append(
                    RawItem(
                        event_id=cfg.event_id,
                        source=SOURCE_NAME,
                        source_id=f"c:{cid}",
                        query_id=query_id,
                        run_id=run_id,
                        captured_by="api",
                        collected_at=collected_at,
                        payload=comment,
                    )
                )

        if page_rows:
            res = writer(page_rows, run_id=run_id)
            written += res.written

        next_token = data.get("nextPageToken")
        set_cursor(
            cfg.event_id,
            SOURCE_NAME,
            query_id,
            format_iso_utc(bounds.end) if not next_token else (cursor_obj.cursor if cursor_obj else None),
            step_done=not bool(next_token),
            updated_at=collected_at,
            client=firestore_client,
        )
        if not next_token:
            break
        page_token = str(next_token)

    return written, quota_used


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
    api_key: str | None = None,
    searches_per_run: int | None = None,
) -> dict[str, Any]:
    """Run the YouTube Data API v3 collector for one configured event."""
    if cfg.sources.youtube is None or not cfg.sources.youtube.enabled:
        _log_json(logging.INFO, "collector_disabled", event_id=cfg.event_id, source=SOURCE_NAME)
        return {"written": 0, "quota_units": 0, "skipped": True}

    if api_key is None or searches_per_run is None:
        settings = YouTubeSettings()
        effective_key = settings.youtube_api_key if api_key is None else api_key
        max_searches = (
            settings.youtube_searches_per_run if searches_per_run is None else searches_per_run
        )
    else:
        effective_key = api_key
        max_searches = searches_per_run
    max_videos = cfg.sources.youtube.max_videos or 25

    current_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    active_run_id = run_id or f"yt-{uuid.uuid4().hex[:12]}"
    queries = build_queries(cfg, SOURCE_NAME)

    if dry_run:
        bounds = compute_time_bounds(cfg, since_iso=since_iso, now=current_now)
        summary = {
            "event": "dry_run",
            "service": "collect-youtube",
            "event_id": cfg.event_id,
            "should_run": bounds.should_run,
            "start": format_iso_utc(bounds.start),
            "end": format_iso_utc(bounds.end),
            "window_start": format_iso_utc(bounds.window_start),
            "window_end": format_iso_utc(bounds.window_end),
            "max_videos": max_videos,
            "searches_per_run": max_searches,
            "queries": [{"query_id": q.query_id, "text": q.text} for q in queries],
        }
        sys.stdout.write(json.dumps(summary) + "\n")
        return {"written": 0, "quota_units": 0, "queries": len(queries), "dry_run": True}

    if not effective_key.strip():
        raise ValueError("YOUTUBE_API_KEY is required when running collect-youtube")

    global_bounds = compute_time_bounds(cfg, since_iso=since_iso, now=current_now)
    if not global_bounds.should_run:
        _log_json(logging.INFO, "collector_window_closed", event_id=cfg.event_id)
        return {"written": 0, "quota_units": 0, "skipped": True}

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    total_written = 0
    quota_units = 0
    searches_done = 0

    try:
        stored_video_ids = _load_stored_video_ids(cfg.event_id, firestore_client)
        stored_set = set(stored_video_ids)
        discovered_new_ids: list[str] = []

        # 1. Search for new videos (capped by YOUTUBE_SEARCHES_PER_RUN and max_videos)
        for query in queries:
            if searches_done >= max_searches:
                break
            if len(stored_set) + len(discovered_new_ids) >= max_videos:
                break

            cursor_obj = get_cursor(cfg.event_id, SOURCE_NAME, query.query_id, client=firestore_client)
            cursor_iso = cursor_obj.cursor if cursor_obj is not None else None
            bounds = compute_time_bounds(
                cfg, cursor_iso=cursor_iso, since_iso=since_iso, now=current_now
            )
            if not bounds.should_run or bounds.start >= bounds.end:
                continue

            search_data, _ = _yt_get_json(
                client,
                "search",
                {
                    "part": "snippet",
                    "type": "video",
                    "order": "date",
                    "q": query.text,
                    "publishedAfter": format_iso_utc(bounds.start),
                    "publishedBefore": format_iso_utc(bounds.end),
                    "maxResults": BATCH_SIZE,
                    "key": effective_key,
                },
            )
            searches_done += 1
            quota_units += QUOTA_SEARCH

            for item in search_data.get("items") or []:
                if len(stored_set) + len(discovered_new_ids) >= max_videos:
                    break
                if not isinstance(item, dict):
                    continue
                vid = (item.get("id") or {}).get("videoId")
                if not isinstance(vid, str) or not vid.strip():
                    continue
                if vid not in stored_set and vid not in discovered_new_ids:
                    discovered_new_ids.append(vid)

            set_cursor(
                cfg.event_id,
                SOURCE_NAME,
                query.query_id,
                format_iso_utc(bounds.end),
                step_done=True,
                updated_at=current_now,
                client=firestore_client,
            )

        # 2. Fetch videos.list for new video IDs (batches of 50)
        video_channel_ids: list[str] = []
        seen_channels: set[str] = set()
        added_video_ids: list[str] = []

        for idx in range(0, len(discovered_new_ids), BATCH_SIZE):
            batch_ids = discovered_new_ids[idx : idx + BATCH_SIZE]
            vid_data, _ = _yt_get_json(
                client,
                "videos",
                {
                    "part": "snippet,statistics",
                    "id": ",".join(batch_ids),
                    "key": effective_key,
                },
            )
            quota_units += QUOTA_LIST

            video_rows: list[RawItem] = []
            for video in vid_data.get("items") or []:
                if not isinstance(video, dict):
                    continue
                vid = str(video.get("id") or "").strip()
                snippet = video.get("snippet") or {}
                if not vid or not isinstance(snippet, dict):
                    continue
                pub_dt = _parse_published_at(snippet)
                if not _is_within_bounds(pub_dt, global_bounds):
                    continue
                video_rows.append(
                    RawItem(
                        event_id=cfg.event_id,
                        source=SOURCE_NAME,
                        source_id=vid,
                        query_id="video",
                        run_id=active_run_id,
                        captured_by="api",
                        collected_at=current_now,
                        payload=video,
                    )
                )
                added_video_ids.append(vid)
                ch_id = snippet.get("channelId")
                if isinstance(ch_id, str) and ch_id.strip() and ch_id not in seen_channels:
                    seen_channels.add(ch_id)
                    video_channel_ids.append(ch_id)

            if video_rows:
                res = writer(video_rows, run_id=active_run_id)
                total_written += res.written

        # 3. Fetch channels.list ONLY for video channels (never commenters)
        for idx in range(0, len(video_channel_ids), BATCH_SIZE):
            ch_batch = video_channel_ids[idx : idx + BATCH_SIZE]
            ch_data, _ = _yt_get_json(
                client,
                "channels",
                {
                    "part": "statistics",
                    "id": ",".join(ch_batch),
                    "key": effective_key,
                },
            )
            quota_units += QUOTA_LIST

            ch_rows: list[RawItem] = []
            for ch in ch_data.get("items") or []:
                if not isinstance(ch, dict):
                    continue
                cid = str(ch.get("id") or "").strip()
                if not cid:
                    continue
                ch_rows.append(
                    RawItem(
                        event_id=cfg.event_id,
                        source=SOURCE_NAME,
                        source_id=f"channel:{cid}",
                        query_id="channel",
                        run_id=active_run_id,
                        captured_by="api",
                        collected_at=current_now,
                        payload=ch,
                    )
                )
            if ch_rows:
                res = writer(ch_rows, run_id=active_run_id)
                total_written += res.written

        if added_video_ids:
            stored_video_ids = sorted(stored_set | set(added_video_ids))
            _save_stored_video_ids(
                cfg.event_id,
                stored_video_ids,
                updated_at=current_now,
                firestore_client=firestore_client,
            )

        # 4. Fetch commentThreads.list for each stored video
        for vid in stored_video_ids:
            c_written, c_quota = _fetch_video_comments(
                cfg,
                vid,
                global_bounds,
                api_key=effective_key,
                run_id=active_run_id,
                collected_at=current_now,
                http_client=client,
                firestore_client=firestore_client,
                writer=writer,
            )
            total_written += c_written
            quota_units += c_quota
    finally:
        if owns_client:
            client.close()

    _log_json(
        logging.INFO,
        "collector_complete",
        event_id=cfg.event_id,
        run_id=active_run_id,
        written=total_written,
        quota_units_used=quota_units,
        searches_used=searches_done,
        stored_videos=len(stored_video_ids),
    )
    return {
        "written": total_written,
        "quota_units": quota_units,
        "searches_used": searches_done,
        "run_id": active_run_id,
    }


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="YouTube Data API v3 collector")
    parser.add_argument("--event", required=True, help="Path to event YAML config")
    parser.add_argument("--dry-run", action="store_true", help="Print queries and bounds without API calls")
    parser.add_argument("--since", default=None, help="Override start timestamp (ISO-8601 UTC)")
    args = parser.parse_args(argv)

    cfg = load_event(args.event)
    run_collector(cfg, dry_run=args.dry_run, since_iso=args.since)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
