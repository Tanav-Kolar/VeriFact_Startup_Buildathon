"""Shared data models for raw_items, BigQuery writes, and Firestore cursors (Spec 03)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SourceType = Literal["x", "hn", "youtube", "gdelt", "firecrawl", "manual"]
CapturedByType = Literal["api", "backfill"]


def _validate_non_empty_str(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _validate_tz_aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be in UTC (offset 0)")
    return value


class RawItem(BaseModel):
    """Immutable row for the BigQuery raw_items append-only table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    source: SourceType
    source_id: str
    query_id: str | None = None
    run_id: str | None = None
    captured_by: CapturedByType | None = "api"
    collected_at: datetime
    payload: dict[str, Any]

    @field_validator("event_id")
    @classmethod
    def _check_event_id(cls, value: str) -> str:
        return _validate_non_empty_str(value, "event_id")

    @field_validator("source_id")
    @classmethod
    def _check_source_id(cls, value: str) -> str:
        return _validate_non_empty_str(value, "source_id")

    @field_validator("collected_at")
    @classmethod
    def _check_collected_at(cls, value: datetime) -> datetime:
        return _validate_tz_aware_utc(value, "collected_at")

    @field_validator("payload", mode="before")
    @classmethod
    def _check_payload_is_dict(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("payload must be a dict")
        return value


class Rejection(BaseModel):
    """Details of a raw_items row rejected by BigQuery after retries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    source_id: str
    reason: str


class WriteResult(BaseModel):
    """Summary returned by write_raw_items."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    written: int
    rejected: list[Rejection] = Field(default_factory=list)


class Cursor(BaseModel):
    """Collector cursor state stored in Firestore collector_cursors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cursor: str | None = None
    updated_at: datetime
    step_done: bool = False

    @field_validator("updated_at")
    @classmethod
    def _check_updated_at(cls, value: datetime) -> datetime:
        return _validate_tz_aware_utc(value, "updated_at")
