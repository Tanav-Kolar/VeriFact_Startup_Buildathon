"""Unit tests for verifact_core.models and verifact_core.bq (Spec 03)."""

from __future__ import annotations

import copy
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from verifact_core.bq import write_raw_items  # noqa: E402
from verifact_core.models import RawItem, Rejection, WriteResult  # noqa: E402


def _make_item(
    source_id: str = "1001",
    *,
    event_id: str = "test-event",
    source: str = "x",
    query_id: str | None = "thread",
    captured_by: str = "api",
    collected_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> RawItem:
    return RawItem(
        event_id=event_id,
        source=source,  # type: ignore[arg-type]
        source_id=source_id,
        query_id=query_id,
        captured_by=captured_by,  # type: ignore[arg-type]
        collected_at=collected_at or datetime(2026, 11, 16, 14, 0, 0, tzinfo=timezone.utc),
        payload=payload if payload is not None else {"id": source_id, "text": "sample post"},
    )


def test_raw_item_valid_sources_and_immutability() -> None:
    for src in ("x", "hn", "youtube", "gdelt", "firecrawl", "manual"):
        item = _make_item(source=src)
        assert item.source == src

    item = _make_item()
    with pytest.raises(ValidationError):
        item.source_id = "mutated"  # type: ignore[misc]


def test_raw_item_rejects_naive_datetime_and_non_utc() -> None:
    # Naive datetime must be rejected (AC 3)
    with pytest.raises(ValidationError, match="UTC"):
        _make_item(collected_at=datetime(2026, 11, 16, 14, 0, 0))

    # Non-UTC offset must be rejected
    ist = timezone(timedelta(hours=5, minutes=30))
    with pytest.raises(ValidationError, match="UTC"):
        _make_item(collected_at=datetime(2026, 11, 16, 14, 0, 0, tzinfo=ist))


def test_raw_item_rejects_unknown_source_and_empty_ids() -> None:
    # Unknown source must be rejected (AC 3)
    with pytest.raises(ValidationError):
        _make_item(source="reddit")

    # Empty source_id must be rejected (AC 3)
    with pytest.raises(ValidationError):
        _make_item(source_id="")

    with pytest.raises(ValidationError):
        _make_item(source_id="   ")

    # Empty event_id must be rejected
    with pytest.raises(ValidationError):
        _make_item(event_id="")

    # Non-dict payload must be rejected
    with pytest.raises(ValidationError):
        RawItem(
            event_id="test-event",
            source="x",
            source_id="1001",
            collected_at=datetime(2026, 11, 16, 14, 0, 0, tzinfo=timezone.utc),
            payload=["not", "a", "dict"],  # type: ignore[arg-type]
        )


def test_write_1200_valid_rows_makes_3_insert_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 1: Writing 1,200 valid rows makes 3 insert calls, and written == 1200."""
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    monkeypatch.setenv("BQ_DATASET", "verifact")

    rows = [_make_item(source_id=str(i)) for i in range(1200)]
    mock_client = MagicMock()
    mock_client.insert_rows_json.return_value = []

    result = write_raw_items(rows, run_id="run-1200", client=mock_client)

    assert result == WriteResult(written=1200, rejected=[])
    assert mock_client.insert_rows_json.call_count == 3

    batch_lengths = [
        len(call.args[1]) for call in mock_client.insert_rows_json.call_args_list
    ]
    assert batch_lengths == [500, 500, 200]


def test_rejected_row_is_retried_logged_and_returned_while_others_written(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 2: A row the backend rejects appears in rejected and in a bq_insert_error log line with its reason, and the other rows are still written."""
    sleeps: list[float] = []
    monkeypatch.setattr("verifact_core.bq.time.sleep", lambda s: sleeps.append(s))

    rows = [
        _make_item(source_id="post-1"),
        _make_item(source_id="post-bad"),
        _make_item(source_id="post-3"),
    ]

    mock_client = MagicMock()
    # Attempt 1 has 3 rows (index 1 fails); attempts 2 and 3 retry only the 1 failed row (index 0 fails)
    mock_client.insert_rows_json.side_effect = [
        [{"index": 1, "errors": [{"reason": "invalid", "message": "schema mismatch"}]}],
        [{"index": 0, "errors": [{"reason": "invalid", "message": "schema mismatch"}]}],
        [{"index": 0, "errors": [{"reason": "invalid", "message": "schema mismatch"}]}],
    ]

    with caplog.at_level(logging.ERROR):
        result = write_raw_items(rows, run_id="run-reject", client=mock_client)

    assert result.written == 2
    assert len(result.rejected) == 1
    assert result.rejected[0] == Rejection(
        source="x",
        source_id="post-bad",
        reason="invalid: schema mismatch",
    )
    assert mock_client.insert_rows_json.call_count == 3
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0] > 0

    # Check structured log line
    error_logs = [
        json.loads(rec.getMessage())
        for rec in caplog.records
        if "bq_insert_error" in rec.getMessage()
    ]
    assert len(error_logs) == 1
    assert error_logs[0]["event"] == "bq_insert_error"
    assert error_logs[0]["source"] == "x"
    assert error_logs[0]["source_id"] == "post-bad"
    assert error_logs[0]["reason"] == "invalid: schema mismatch"


