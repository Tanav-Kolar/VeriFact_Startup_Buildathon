"""Fetch-pages service: article body markdown and dated official snapshots (Spec 05)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from google.cloud import bigquery
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from verifact_core.bq import write_raw_items
from verifact_core.config import EventConfig, load_event
from verifact_core.cursors import add_reads, get_cursor, get_reads, set_cursor
from verifact_core.models import RawItem
from verifact_core.url_safety import is_social_url, url_hash

logger = logging.getLogger("fetch_pages")
logging.basicConfig(level=logging.INFO, format="%(message)s")

MAX_FAILURES = 3
DEFAULT_BASE_URL = "https://api.firecrawl.dev/v2"


class FetchPagesSettings(BaseSettings):
    """Runtime configuration for fetch-pages job."""

    model_config = SettingsConfigDict(extra="ignore")

    firecrawl_api_key: str | None = Field(default=None, validation_alias="FIRECRAWL_API_KEY")
    firecrawl_max_total: int | None = Field(default=None, validation_alias="FIRECRAWL_MAX_TOTAL")
    firecrawl_max_per_run: int = Field(default=50, validation_alias="FIRECRAWL_MAX_PER_RUN")
    gcp_project: str | None = Field(default=None, validation_alias="GCP_PROJECT")
    bq_dataset: str = Field(default="verifact", validation_alias="BQ_DATASET")
    firestore_database: str = Field(default="(default)", validation_alias="FIRESTORE_DATABASE")
    firecrawl_base_url: str = Field(default=DEFAULT_BASE_URL, validation_alias="FIRECRAWL_BASE_URL")


def _log_event(event_name: str, **kwargs: Any) -> None:
    data = {"event": event_name, **kwargs}
    logger.info(json.dumps(data))


def format_request_body(url: str) -> dict[str, Any]:
    """Format exact Firecrawl scrape request body (AC 7)."""
    return {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
    }


def scrape_firecrawl(
    url: str,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    http_client: Any = None,
) -> tuple[int, dict[str, Any]]:
    """Scrape a single URL via Firecrawl v2 API."""
    if not api_key:
        raise ValueError("FIRECRAWL_API_KEY is required to call Firecrawl API")

    endpoint = f"{base_url.rstrip('/')}/scrape"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = format_request_body(url)

    if http_client is not None:
        resp = http_client.post(endpoint, json=body, headers=headers, timeout=30.0)
    else:
        resp = requests.post(endpoint, json=body, headers=headers, timeout=30.0)

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {"error": resp.text, "status_code": resp.status_code}
    return resp.status_code, data


def _get_article_cursor_state(event_id: str, fs_client: Any = None) -> dict[str, int]:
    cursor_obj = get_cursor(event_id, "firecrawl", "article", client=fs_client)
    if not cursor_obj or not cursor_obj.cursor:
        return {}
    try:
        data = json.loads(cursor_obj.cursor)
        return data.get("failures", {})
    except (json.JSONDecodeError, TypeError):
        return {}


def _save_article_cursor_state(
    event_id: str, failures: dict[str, int], fs_client: Any = None
) -> None:
    cursor_str = json.dumps({"failures": failures})
    set_cursor(event_id, "firecrawl", "article", cursor=cursor_str, client=fs_client)


def _get_official_cursor_state(
    event_id: str, fs_client: Any = None
) -> tuple[dict[str, str], dict[str, int]]:
    cursor_obj = get_cursor(event_id, "firecrawl", "official", client=fs_client)
    if not cursor_obj or not cursor_obj.cursor:
        return {}, {}
    try:
        data = json.loads(cursor_obj.cursor)
        return data.get("snapshots", {}), data.get("failures", {})
    except (json.JSONDecodeError, TypeError):
        return {}, {}


def _save_official_cursor_state(
    event_id: str,
    snapshots: dict[str, str],
    failures: dict[str, int],
    fs_client: Any = None,
) -> None:
    cursor_str = json.dumps({"snapshots": snapshots, "failures": failures})
    set_cursor(event_id, "firecrawl", "official", cursor=cursor_str, client=fs_client)


def get_pending_gdelt_articles(
    event_id: str,
    settings: FetchPagesSettings,
    limit: int = 50,
    bq_client: Any = None,
) -> list[dict[str, Any]]:
    """Query GDELT articles from BigQuery that have not yet been scraped by Firecrawl."""
    if bq_client is None:
        project = settings.gcp_project or None
        bq_client = bigquery.Client(project=project)

    project_prefix = f"{settings.gcp_project}." if settings.gcp_project else ""
    table_ref = f"{project_prefix}{settings.bq_dataset}.raw_items"

    query = f"""
    SELECT
      g.source_id,
      JSON_VALUE(g.payload, '$.url') AS url,
      COALESCE(
        TIMESTAMP(JSON_VALUE(g.payload, '$.seendate')),
        g.collected_at
      ) AS created_at
    FROM `{table_ref}` g
    WHERE g.event_id = @event_id
      AND g.source = 'gdelt'
      AND g.source_id NOT IN (
        SELECT f.source_id
        FROM `{table_ref}` f
        WHERE f.event_id = @event_id
          AND f.source = 'firecrawl'
          AND f.query_id = 'article'
      )
    ORDER BY created_at ASC
    LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("event_id", "STRING", event_id),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    rows = bq_client.query(query, job_config=job_config).result()
    candidates: list[dict[str, Any]] = []
    for r in rows:
        url_val = r.get("url") if isinstance(r, dict) else getattr(r, "url", None)
        src_id = r.get("source_id") if isinstance(r, dict) else getattr(r, "source_id", None)
        if url_val and src_id:
            candidates.append({"source_id": src_id, "url": url_val})
    return candidates


