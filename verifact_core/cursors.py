"""Firestore cursor store and atomic read counter for collectors (Spec 03)."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from verifact_core.models import Cursor

COLLECTION_NAME = "collector_cursors"
_READS_LOCK = threading.Lock()


class CursorSettings(BaseSettings):
    """Environment configuration for Firestore cursor store."""

    model_config = SettingsConfigDict(extra="ignore")

    gcp_project: str | None = Field(default=None, validation_alias="GCP_PROJECT")
    firestore_database: str = Field(
        default="(default)", validation_alias="FIRESTORE_DATABASE"
    )


def _get_firestore_client(client: Any | None = None) -> Any:
    if client is not None:
        return client
    settings = CursorSettings()
    project = settings.gcp_project or None
    return firestore.Client(project=project, database=settings.firestore_database)


def _validate_key_part(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _cursor_doc_id(event_id: str, source: str, query_id: str) -> str:
    _validate_key_part(event_id, "event_id")
    _validate_key_part(source, "source")
    _validate_key_part(query_id, "query_id")
    return f"{event_id}__{source}__{query_id}"


def _reads_doc_id(event_id: str, source: str) -> str:
    _validate_key_part(event_id, "event_id")
    _validate_key_part(source, "source")
    return f"{event_id}__{source}__reads"


def get_cursor(
    event_id: str,
    source: str,
    query_id: str,
    *,
    client: Any | None = None,
) -> Cursor | None:
    """Return the stored Cursor for (event_id, source, query_id), or None if absent."""
    doc_id = _cursor_doc_id(event_id, source, query_id)
    fs_client = _get_firestore_client(client)
    snapshot = fs_client.collection(COLLECTION_NAME).document(doc_id).get()
    if not snapshot.exists:
        return None
    data = snapshot.to_dict() or {}
    updated_at = data.get("updated_at")
    if isinstance(updated_at, datetime) and updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return Cursor(
        cursor=data.get("cursor"),
        updated_at=updated_at,
        step_done=bool(data.get("step_done", False)),
    )


def set_cursor(
    event_id: str,
    source: str,
    query_id: str,
    cursor: str | Cursor | None = None,
    *,
    step_done: bool = False,
    updated_at: datetime | None = None,
    client: Any | None = None,
) -> Cursor:
    """Create or update the cursor document at collector_cursors/{event_id}__{source}__{query_id}."""
    doc_id = _cursor_doc_id(event_id, source, query_id)
    if isinstance(cursor, Cursor):
        cursor_obj = cursor
    else:
        ts = updated_at if updated_at is not None else datetime.now(timezone.utc)
        cursor_obj = Cursor(cursor=cursor, updated_at=ts, step_done=step_done)

    fs_client = _get_firestore_client(client)
    doc_ref = fs_client.collection(COLLECTION_NAME).document(doc_id)
    doc_ref.set(
        {
            "cursor": cursor_obj.cursor,
            "updated_at": cursor_obj.updated_at,
            "step_done": cursor_obj.step_done,
        },
        merge=True,
    )
    return cursor_obj


def add_reads(
    event_id: str,
    source: str,
    n: int,
    *,
    client: Any | None = None,
) -> int:
    """Atomically increment reads_total for (event_id, source) and return the new total."""
    doc_id = _reads_doc_id(event_id, source)
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError("n must be a non-negative integer; reads_total is never reset")

    fs_client = _get_firestore_client(client)
    doc_ref = fs_client.collection(COLLECTION_NAME).document(doc_id)
    with _READS_LOCK:
        doc_ref.set(
            {
                "reads_total": firestore.Increment(n),
                "updated_at": datetime.now(timezone.utc),
            },
            merge=True,
        )
        snapshot = doc_ref.get()
        if not snapshot.exists:
            return 0
        data = snapshot.to_dict() or {}
        return int(data.get("reads_total", 0))


def get_reads(
    event_id: str,
    source: str,
    *,
    client: Any | None = None,
) -> int:
    """Return the current reads_total for (event_id, source), or 0 if not yet recorded."""
    doc_id = _reads_doc_id(event_id, source)
    fs_client = _get_firestore_client(client)
    snapshot = fs_client.collection(COLLECTION_NAME).document(doc_id).get()
    if not snapshot.exists:
        return 0
    data = snapshot.to_dict() or {}
    return int(data.get("reads_total", 0))
