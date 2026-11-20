"""Unit tests for verifact_core.cursors (Spec 03)."""

from __future__ import annotations

import copy
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from verifact_core.cursors import add_reads, get_cursor, get_reads, set_cursor  # noqa: E402
from verifact_core.models import Cursor  # noqa: E402


class FakeDocumentSnapshot:
    """In-memory Firestore document snapshot."""

    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = copy.deepcopy(data) if data is not None else None

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data)


class FakeDocumentReference:
    """Thread-safe in-memory Firestore document reference supporting Increment."""

    def __init__(self, store: dict[str, dict[str, Any]], doc_id: str, lock: threading.Lock) -> None:
        self._store = store
        self._doc_id = doc_id
        self._lock = lock

    def get(self, transaction: Any | None = None) -> FakeDocumentSnapshot:
        with self._lock:
            return FakeDocumentSnapshot(self._store.get(self._doc_id))

    def set(self, document_data: dict[str, Any], merge: bool = False) -> None:
        with self._lock:
            current = copy.deepcopy(self._store.get(self._doc_id, {})) if merge else {}
            for key, value in document_data.items():
                if hasattr(value, "value") and type(value).__name__ == "Increment":
                    prev_val = int(current.get(key, 0))
                    current[key] = prev_val + int(value.value)
                else:
                    current[key] = copy.deepcopy(value)
            self._store[self._doc_id] = current


class FakeCollectionReference:
    """Thread-safe in-memory Firestore collection reference."""

    def __init__(self, store: dict[str, dict[str, Any]], lock: threading.Lock) -> None:
        self._store = store
        self._lock = lock

    def document(self, document_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(self._store, document_id, self._lock)


class FakeFirestoreClient:
    """Thread-safe in-memory Firestore client for zero-GCP unit tests."""

    def __init__(self) -> None:
        self._collections: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def collection(self, name: str) -> FakeCollectionReference:
        with self._lock:
            col = self._collections.setdefault(name, {})
        return FakeCollectionReference(col, self._lock)

    def raw_doc(self, collection_name: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._collections.get(collection_name, {}).get(doc_id))


def test_get_cursor_returns_none_for_unknown_key() -> None:
    """AC 6 (part 1): get_cursor returns None for an unknown key."""
    client = FakeFirestoreClient()
    assert get_cursor("test-event", "x", "thread", client=client) is None


def test_set_cursor_and_get_cursor_round_trip() -> None:
    """AC 6 (part 2): set_cursor then get_cursor round-trips."""
    client = FakeFirestoreClient()
    ts = datetime(2026, 11, 16, 15, 30, 0, tzinfo=timezone.utc)

    saved = set_cursor(
        "test-event",
        "x",
        "thread",
        "next_token_123",
        step_done=True,
        updated_at=ts,
        client=client,
    )
    assert saved == Cursor(cursor="next_token_123", updated_at=ts, step_done=True)

    loaded = get_cursor("test-event", "x", "thread", client=client)
    assert loaded == saved

    # Verify exact Firestore collection and document path (Step 4)
    raw = client.raw_doc("collector_cursors", "test-event__x__thread")
    assert raw is not None
    assert raw["cursor"] == "next_token_123"
    assert raw["updated_at"] == ts
    assert raw["step_done"] is True


def test_set_cursor_accepts_cursor_model_instance() -> None:
    client = FakeFirestoreClient()
    ts = datetime(2026, 11, 16, 16, 0, 0, tzinfo=timezone.utc)
    cursor_obj = Cursor(cursor="page_2", updated_at=ts, step_done=False)

    saved = set_cursor("test-event", "hn", "q-1", cursor_obj, client=client)
    assert saved == cursor_obj
    assert get_cursor("test-event", "hn", "q-1", client=client) == cursor_obj


def test_concurrent_add_reads_starting_from_100_ends_at_200() -> None:
    """AC 5: Two concurrent add_reads(..., 50) calls starting from 100 end at 200."""
    client = FakeFirestoreClient()

    initial = add_reads("test-event", "x", 100, client=client)
    assert initial == 100
    assert get_reads("test-event", "x", client=client) == 100

    barrier = threading.Barrier(2)

    def _worker() -> int:
        barrier.wait(timeout=5.0)
        return add_reads("test-event", "x", 50, client=client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_worker), pool.submit(_worker)]
        results = sorted(f.result() for f in futures)

    assert get_reads("test-event", "x", client=client) == 200
    assert results[-1] == 200

    # Verify exact Firestore document path (Step 5)
    raw = client.raw_doc("collector_cursors", "test-event__x__reads")
    assert raw is not None
    assert raw["reads_total"] == 200


def test_get_reads_defaults_to_zero_and_never_resets() -> None:
    client = FakeFirestoreClient()
    assert get_reads("test-event", "x", client=client) == 0

    assert add_reads("test-event", "x", 25, client=client) == 25
    assert add_reads("test-event", "x", 0, client=client) == 25

    # Negative values that would decrease or reset reads_total must be rejected
    with pytest.raises(ValueError, match="non-negative"):
        add_reads("test-event", "x", -5, client=client)

    assert get_reads("test-event", "x", client=client) == 25


def test_firestore_client_reads_project_and_database_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 6: Project and database names come from env (GCP_PROJECT, FIRESTORE_DATABASE default '(default)')."""
    monkeypatch.setenv("GCP_PROJECT", "env-gcp-project")
    monkeypatch.delenv("FIRESTORE_DATABASE", raising=False)

    mock_cls = MagicMock()
    fake_instance = FakeFirestoreClient()
    mock_cls.return_value = fake_instance
    monkeypatch.setattr("verifact_core.cursors.firestore.Client", mock_cls)

    assert get_reads("test-event", "x") == 0
    mock_cls.assert_called_once_with(project="env-gcp-project", database="(default)")

    mock_cls.reset_mock()
    monkeypatch.setenv("FIRESTORE_DATABASE", "custom-db")
    assert get_reads("test-event", "x") == 0
    mock_cls.assert_called_once_with(project="env-gcp-project", database="custom-db")


def test_cursor_key_validation() -> None:
    client = FakeFirestoreClient()
    with pytest.raises(ValueError, match="event_id"):
        get_cursor("", "x", "q1", client=client)
    with pytest.raises(ValueError, match="source"):
        get_reads("test-event", "   ", client=client)