def _process_scrape_result(
    event_id: str,
    query_id: str,
    source_id: str,
    status_code: int,
    resp_payload: dict[str, Any],
    now: datetime,
    run_id: str,
    bq_client: Any = None,
) -> bool:
    is_success = status_code == 200 and resp_payload.get("success", True) is not False
    if is_success:
        raw_item = RawItem(
            event_id=event_id,
            source="firecrawl",
            source_id=source_id,
            query_id=query_id,
            captured_by="api",
            collected_at=now,
            payload=resp_payload,
        )
        write_raw_items([raw_item], run_id=run_id, client=bq_client)
        return True
    return False


def _run_official_snapshots(
    config: EventConfig,
    settings: FetchPagesSettings,
    budget_left: int,
    dry_run: bool,
    current_time: datetime,
    fs_client: Any,
    bq_client: Any,
    run_id: str,
) -> tuple[int, int, int, int]:
    """Execute official snapshots for config.official.urls."""
    event_id = config.event_id
    today_str = current_time.strftime("%Y-%m-%d")
    if dry_run and fs_client is None:
        snapshots, failures = {}, {}
    else:
        snapshots, failures = _get_official_cursor_state(event_id, fs_client)

    calls_made = 0
    written = 0
    skipped_social = 0
    skipped_failures = 0

    for url in config.official.urls:
        if calls_made >= budget_left:
            _log_event("budget_exhausted", calls_made=calls_made)
            break

        if is_social_url(url):
            _log_event("skipped_social", url=url, reason="blocklisted_social_host")
            skipped_social += 1
            continue

        u_hash = url_hash(url)
        source_id = f"{u_hash}:{today_str}"

        if snapshots.get(u_hash) == today_str:
            _log_event("already_snapshotted_today", url=url, date=today_str)
            continue

        if failures.get(u_hash, 0) >= MAX_FAILURES:
            _log_event("skipped_max_failures", url=url, failure_count=failures[u_hash])
            skipped_failures += 1
            continue

        if dry_run:
            _log_event("dry_run_official", url=url, source_id=source_id)
            continue

        status_code, resp = scrape_firecrawl(
            url,
            settings.firecrawl_api_key or "",
            base_url=settings.firecrawl_base_url,
        )
        calls_made += 1
        add_reads(event_id, "firecrawl", 1, client=fs_client)

        success = _process_scrape_result(
            event_id=event_id,
            query_id="official",
            source_id=source_id,
            status_code=status_code,
            resp_payload=resp,
            now=current_time,
            run_id=run_id,
            bq_client=bq_client,
        )
        if success:
            written += 1
            snapshots[u_hash] = today_str
            failures.pop(u_hash, None)
            _save_official_cursor_state(event_id, snapshots, failures, fs_client)
        else:
            _log_event("fetch_failed", url=url, status_code=status_code, query_id="official")
            failures[u_hash] = failures.get(u_hash, 0) + 1
            _save_official_cursor_state(event_id, snapshots, failures, fs_client)

    return calls_made, written, skipped_social, skipped_failures


