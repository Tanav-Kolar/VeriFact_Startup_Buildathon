"""Unit and integration tests for services/x_backfill (Spec 02)."""

from __future__ import annotations

import copy
import json
import logging
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from verifact_core.cursors import add_reads, get_cursor, get_reads, set_cursor  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
SERVICE_DIR = REPO_ROOT / "services" / "x_backfill"


# ---------------------------------------------------------------------------
# In-memory fakes (zero GCP / zero live X API)
# ---------------------------------------------------------------------------


class FakeDocumentSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = copy.deepcopy(data) if data is not None else None

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data)


class FakeDocumentReference:
    def __init__(
        self, store: dict[str, dict[str, Any]], doc_id: str, lock: threading.Lock
    ) -> None:
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
        self.write_count = 0

    def collection(self, name: str) -> FakeCollectionReference:
        with self._lock:
            col = self._collections.setdefault(name, {})
        return FakeCollectionReference(col, self._lock)


class RecordingBQClient:
    """In-memory BigQuery client capturing every row passed to insert_rows_json."""

    def __init__(self) -> None:
        self.project = "test-project"
        self.rows: list[dict[str, Any]] = []
        self.calls: int = 0

    def insert_rows_json(
        self, table: str, json_rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self.calls += 1
        self.rows.extend(copy.deepcopy(json_rows))
        return []


def _load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / name
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("synthetic") is True, f"Fixture {name} must have synthetic=true"
    return copy.deepcopy(data["response"])


def _write_synthetic_event(
    tmp_path: Path,
    *,
    event_id: str = "acme-widget-hub",
    t0_utc: str = "2026-11-16T13:37:16Z",
    pre_hours: int = 72,
    post_hours: int = 240,
    x_enabled: bool = True,
    max_posts: int | None = 1500,
    official_posts: list[str] | None = None,
    thread_ids: list[str] | None = None,
    official_accounts: list[str] | None = None,
    core_keywords: list[str] | None = None,
    context_keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    org_entities: dict[str, list[str]] | None = None,
) -> Path:
    x_block: dict[str, Any] = {
        "enabled": x_enabled,
        "thread_ids": thread_ids if thread_ids is not None else ["1000000000000000101"],
    }
    if max_posts is not None:
        x_block["max_posts"] = max_posts

    sources: dict[str, Any] = {"x": x_block, "hn": {"enabled": True}}

    data: dict[str, Any] = {
        "event_id": event_id,
        "name": "Acme Widget Hub Launch",
        "t0": {"utc": t0_utc},
        "window": {"pre_hours": pre_hours, "post_hours": post_hours, "live": True},
        "official": {
            "accounts": official_accounts if official_accounts is not None else ["AcmeCorp"],
            "posts": (
                official_posts
                if official_posts is not None
                else ["1000000000000000101", "1000000000000000102"]
            ),
            "urls": ["https://acme.example.org/blog/widget-hub"],
            "spokespeople": ["Alex Rivera"],
        },
        "entities": {
            "org": (
                org_entities
                if org_entities is not None
                else {"Acme": ["Acme Cloud", "@AcmeCorp"]}
            ),
            "product": {"Widget Hub": ["Acme Widget Hub"]},
            "related": [],
        },
        "keywords": {
            "core": (
                core_keywords
                if core_keywords is not None
                else ["acme widget hub", '"widget hub" acme']
            ),
            "context": (
                context_keywords
                if context_keywords is not None
                else ["inference", "hosting", "subsidy"]
            ),
            "exclude": exclude_keywords if exclude_keywords is not None else ["giveaway"],
        },
        "languages": ["en"],
        "sources": sources,
    }
    path = tmp_path / f"{event_id}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _make_standard_mock_client(
    recorded: list[httpx.Request],
) -> httpx.Client:
    """Build an httpx.Client backed by MockTransport serving synthetic fixtures."""
    lookup_resp = _load_fixture("x_backfill_thread_lookup.json")
    replies_p1 = _load_fixture("x_backfill_thread_replies_p1.json")
    replies_p2 = _load_fixture("x_backfill_thread_replies_p2.json")
    quotes_resp = _load_fixture("x_backfill_quotes.json")
    official_resp = _load_fixture("x_backfill_official.json")
    keywords_resp = _load_fixture("x_backfill_keywords.json")

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        path = request.url.path
        params = dict(request.url.params)

        if path == "/2/tweets":
            ids = [i for i in params.get("ids", "").split(",") if i]
            posts = [p for p in lookup_resp["data"] if p["id"] in ids]
            return httpx.Response(200, json={"data": posts})

        if path.startswith("/2/tweets/") and path.endswith("/quote_tweets"):
            if "1000000000000000101" in path:
                return httpx.Response(200, json=quotes_resp)
            return httpx.Response(200, json={"meta": {"result_count": 0}})

        if path == "/2/tweets/search/recent":
            query = params.get("query", "")
            if query.startswith("conversation_id:1000000000000000101"):
                if params.get("next_token") == "synth_thread_next_p2":
                    return httpx.Response(200, json=replies_p2)
                return httpx.Response(200, json=replies_p1)
            if query.startswith("conversation_id:"):
                return httpx.Response(200, json={"meta": {"result_count": 0}})
            if query.startswith("from:"):
                return httpx.Response(200, json=official_resp)
            return httpx.Response(200, json=keywords_resp)

        raise AssertionError(f"Unexpected X API endpoint: {path}")

    return httpx.Client(
        base_url="https://api.x.com",
        transport=httpx.MockTransport(handler),
    )