def test_transient_row_error_recovers_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("verifact_core.bq.time.sleep", lambda _: None)

    rows = [_make_item(source_id="post-1"), _make_item(source_id="post-2")]
    mock_client = MagicMock()
    mock_client.insert_rows_json.side_effect = [
        [{"index": 1, "errors": [{"reason": "backendError", "message": "temporary"}]}],
        [],
    ]

    result = write_raw_items(rows, run_id="run-retry-ok", client=mock_client)

    assert result.written == 2
    assert result.rejected == []
    assert mock_client.insert_rows_json.call_count == 2


def test_exception_on_insert_retries_and_rejects_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("verifact_core.bq.time.sleep", lambda _: None)

    rows = [_make_item(source_id="post-exc")]
    mock_client = MagicMock()
    mock_client.insert_rows_json.side_effect = RuntimeError("service unavailable")

    with caplog.at_level(logging.ERROR):
        result = write_raw_items(rows, run_id="run-exc", client=mock_client)

    assert result.written == 0
    assert result.rejected == [
        Rejection(source="x", source_id="post-exc", reason="service unavailable")
    ]
    assert mock_client.insert_rows_json.call_count == 3


def test_payload_stored_equals_payload_passed_in_byte_for_byte() -> None:
    """AC 4: The payload stored equals the payload passed in, byte for byte after JSON round-trip."""
    original_payload: dict[str, Any] = {
        "id": "9988776655",
        "text": "Hinglish + Unicode: क्या हाल है 🚀 — exact substring check",
        "public_metrics": {
            "like_count": 42,
            "retweet_count": 7,
            "quote_count": 3,
            "reply_count": 5,
            "impression_count": 1200,
        },
        "referenced_tweets": [{"type": "quoted", "id": "112233"}],
        "entities": {"urls": [{"expanded_url": "https://example.com/path?a=1&b=2"}]},
        "flags": {"verified": False, "nullable": None},
    }
    frozen_copy = copy.deepcopy(original_payload)
    expected_bytes = json.dumps(frozen_copy, ensure_ascii=False, sort_keys=True).encode("utf-8")

    item = _make_item(source_id="9988776655", payload=original_payload)
    captured_rows: list[dict[str, Any]] = []

    mock_client = MagicMock()

    def _capture_insert(table: str, json_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        captured_rows.extend(copy.deepcopy(json_rows))
        return []

    mock_client.insert_rows_json.side_effect = _capture_insert

    result = write_raw_items([item], run_id="run-roundtrip", client=mock_client)
    assert result.written == 1
    assert len(captured_rows) == 1

    stored_payload = captured_rows[0]["payload"]
    # If stored as dict or JSON string, verify exact JSON round-trip equivalence byte-for-byte
    if isinstance(stored_payload, str):
        round_tripped = json.loads(stored_payload)
    else:
        round_tripped = json.loads(json.dumps(stored_payload, ensure_ascii=False))

    assert round_tripped == frozen_copy
    actual_bytes = json.dumps(round_tripped, ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert actual_bytes == expected_bytes


def test_write_empty_rows_makes_no_calls() -> None:
    mock_client = MagicMock()
    result = write_raw_items([], run_id="run-empty", client=mock_client)
    assert result == WriteResult(written=0, rejected=[])
    mock_client.insert_rows_json.assert_not_called()


def test_write_raw_items_validates_run_id_and_default_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="run_id"):
        write_raw_items([], run_id="   ")

    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.setenv("BQ_DATASET", "custom_ds")

    mock_bq_cls = MagicMock()
    mock_instance = MagicMock()
    mock_instance.project = ""
    mock_instance.insert_rows_json.return_value = []
    mock_bq_cls.return_value = mock_instance
    monkeypatch.setattr("verifact_core.bq.bigquery.Client", mock_bq_cls)

    result = write_raw_items([_make_item()], run_id="run-default-client")
    assert result.written == 1
    mock_bq_cls.assert_called_once_with(project=None)
    mock_instance.insert_rows_json.assert_called_once()
    assert mock_instance.insert_rows_json.call_args.args[0] == "custom_ds.raw_items"