def _run_article_fetches(
    config: EventConfig,
    settings: FetchPagesSettings,
    budget_left: int,
    dry_run: bool,
    candidates: list[dict[str, Any]] | None,
    current_time: datetime,
    fs_client: Any,
    bq_client: Any,
    run_id: str,
) -> tuple[int, int, int, int]:
    """Execute news article body scrapes."""
    event_id = config.event_id
    if dry_run and fs_client is None:
        failures = {}
    else:
        failures = _get_article_cursor_state(event_id, fs_client)

    if candidates is None:
        if dry_run and bq_client is None:
            candidates = []
        else:
            candidates = get_pending_gdelt_articles(
                event_id=event_id,
                settings=settings,
                limit=budget_left,
                bq_client=bq_client,
            )

    calls_made = 0
    written = 0
    skipped_social = 0
    skipped_failures = 0

    for cand in candidates:
        if calls_made >= budget_left:
            _log_event("budget_exhausted", calls_made=calls_made)
            break

        url = cand["url"]
        source_id = cand["source_id"]

        if is_social_url(url):
            _log_event("skipped_social", url=url, source_id=source_id)
            skipped_social += 1
            continue

        if failures.get(source_id, 0) >= MAX_FAILURES:
            _log_event(
                "skipped_max_failures",
                source_id=source_id,
                url=url,
                failure_count=failures[source_id],
            )
            skipped_failures += 1
            continue

        if dry_run:
            _log_event("dry_run_article", url=url, source_id=source_id)
            continue

        status_code, resp = scrape_firecrawl(
            url,
            settings.firecrawl_api_key or "",
            base_url=settings.firecrawl_base_url,
        )
        calls_made += 1
        add_reads(event_id, "firecrawl", 1, client=fs_client)

        success = _process_scrape_result(
            event_id=event_id,
            query_id="article",
            source_id=source_id,
            status_code=status_code,
            resp_payload=resp,
            now=current_time,
            run_id=run_id,
            bq_client=bq_client,
        )
        if success:
            written += 1
            failures.pop(source_id, None)
            _save_article_cursor_state(event_id, failures, fs_client)
        else:
            _log_event("fetch_failed", url=url, status_code=status_code, query_id="article")
            failures[source_id] = failures.get(source_id, 0) + 1
            _save_article_cursor_state(event_id, failures, fs_client)

    return calls_made, written, skipped_social, skipped_failures


def fetch_pages_job(
    event_path: str | Path,
    mode: str = "both",
    dry_run: bool = False,
    article_candidates: list[dict[str, Any]] | None = None,
    fs_client: Any = None,
    bq_client: Any = None,
    current_time: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    """Main execution entrypoint for fetch-pages job."""
    settings = FetchPagesSettings()
    if settings.firecrawl_max_total is None or settings.firecrawl_max_total <= 0:
        raise ValueError("FIRECRAWL_MAX_TOTAL is required to enforce project credit budget")

    if not dry_run and not settings.firecrawl_api_key:
        raise ValueError("FIRECRAWL_API_KEY is required for live scrapes")

    config = load_event(event_path)
    event_id = config.event_id
    now = current_time or datetime.now(timezone.utc)
    run_id = f"fetch_pages_{int(now.timestamp())}"

    # Budget calculation (AC 1, AC 2)
    if dry_run and fs_client is None:
        used_reads = 0
    else:
        used_reads = get_reads(event_id, "firecrawl", client=fs_client)
    remaining_total = max(0, settings.firecrawl_max_total - used_reads)
    budget = min(settings.firecrawl_max_per_run, remaining_total)

    if budget <= 0:
        _log_event(
            "cap_reached",
            used_reads=used_reads,
            firecrawl_max_total=settings.firecrawl_max_total,
        )
        return 0, {
            "cap_reached": True,
            "calls_made": 0,
            "written": 0,
            "used_reads": used_reads,
        }

    total_calls = 0
    total_written = 0
    total_skipped_social = 0
    total_skipped_failures = 0

    if mode in ("official", "both"):
        c, w, ss, sf = _run_official_snapshots(
            config=config,
            settings=settings,
            budget_left=budget - total_calls,
            dry_run=dry_run,
            current_time=now,
            fs_client=fs_client,
            bq_client=bq_client,
            run_id=run_id,
        )
        total_calls += c
        total_written += w
        total_skipped_social += ss
        total_skipped_failures += sf

    if mode in ("articles", "both") and total_calls < budget:
        c, w, ss, sf = _run_article_fetches(
            config=config,
            settings=settings,
            budget_left=budget - total_calls,
            dry_run=dry_run,
            candidates=article_candidates,
            current_time=now,
            fs_client=fs_client,
            bq_client=bq_client,
            run_id=run_id,
        )
        total_calls += c
        total_written += w
        total_skipped_social += ss
        total_skipped_failures += sf

    summary = {
        "event_id": event_id,
        "mode": mode,
        "dry_run": dry_run,
        "budget": budget,
        "calls_made": total_calls,
        "written": total_written,
        "skipped_social": total_skipped_social,
        "skipped_max_failures": total_skipped_failures,
    }
    _log_event("fetch_pages_complete", **summary)
    return 0, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch pages and official snapshots via Firecrawl")
    parser.add_argument("--event", required=True, help="Path to event YAML file")
    parser.add_argument(
        "--mode",
        choices=["articles", "official", "both"],
        default="both",
        help="Fetch mode: articles, official, or both",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview fetch operations without network calls or writes",
    )

    args = parser.parse_args()
    try:
        status_code, _ = fetch_pages_job(
            event_path=args.event,
            mode=args.mode,
            dry_run=args.dry_run,
        )
        sys.exit(status_code)
    except Exception as exc:
        _log_event("job_error", error=str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
