"""Append-only BigQuery writer for raw_items (Spec 03)."""

from __future__ import annotations

import json
import logging
import time
from datetime import timezone
from typing import Any

from google.cloud import bigquery
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from verifact_core.models import RawItem, Rejection, WriteResult

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.5
RAW_ITEMS_TABLE = "raw_items"


class BqSettings(BaseSettings):
    """Environment configuration for BigQuery raw_items writer."""

    model_config = SettingsConfigDict(extra="ignore")

    gcp_project: str = Field(default="", validation_alias="GCP_PROJECT")
    bq_dataset: str = Field(default="verifact", validation_alias="BQ_DATASET")


def _get_bq_client(client: Any | None, settings: BqSettings) -> Any:
    if client is not None:
        return client
    project = settings.gcp_project or None
    return bigquery.Client(project=project)


def _resolve_table_ref(
    bq_client: Any, settings: BqSettings, table: str = RAW_ITEMS_TABLE
) -> str:
    if "." in table:
        return table
    project = settings.gcp_project or getattr(bq_client, "project", "") or ""
    if isinstance(project, str) and project.strip():
        return f"{project.strip()}.{settings.bq_dataset}.{table}"
    return f"{settings.bq_dataset}.{table}"


def _row_to_bq_dict(item: RawItem, run_id: str) -> dict[str, Any]:
    collected_iso = (
        item.collected_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    return {
        "event_id": item.event_id,
        "source": item.source,
        "source_id": item.source_id,
        "query_id": item.query_id,
        "run_id": item.run_id if item.run_id is not None else run_id,
        "captured_by": item.captured_by,
        "collected_at": collected_iso,
        "payload": item.payload,
    }


def _extract_error_reason(err_entry: dict[str, Any]) -> str:
    errors = err_entry.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            reason = str(first.get("reason", "")).strip()
            message = str(first.get("message", "")).strip()
            if reason and message and reason != message:
                return f"{reason}: {message}"
            if reason:
                return reason
            if message:
                return message
        return str(first)
    if "reason" in err_entry:
        return str(err_entry["reason"])
    if "message" in err_entry:
        return str(err_entry["message"])
    return "unknown_insert_error"


def _log_rejection(item: RawItem, reason: str, run_id: str) -> None:
    payload = {
        "event": "bq_insert_error",
        "source": item.source,
        "source_id": item.source_id,
        "reason": reason,
        "run_id": run_id,
    }
    logger.error(json.dumps(payload), extra=payload)


def _log_row_rejection(table: str, source_id: str, reason: str) -> None:
    payload = {
        "event": "bq_insert_error",
        "table": table,
        "source": table,
        "source_id": source_id,
        "reason": reason,
    }
    logger.error(json.dumps(payload), extra=payload)


def _row_identifier(row: dict[str, Any], idx: int) -> str:
    for key in ("item_id", "storyline_id", "source_id"):
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val)
    return f"row:{idx}"


def _insert_json_batch(
    bq_client: Any,
    table_ref: str,
    batch_dicts: list[dict[str, Any]],
) -> tuple[int, list[tuple[int, str]]]:
    """Insert a batch of JSON dicts with retries; returns (written, [(orig_idx, reason), ...])."""
    pending: list[tuple[int, dict[str, Any]]] = list(enumerate(batch_dicts))
    last_reasons: dict[int, str] = {}
    written = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 2)))

        row_dicts = [row for _, row in pending]
        try:
            errors = bq_client.insert_rows_json(table_ref, row_dicts)
        except Exception as exc:  # noqa: BLE001
            reason_str = str(exc) or exc.__class__.__name__
            last_reasons = {pos: reason_str for pos in range(len(pending))}
            continue

        if not errors:
            written += len(pending)
            pending = []
            break

        failed_by_pos: dict[int, str] = {}
        for err_entry in errors:
            raw_idx = err_entry.get("index", 0)
            pos = int(raw_idx) if isinstance(raw_idx, int) else 0
            if pos >= len(pending) and len(pending) == 1:
                pos = 0
            if 0 <= pos < len(pending):
                failed_by_pos[pos] = _extract_error_reason(err_entry)

        if not failed_by_pos:
            reason_str = _extract_error_reason(errors[0]) if errors else "unknown_insert_error"
            failed_by_pos = {pos: reason_str for pos in range(len(pending))}

        written += max(0, len(pending) - len(failed_by_pos))

        next_pending: list[tuple[int, dict[str, Any]]] = []
        next_reasons: dict[int, str] = {}
        for old_pos in sorted(failed_by_pos):
            new_pos = len(next_pending)
            next_pending.append(pending[old_pos])
            next_reasons[new_pos] = failed_by_pos[old_pos]

        pending = next_pending
        last_reasons = next_reasons

    failed_out = [
        (orig_idx, last_reasons.get(pos, "unknown_insert_error"))
        for pos, (orig_idx, _) in enumerate(pending)
    ]
    return written, failed_out


def _insert_single_batch(
    bq_client: Any,
    table_ref: str,
    batch: list[RawItem],
    run_id: str,
) -> tuple[int, list[Rejection]]:
    row_dicts = [_row_to_bq_dict(item, run_id) for item in batch]
    written, failed = _insert_json_batch(bq_client, table_ref, row_dicts)
    rejected: list[Rejection] = []
    for orig_idx, reason in failed:
        item = batch[orig_idx]
        _log_rejection(item, reason, run_id)
        rejected.append(
            Rejection(source=item.source, source_id=item.source_id, reason=reason)
        )
    return written, rejected


def write_rows(
    table: str,
    rows: list[dict[str, Any]],
    *,
    client: Any | None = None,
) -> WriteResult:
    """Append generic dict rows to a BigQuery table in batches of at most 500."""
    if not isinstance(table, str) or not table.strip():
        raise ValueError("table must be a non-empty string")
    if not rows:
        return WriteResult(written=0, rejected=[])

    settings = BqSettings()
    bq_client = _get_bq_client(client, settings)
    table_ref = _resolve_table_ref(bq_client, settings, table.strip())

    total_written = 0
    all_rejected: list[Rejection] = []

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        batch_written, failed = _insert_json_batch(bq_client, table_ref, batch)
        total_written += batch_written
        for orig_idx, reason in failed:
            row_id = _row_identifier(batch[orig_idx], start + orig_idx)
            _log_row_rejection(table.strip(), row_id, reason)
            all_rejected.append(
                Rejection(source=table.strip(), source_id=row_id, reason=reason)
            )

    return WriteResult(written=total_written, rejected=all_rejected)


def write_raw_items(
    rows: list[RawItem],
    *,
    run_id: str,
    client: Any | None = None,
) -> WriteResult:
    """Append RawItem rows to BigQuery raw_items in batches of at most 500."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    if not rows:
        return WriteResult(written=0, rejected=[])

    settings = BqSettings()
    bq_client = _get_bq_client(client, settings)
    table_ref = _resolve_table_ref(bq_client, settings, RAW_ITEMS_TABLE)

    total_written = 0
    all_rejected: list[Rejection] = []

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        batch_written, batch_rejected = _insert_single_batch(
            bq_client, table_ref, batch, run_id
        )
        total_written += batch_written
        all_rejected.extend(batch_rejected)

    return WriteResult(written=total_written, rejected=all_rejected)