def _extract_json_logs(caplog: pytest.LogCaptureFixture, event_name: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for rec in caplog.records:
        try:
            parsed = json.loads(rec.getMessage())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("event") == event_name:
            results.append(parsed)
    return results


# ---------------------------------------------------------------------------
# AC 1: --dry-run makes zero HTTP requests and prints all four steps
# ---------------------------------------------------------------------------


def test_ac1_dry_run_makes_zero_http_calls_and_prints_all_four_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 1: --dry-run makes zero HTTP calls (failing client) and prints all 4 steps."""
    from services.x_backfill.main import run_backfill

    event_path = _write_synthetic_event(tmp_path)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)

    def fail_on_any_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"HTTP call must not happen in dry-run: {request.url}")

    failing_client = httpx.Client(
        base_url="https://api.x.com",
        transport=httpx.MockTransport(fail_on_any_call),
    )
    bq_client = RecordingBQClient()
    fs_client = FakeFirestoreClient()
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    with caplog.at_level(logging.INFO):
        summary = run_backfill(
            event_path=event_path,
            dry_run=True,
            http_client=failing_client,
            bq_client=bq_client,
            firestore_client=fs_client,
            now_fn=lambda: frozen_now,
        )

    assert bq_client.calls == 0
    assert get_reads("acme-widget-hub", "x", client=fs_client) == 0
    assert summary.reads_total == 0
    assert summary.rows_written == 0

    stdout_text = capsys.readouterr().out
    plan_logs = _extract_json_logs(caplog, "x_backfill_dry_run_plan")
    assert len(plan_logs) == 1
    plan = plan_logs[0]
    planned_steps = {req["step"] for req in plan["requests"]}
    assert planned_steps == {"thread", "quotes", "official", "keywords"}
    assert "start_time" in plan
    assert "end_time" in plan
    assert plan["budget"] == 1500
    # Verify queries appear in printed stdout JSON too
    assert "conversation_id:1000000000000000101" in stdout_text
    assert "from:AcmeCorp" in stdout_text
    assert "-is:retweet" in stdout_text
    assert "-giveaway" in stdout_text


# ---------------------------------------------------------------------------
# AC 2: --limit 10 requests at most 10 posts and updates reads_total exactly
# ---------------------------------------------------------------------------


def test_ac2_limit_10_respects_budget_and_updates_reads_total_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 2: --limit 10 never requests >10 posts and reads_total rises by exact returned count."""
    from services.x_backfill.main import run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    event_path = _write_synthetic_event(tmp_path)
    recorded: list[httpx.Request] = []
    http_client = _make_standard_mock_client(recorded)
    bq_client = RecordingBQClient()
    fs_client = FakeFirestoreClient()
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    summary = run_backfill(
        event_path=event_path,
        limit=10,
        http_client=http_client,
        bq_client=bq_client,
        firestore_client=fs_client,
        now_fn=lambda: frozen_now,
    )

    # First call is /2/tweets lookup of 2 IDs -> returns 2 posts -> remaining budget 8 (< min 10)
    # Job must stop with cap_reached without issuing a search call requesting max_results=10
    total_requested = 0
    for req in recorded:
        params = dict(req.url.params)
        if req.url.path == "/2/tweets":
            total_requested += len([i for i in params["ids"].split(",") if i])
        else:
            max_res = int(params["max_results"])
            assert 10 <= max_res <= 100
            total_requested += max_res

    assert total_requested <= 10
    assert summary.reads_total == 2
    assert get_reads("acme-widget-hub", "x", client=fs_client) == 2
    assert summary.stop_reason == "cap_reached"
    assert summary.rows_written == 2


def test_ac2_limit_10_on_search_step_caps_max_results_to_10(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 2 (search path): --limit 10 on keywords requests max_results=10 and rises by returned count."""
    from services.x_backfill.main import run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    event_path = _write_synthetic_event(tmp_path)
    recorded: list[httpx.Request] = []

    synth_posts = [
        {
            "id": f"2000000000000000{i:03d}",
            "text": f"Synthetic keyword post {i}",
            "author_id": "9000000050",
            "created_at": "2026-11-17T12:00:00.000Z",
            "lang": "en",
        }
        for i in range(10)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(
            200,
            json={
                "data": synth_posts,
                "meta": {"newest_id": synth_posts[0]["id"], "result_count": 10, "next_token": "more"},
            },
        )

    http_client = httpx.Client(base_url="https://api.x.com", transport=httpx.MockTransport(handler))
    bq_client = RecordingBQClient()
    fs_client = FakeFirestoreClient()
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    summary = run_backfill(
        event_path=event_path,
        limit=10,
        step="keywords",
        http_client=http_client,
        bq_client=bq_client,
        firestore_client=fs_client,
        now_fn=lambda: frozen_now,
    )

    assert len(recorded) == 1
    assert recorded[0].url.params["max_results"] == "10"
    assert summary.reads_total == 10
    assert get_reads("acme-widget-hub", "x", client=fs_client) == 10
    assert summary.stop_reason == "cap_reached"


# ---------------------------------------------------------------------------
# AC 3: reads_total already at max_posts -> exit 0, cap_reached, zero requests
# ---------------------------------------------------------------------------


def test_ac3_reads_at_max_posts_exits_0_cap_reached_zero_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 3: reads_total == max_posts -> exit 0, cap_reached, zero X requests."""
    from services.x_backfill.main import main, run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    event_path = _write_synthetic_event(tmp_path, max_posts=50)
    fs_client = FakeFirestoreClient()
    add_reads("acme-widget-hub", "x", 50, client=fs_client)

    def fail_on_any_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Must not call X API when cap is already reached")

    http_client = httpx.Client(
        base_url="https://api.x.com",
        transport=httpx.MockTransport(fail_on_any_call),
    )
    bq_client = RecordingBQClient()
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    with caplog.at_level(logging.INFO):
        summary = run_backfill(
            event_path=event_path,
            http_client=http_client,
            bq_client=bq_client,
            firestore_client=fs_client,
            now_fn=lambda: frozen_now,
        )

    assert summary.stop_reason == "cap_reached"
    assert summary.reads_total == 50
    assert summary.rows_written == 0
    assert bq_client.calls == 0

    done_logs = _extract_json_logs(caplog, "x_backfill_done")
    assert len(done_logs) == 1
    assert done_logs[0]["stop_reason"] == "cap_reached"
    assert done_logs[0]["reads_total"] == 50

    # Also verify CLI exit code is 0 when run via main()
    monkeypatch.setattr(
        "services.x_backfill.main.run_backfill",
        lambda **kwargs: summary,
    )
    assert main(["--event", str(event_path)]) == 0


# ---------------------------------------------------------------------------
# AC 4: Re-running after a completed step skips it (step_done)
# ---------------------------------------------------------------------------


def test_ac4_rerun_skips_completed_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4: Completed step (step_done=True) is skipped with zero requests on re-run."""
    from services.x_backfill.main import run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    event_path = _write_synthetic_event(tmp_path)
    fs_client = FakeFirestoreClient()
    bq_client = RecordingBQClient()
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    # Mark 'thread' already completed before run
    set_cursor(
        "acme-widget-hub",
        "x",
        "thread",
        "1000000000000000203",
        step_done=True,
        updated_at=frozen_now,
        client=fs_client,
    )

    recorded_run1: list[httpx.Request] = []
    client1 = _make_standard_mock_client(recorded_run1)
    summary1 = run_backfill(
        event_path=event_path,
        http_client=client1,
        bq_client=bq_client,
        firestore_client=fs_client,
        now_fn=lambda: frozen_now,
    )

    assert summary1.stop_reason == "completed"
    assert summary1.reads_by_step["thread"] == 0
    assert not any(
        r.url.path == "/2/tweets" or "conversation_id:" in r.url.params.get("query", "")
        for r in recorded_run1
    )
    for step_name in ("thread", "quotes", "official", "keywords"):
        cur = get_cursor("acme-widget-hub", "x", step_name, client=fs_client)
        assert cur is not None and cur.step_done is True

    # Re-run after ALL steps completed -> zero HTTP requests
    def fail_on_any_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request on completed re-run: {request.url}")

    client2 = httpx.Client(
        base_url="https://api.x.com",
        transport=httpx.MockTransport(fail_on_any_call),
    )
    summary2 = run_backfill(
        event_path=event_path,
        http_client=client2,
        bq_client=bq_client,
        firestore_client=fs_client,
        now_fn=lambda: frozen_now,
    )
    assert summary2.stop_reason == "completed"
    assert sum(summary2.reads_by_step.values()) == 0


# ---------------------------------------------------------------------------
# AC 5: No expansions, no user.fields, no user lookup endpoint anywhere
# ---------------------------------------------------------------------------


def test_ac5_no_expansions_user_fields_or_user_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 5: Every request has required tweet.fields, no expansions/user.fields, no user endpoints."""
    from services.x_backfill.main import TWEET_FIELDS, run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    event_path = _write_synthetic_event(tmp_path)
    recorded: list[httpx.Request] = []
    http_client = _make_standard_mock_client(recorded)
    bq_client = RecordingBQClient()
    fs_client = FakeFirestoreClient()
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    run_backfill(
        event_path=event_path,
        http_client=http_client,
        bq_client=bq_client,
        firestore_client=fs_client,
        now_fn=lambda: frozen_now,
    )

    assert len(recorded) >= 5
    for req in recorded:
        params = dict(req.url.params)
        assert params.get("tweet.fields") == TWEET_FIELDS
        assert "expansions" not in params
        assert "user.fields" not in params
        assert "/users" not in req.url.path

    # Static check across services/x_backfill source files
    for py_file in SERVICE_DIR.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert "/2/users" not in content
        assert "/users/by" not in content
        assert "user.fields" not in content
        assert "expansions" not in content


# ---------------------------------------------------------------------------
# AC 6: raw_items fields and unmodified payload
# ---------------------------------------------------------------------------


def test_ac6_raw_items_metadata_and_unmodified_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 6: Every row has source='x', captured_by='backfill', step query_id, unmodified payload."""
    from services.x_backfill.main import run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    event_path = _write_synthetic_event(tmp_path)
    recorded: list[httpx.Request] = []
    http_client = _make_standard_mock_client(recorded)
    bq_client = RecordingBQClient()
    fs_client = FakeFirestoreClient()
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    run_backfill(
        event_path=event_path,
        http_client=http_client,
        bq_client=bq_client,
        firestore_client=fs_client,
        now_fn=lambda: frozen_now,
    )

    # Expected unmodified payloads indexed by post id
    expected_by_id: dict[str, dict[str, Any]] = {}
    for fname in (
        "x_backfill_thread_lookup.json",
        "x_backfill_thread_replies_p1.json",
        "x_backfill_thread_replies_p2.json",
        "x_backfill_quotes.json",
        "x_backfill_official.json",
        "x_backfill_keywords.json",
    ):
        for post in _load_fixture(fname)["data"]:
            expected_by_id[post["id"]] = post

    assert len(bq_client.rows) >= len(expected_by_id)
    seen_steps: set[str] = set()
    for row in bq_client.rows:
        assert row["event_id"] == "acme-widget-hub"
        assert row["source"] == "x"
        assert row["captured_by"] == "backfill"
        assert row["query_id"] in {"thread", "quotes", "official", "keywords"}
        seen_steps.add(row["query_id"])
        expected_payload = expected_by_id[row["source_id"]]
        assert json.dumps(row["payload"], ensure_ascii=False, sort_keys=True) == json.dumps(
            expected_payload, ensure_ascii=False, sort_keys=True
        )

    assert seen_steps == {"thread", "quotes", "official", "keywords"}


# ---------------------------------------------------------------------------
# AC 7: Strict step order thread -> quotes -> official -> keywords
# ---------------------------------------------------------------------------


def test_ac7_strict_step_order_and_coherent_stop_at_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 7: Steps run thread -> quotes -> official -> keywords; stopping at cap leaves earlier steps complete."""
    from services.x_backfill.main import run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    event_path = _write_synthetic_event(tmp_path)
    recorded: list[httpx.Request] = []
    http_client = _make_standard_mock_client(recorded)
    bq_client = RecordingBQClient()
    fs_client = FakeFirestoreClient()
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    # Full run verifies strict step ordering
    summary = run_backfill(
        event_path=event_path,
        http_client=http_client,
        bq_client=bq_client,
        firestore_client=fs_client,
        now_fn=lambda: frozen_now,
    )
    assert summary.stop_reason == "completed"

    def _classify_req(req: httpx.Request) -> str:
        if req.url.path == "/2/tweets" or "conversation_id:" in req.url.params.get("query", ""):
            return "thread"
        if req.url.path.endswith("/quote_tweets"):
            return "quotes"
        if req.url.params.get("query", "").startswith("from:"):
            return "official"
        return "keywords"

    step_sequence = [_classify_req(r) for r in recorded]
    order_rank = {"thread": 0, "quotes": 1, "official": 2, "keywords": 3}
    ranks = [order_rank[s] for s in step_sequence]
    assert ranks == sorted(ranks)
    assert set(step_sequence) == {"thread", "quotes", "official", "keywords"}

    # Verify keywords step uses sort_order=relevancy
    keyword_reqs = [r for r in recorded if _classify_req(r) == "keywords"]
    assert len(keyword_reqs) >= 1
    for req in keyword_reqs:
        assert req.url.params.get("sort_order") == "relevancy"
        assert "-is:retweet" in req.url.params.get("query", "")
        assert "-giveaway" in req.url.params.get("query", "")

    # Now test stopping at cap after thread + quotes:
    # With 1 official post, thread returns 1 (lookup) + 2 (p1) + 1 (p2) = 4 posts,
    # quotes returns 1 post -> total 5 posts; with limit=14, remaining=9 (< min 10) right after quotes!
    single_post_event = _write_synthetic_event(
        tmp_path,
        event_id="acme-single-post",
        official_posts=["1000000000000000101"],
        thread_ids=["1000000000000000101"],
    )
    recorded_cap: list[httpx.Request] = []
    http_cap = _make_standard_mock_client(recorded_cap)
    bq_cap = RecordingBQClient()
    fs_cap = FakeFirestoreClient()

    summary_cap = run_backfill(
        event_path=single_post_event,
        limit=14,
        http_client=http_cap,
        bq_client=bq_cap,
        firestore_client=fs_cap,
        now_fn=lambda: frozen_now,
    )
    assert summary_cap.stop_reason == "cap_reached"
    # thread (4) + quotes (1) = 5 reads -> remaining budget 9 < 10 before official starts
    assert get_cursor("acme-single-post", "x", "thread", client=fs_cap).step_done is True  # type: ignore[union-attr]
    assert get_cursor("acme-single-post", "x", "quotes", client=fs_cap).step_done is True  # type: ignore[union-attr]
    assert get_cursor("acme-single-post", "x", "official", client=fs_cap) is None
    assert get_cursor("acme-single-post", "x", "keywords", client=fs_cap) is None

    # Resuming with a higher limit skips thread & quotes and completes official & keywords
    recorded_resume: list[httpx.Request] = []
    http_resume = _make_standard_mock_client(recorded_resume)
    summary_resume = run_backfill(
        event_path=single_post_event,
        limit=100,
        http_client=http_resume,
        bq_client=bq_cap,
        firestore_client=fs_cap,
        now_fn=lambda: frozen_now,
    )
    assert summary_resume.stop_reason == "completed"
    assert summary_resume.reads_by_step["thread"] == 0
    assert summary_resume.reads_by_step["quotes"] == 0
    assert summary_resume.reads_by_step["official"] == 1
    assert summary_resume.reads_by_step["keywords"] >= 1
    assert get_cursor("acme-single-post", "x", "official", client=fs_cap).step_done is True  # type: ignore[union-attr]
    assert get_cursor("acme-single-post", "x", "keywords", client=fs_cap).step_done is True  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# AC 8: Search start_time is never earlier than now - 7 days (frozen clock)
# ---------------------------------------------------------------------------


def test_ac8_search_time_bounds_clamped_to_7_days_frozen_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 8: start_time is never earlier than now - 7 days; end_time <= now - 30s."""
    from services.x_backfill.main import compute_search_bounds, run_backfill
    from verifact_core.config import load_event

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    # t0 = 2026-11-16T13:37:16Z, pre_hours = 72 -> raw start = 2026-11-13T13:37:16Z
    event_path = _write_synthetic_event(tmp_path)
    cfg = load_event(event_path)

    # Frozen clock 6 days after T0: raw_start (Nov 13) is > 7 days before now (Nov 22)
    frozen_now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    start_dt, end_dt = compute_search_bounds(cfg, frozen_now)
    assert start_dt == frozen_now - timedelta(days=7) + timedelta(minutes=10)
    assert start_dt >= frozen_now - timedelta(days=7)
    assert end_dt == frozen_now - timedelta(seconds=30)

    # Verify actual HTTP requests send the clamped start_time and end_time
    recorded: list[httpx.Request] = []
    http_client = _make_standard_mock_client(recorded)
    bq_client = RecordingBQClient()
    fs_client = FakeFirestoreClient()

    run_backfill(
        event_path=event_path,
        http_client=http_client,
        bq_client=bq_client,
        firestore_client=fs_client,
        now_fn=lambda: frozen_now,
    )

    search_reqs = [r for r in recorded if r.url.path == "/2/tweets/search/recent"]
    assert len(search_reqs) >= 1
    for req in search_reqs:
        req_start = datetime.fromisoformat(req.url.params["start_time"].replace("Z", "+00:00"))
        req_end = datetime.fromisoformat(req.url.params["end_time"].replace("Z", "+00:00"))
        assert req_start >= frozen_now - timedelta(days=7)
        assert req_start == start_dt
        assert req_end == end_dt


# ---------------------------------------------------------------------------
# AC 9: 429 waits and retries; 5xx retries up to 3x; 403 on quotes -> official
# ---------------------------------------------------------------------------


def test_ac9_429_waits_and_retries_and_403_on_quotes_moves_to_official(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 9: 429 waits until x-rate-limit-reset and retries; 403 on quotes logs and moves to official."""
    from services.x_backfill.main import run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    event_path = _write_synthetic_event(tmp_path)
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    reset_ts = int(frozen_now.timestamp()) + 42

    lookup_resp = _load_fixture("x_backfill_thread_lookup.json")
    official_resp = _load_fixture("x_backfill_official.json")
    keywords_resp = _load_fixture("x_backfill_keywords.json")

    sleeps: list[float] = []
    attempts_by_path: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        attempts_by_path[path] = attempts_by_path.get(path, 0) + 1

        if path == "/2/tweets":
            if attempts_by_path[path] == 1:
                return httpx.Response(
                    429,
                    headers={"x-rate-limit-reset": str(reset_ts)},
                    json={"title": "Too Many Requests"},
                )
            return httpx.Response(200, json=lookup_resp)

        if path.endswith("/quote_tweets"):
            return httpx.Response(403, json={"title": "Forbidden", "detail": "Not permitted"})

        if path == "/2/tweets/search/recent":
            query = request.url.params.get("query", "")
            if query.startswith("conversation_id:"):
                return httpx.Response(200, json={"meta": {"result_count": 0}})
            if query.startswith("from:"):
                return httpx.Response(200, json=official_resp)
            return httpx.Response(200, json=keywords_resp)

        raise AssertionError(f"Unexpected endpoint: {path}")

    http_client = httpx.Client(base_url="https://api.x.com", transport=httpx.MockTransport(handler))
    bq_client = RecordingBQClient()
    fs_client = FakeFirestoreClient()

    with caplog.at_level(logging.INFO):
        summary = run_backfill(
            event_path=event_path,
            http_client=http_client,
            bq_client=bq_client,
            firestore_client=fs_client,
            now_fn=lambda: frozen_now,
            sleep_fn=lambda s: sleeps.append(s),
        )

    # 429 waited 42 seconds and succeeded on retry
    assert 42.0 in sleeps
    assert attempts_by_path["/2/tweets"] == 2

    # 403 on quotes logged and continued to official & keywords
    auth_logs = _extract_json_logs(caplog, "x_step_auth_error")
    assert len(auth_logs) == 1
    assert auth_logs[0]["step"] == "quotes"
    assert auth_logs[0]["status_code"] == 403

    assert summary.reads_by_step["thread"] == 2
    assert summary.reads_by_step["quotes"] == 0
    assert summary.reads_by_step["official"] == 1
    assert summary.reads_by_step["keywords"] >= 1
    assert summary.stop_reason == "completed"


def test_5xx_retries_up_to_3_times_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Step 7: 5xx retries up to 3 times with backoff, recovers or exits with stop_reason='error'."""
    from services.x_backfill.main import XApiError, run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    event_path = _write_synthetic_event(tmp_path)
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    official_resp = _load_fixture("x_backfill_official.json")

    # Case 1: 2 failures then 200 -> recovers
    sleeps_ok: list[float] = []
    calls_ok = 0

    def handler_recover(request: httpx.Request) -> httpx.Response:
        nonlocal calls_ok
        calls_ok += 1
        if calls_ok <= 2:
            return httpx.Response(503, json={"title": "Service Unavailable"})
        return httpx.Response(200, json=official_resp)

    summary_ok = run_backfill(
        event_path=event_path,
        step="official",
        http_client=httpx.Client(
            base_url="https://api.x.com", transport=httpx.MockTransport(handler_recover)
        ),
        bq_client=RecordingBQClient(),
        firestore_client=FakeFirestoreClient(),
        now_fn=lambda: frozen_now,
        sleep_fn=lambda s: sleeps_ok.append(s),
    )
    assert calls_ok == 3
    assert len(sleeps_ok) == 2
    assert sleeps_ok[1] > sleeps_ok[0] > 0
    assert summary_ok.stop_reason == "completed"

    # Case 2: persistent 500 -> 1 initial + 3 retries = 4 calls, emits stop_reason='error', raises
    sleeps_fail: list[float] = []
    calls_fail = 0

    def handler_fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls_fail
        calls_fail += 1
        return httpx.Response(500, json={"title": "Internal Server Error"})

    caplog.clear()
    with caplog.at_level(logging.INFO), pytest.raises(XApiError):
        run_backfill(
            event_path=event_path,
            step="official",
            http_client=httpx.Client(
                base_url="https://api.x.com", transport=httpx.MockTransport(handler_fail)
            ),
            bq_client=RecordingBQClient(),
            firestore_client=FakeFirestoreClient(),
            now_fn=lambda: frozen_now,
            sleep_fn=lambda s: sleeps_fail.append(s),
        )

    assert calls_fail == 4
    assert len(sleeps_fail) == 3
    done_logs = _extract_json_logs(caplog, "x_backfill_done")
    assert len(done_logs) == 1
    assert done_logs[0]["stop_reason"] == "error"


# ---------------------------------------------------------------------------
# AC 10: No event-specific literals in services/x_backfill/
# ---------------------------------------------------------------------------


def test_ac10_no_event_specific_literals_in_service() -> None:
    """AC 10: No event-specific literals appear anywhere in services/x_backfill/."""
    forbidden_patterns = [
        r"sarvam",
        r"2122323115535720921",
        r"2122323152172883984",
        r"token factory",
    ]
    files = list(SERVICE_DIR.rglob("*"))
    assert any(f.name == "main.py" for f in files)
    for file_path in files:
        if file_path.is_file() and "__pycache__" not in file_path.parts:
            text = file_path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                assert not re.search(pattern, text, re.IGNORECASE), (
                    f"Forbidden event-specific literal {pattern!r} found in {file_path}"
                )


# ---------------------------------------------------------------------------
# AC 11: Final x_backfill_done structured JSON log line with all required fields
# ---------------------------------------------------------------------------


def test_ac11_final_x_backfill_done_log_contains_all_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 11: Final x_backfill_done JSON log contains reads per step, reads_total, rows_written, rows_rejected, stop_reason."""
    from services.x_backfill.main import run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    event_path = _write_synthetic_event(tmp_path)
    recorded: list[httpx.Request] = []
    http_client = _make_standard_mock_client(recorded)
    bq_client = RecordingBQClient()
    fs_client = FakeFirestoreClient()
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    with caplog.at_level(logging.INFO):
        summary = run_backfill(
            event_path=event_path,
            http_client=http_client,
            bq_client=bq_client,
            firestore_client=fs_client,
            now_fn=lambda: frozen_now,
        )

    done_logs = _extract_json_logs(caplog, "x_backfill_done")
    assert len(done_logs) == 1
    log = done_logs[0]

    assert log["event"] == "x_backfill_done"
    assert log["event_id"] == "acme-widget-hub"
    assert isinstance(log["run_id"], str) and log["run_id"]
    assert set(log["reads_by_step"].keys()) == {"thread", "quotes", "official", "keywords"}
    assert log["reads_total"] == summary.reads_total
    assert log["rows_written"] == summary.rows_written
    assert log["rows_rejected"] == 0
    assert log["stop_reason"] == "completed"


# ---------------------------------------------------------------------------
# Startup validation & query builder edge cases
# ---------------------------------------------------------------------------


def test_refuses_to_start_when_x_disabled_or_token_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 1: Refuse to start when sources.x.enabled is false or X_BEARER_TOKEN is unset."""
    from services.x_backfill.main import BackfillConfigError, main, run_backfill

    # X disabled
    disabled_path = _write_synthetic_event(tmp_path, event_id="x-disabled", x_enabled=False, max_posts=None)
    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    with pytest.raises(BackfillConfigError, match="enabled"):
        run_backfill(event_path=disabled_path, firestore_client=FakeFirestoreClient())
    assert main(["--event", str(disabled_path)]) != 0

    # Missing X_BEARER_TOKEN (without --dry-run)
    enabled_path = _write_synthetic_event(tmp_path, event_id="x-enabled")
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    with pytest.raises(BackfillConfigError, match="X_BEARER_TOKEN"):
        run_backfill(event_path=enabled_path, firestore_client=FakeFirestoreClient())
    assert main(["--event", str(enabled_path)]) != 0


def test_build_keyword_queries_splits_long_queries_under_512_chars(tmp_path: Path) -> None:
    """Keywords builder produces valid queries <= 512 chars with core, context+org, exclude, -is:retweet."""
    from services.x_backfill.main import build_keyword_queries
    from verifact_core.config import load_event

    many_context = [f"contextterm{i:03d}" for i in range(45)]
    event_path = _write_synthetic_event(
        tmp_path,
        event_id="long-keywords",
        context_keywords=many_context,
        exclude_keywords=["spam", "airdrop promo"],
    )
    cfg = load_event(event_path)
    queries = build_keyword_queries(cfg)

    assert len(queries) >= 3  # 1 core + at least 2 chunked context queries
    for q in queries:
        assert len(q) <= 512
        assert "-is:retweet" in q
        assert "-spam" in q
        assert '-"airdrop promo"' in q

    # Every context term appears in at least one query alongside org aliases
    combined = "\n".join(queries)
    for term in many_context:
        assert term in combined
    assert 'Acme OR "Acme Cloud" OR @AcmeCorp' in combined


def test_bq_rejected_rows_counted_in_summary_and_done_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify rows_rejected from write_raw_items propagates to x_backfill_done."""
    from services.x_backfill.main import run_backfill

    monkeypatch.setenv("X_BEARER_TOKEN", "test-synthetic-token")
    monkeypatch.setattr("verifact_core.bq.time.sleep", lambda _: None)
    event_path = _write_synthetic_event(tmp_path)
    recorded: list[httpx.Request] = []
    http_client = _make_standard_mock_client(recorded)

    rejecting_bq = MagicMock()
    rejecting_bq.project = "test-project"
    rejecting_bq.insert_rows_json.return_value = [
        {"index": 0, "errors": [{"reason": "invalid", "message": "bad row"}]}
    ]
    fs_client = FakeFirestoreClient()
    frozen_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    with caplog.at_level(logging.INFO):
        summary = run_backfill(
            event_path=event_path,
            step="official",
            http_client=http_client,
            bq_client=rejecting_bq,
            firestore_client=fs_client,
            now_fn=lambda: frozen_now,
        )

    assert summary.rows_written == 0
    assert summary.rows_rejected == 1
    done_logs = _extract_json_logs(caplog, "x_backfill_done")
    assert done_logs[0]["rows_rejected"] == 1
