"""Unit and integration tests for services/collect_x (live scheduled X collector)."""

from __future__ import annotations

import copy
import json
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from verifact_core.cursors import add_reads, get_cursor, get_reads, set_cursor  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
SERVICE_DIR = REPO_ROOT / "services" / "collect_x"

EXPECTED_TWEET_FIELDS = (
    "created_at,lang,author_id,conversation_id,referenced_tweets,entities,public_metrics"
)


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

    def collection(self, name: str) -> FakeCollectionReference:
        with self._lock:
            col = self._collections.setdefault(name, {})
        return FakeCollectionReference(col, self._lock)

    def raw_docs(self, collection_name: str = "collector_cursors") -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._collections.get(collection_name, {}))


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
    live: bool = True,
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

    data: dict[str, Any] = {
        "event_id": event_id,
        "name": "Acme Widget Hub Launch",
        "t0": {"utc": t0_utc},
        "window": {"pre_hours": pre_hours, "post_hours": post_hours, "live": live},
        "official": {
            "accounts": official_accounts if official_accounts is not None else ["AcmeCorp"],
            "posts": (
                official_posts
                if official_posts is not None
                else ["1000000000000000101"]
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
                else ["acme widget hub"]
            ),
            "context": (
                context_keywords
                if context_keywords is not None
                else ["inference"]
            ),
            "exclude": exclude_keywords if exclude_keywords is not None else ["giveaway"],
        },
        "languages": ["en"],
        "sources": {"x": x_block, "hn": {"enabled": True}},
        "turning_points": {"detect_official_response": True, "manual": []},
        "seed_storylines": [],
        "claims_to_check": [],
        "conflicts_of_interest": [],
        "known_coverage": [],
    }
    path = tmp_path / f"{event_id}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _parse_json_logs(stdout: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _make_mock_client(handler: Any) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.x.com",
        transport=httpx.MockTransport(handler),
    )


def _make_post(post_id: str, text: str = "Synthetic post text", created_at: str = "2026-11-18T12:00:00.000Z") -> dict[str, Any]:
    return {
        "id": post_id,
        "text": text,
        "author_id": "9000000099",
        "created_at": created_at,
        "lang": "en",
        "conversation_id": post_id,
        "public_metrics": {
            "retweet_count": 1,
            "reply_count": 0,
            "like_count": 5,
            "quote_count": 0,
            "bookmark_count": 0,
            "impression_count": 100,
        },
    }


# ---------------------------------------------------------------------------
# 1. Startup validation & S2.2 refusal tests
# ---------------------------------------------------------------------------


def test_refuses_when_x_source_disabled(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXConfigError, CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(tmp_path, x_enabled=False, max_posts=500)
    with pytest.raises(CollectXConfigError, match="sources.x.enabled"):
        run_collect_x(
            event_path,
            settings=CollectXSettings(X_BEARER_TOKEN="test-token"),
            firestore_client=FakeFirestoreClient(),
            bq_client=RecordingBQClient(),
        )


def test_refuses_when_max_posts_missing(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(tmp_path, x_enabled=True, max_posts=None)
    # EventConfig itself rejects enabled x without max_posts (S2.2)
    with pytest.raises(ValueError, match="max_posts"):
        run_collect_x(
            event_path,
            settings=CollectXSettings(X_BEARER_TOKEN="test-token"),
            firestore_client=FakeFirestoreClient(),
            bq_client=RecordingBQClient(),
        )


def test_refuses_when_bearer_token_missing_without_dry_run(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXConfigError, CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(tmp_path, x_enabled=True, max_posts=500)
    with pytest.raises(CollectXConfigError, match="X_BEARER_TOKEN"):
        run_collect_x(
            event_path,
            dry_run=False,
            settings=CollectXSettings(X_BEARER_TOKEN=None),
            firestore_client=FakeFirestoreClient(),
            bq_client=RecordingBQClient(),
        )


def test_refuses_non_positive_limit(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXConfigError, CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(tmp_path, x_enabled=True, max_posts=500)
    with pytest.raises(CollectXConfigError, match="--limit"):
        run_collect_x(
            event_path,
            limit=0,
            dry_run=True,
            settings=CollectXSettings(X_BEARER_TOKEN=None),
        )


# ---------------------------------------------------------------------------
# 2. Dry-run mode (zero HTTP, zero writes, no token required)
# ---------------------------------------------------------------------------


def test_dry_run_makes_zero_http_requests_and_prints_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(tmp_path, max_posts=500)
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()

    def _fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected HTTP call in dry-run: {request.url}")

    frozen_now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_fail_if_called) as client:
        summary = run_collect_x(
            event_path,
            dry_run=True,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: frozen_now,
            settings=CollectXSettings(X_BEARER_TOKEN=None),
        )

    assert summary.stop_reason == "completed"
    assert summary.reads_this_run == 0
    assert summary.rows_written == 0
    assert bq.calls == 0
    assert fs.raw_docs() == {}

    logs = _parse_json_logs(capsys.readouterr().out)
    plan_logs = [r for r in logs if r.get("event") == "collect_x_dry_run_plan"]
    done_logs = [r for r in logs if r.get("event") == "collect_x_done"]
    assert len(plan_logs) == 1
    assert len(done_logs) == 1
    assert done_logs[0]["dry_run"] is True
    requests = plan_logs[0]["requests"]
    assert len(requests) >= 3  # thread + official + keywords
    for req in requests:
        assert req["path"] == "/2/tweets/search/recent"
        assert req["params"]["tweet.fields"] == EXPECTED_TWEET_FIELDS
        assert "expansions" not in req["params"]
        assert "user.fields" not in req["params"]


# ---------------------------------------------------------------------------
# 3. Shared read cap with x-backfill (S2.2, S2.3, S2.4)
# ---------------------------------------------------------------------------


def test_shared_read_cap_stops_immediately_when_exhausted_by_backfill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(tmp_path, max_posts=100)
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()
    # Simulate x-backfill already consuming the full cap on shared (event_id, "x")
    add_reads("acme-widget-hub", "x", 100, client=fs)

    def _fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"No X call allowed when cap is exhausted: {request.url}")

    frozen_now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_fail_if_called) as client:
        summary = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: frozen_now,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert summary.stop_reason == "cap_reached"
    assert summary.reads_this_run == 0
    assert summary.reads_total == 100
    assert summary.rows_written == 0
    assert bq.calls == 0

    done_logs = [r for r in _parse_json_logs(capsys.readouterr().out) if r.get("event") == "collect_x_done"]
    assert len(done_logs) == 1
    assert done_logs[0]["stop_reason"] == "cap_reached"
    assert done_logs[0]["reads_total"] == 100


def test_combined_backfill_and_collect_x_never_exceeds_max_posts(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(tmp_path, max_posts=100)
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()
    # Backfill already consumed 88 reads -> only 12 remaining
    add_reads("acme-widget-hub", "x", 88, client=fs)

    seen_max_results: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        max_res = int(request.url.params["max_results"])
        seen_max_results.append(max_res)
        # Return 10 posts (leaving 2 remaining, which is < SEARCH_MIN_RESULTS=10)
        posts = [_make_post(f"1000000000000000{900 + i}") for i in range(10)]
        return httpx.Response(
            200,
            json={"data": posts, "meta": {"newest_id": posts[0]["id"], "result_count": len(posts)}},
        )

    frozen_now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_handler) as client:
        summary = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: frozen_now,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert seen_max_results == [12]
    assert summary.stop_reason == "cap_reached"
    assert summary.reads_this_run == 10
    assert summary.reads_total == 98
    assert get_reads("acme-widget-hub", "x", client=fs) == 98
    assert summary.reads_total <= 100


def test_limit_flag_caps_single_run_reads(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(tmp_path, max_posts=1500)
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()
    # Prior reads from backfill = 200; limit = 10 should still allow up to 10 in this run
    add_reads("acme-widget-hub", "x", 200, client=fs)

    requested_max: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        max_res = int(request.url.params["max_results"])
        requested_max.append(max_res)
        posts = [_make_post(f"1000000000000001{100 + i}") for i in range(max_res)]
        return httpx.Response(
            200,
            json={
                "data": posts,
                "meta": {"newest_id": posts[0]["id"], "next_token": "more-pages", "result_count": len(posts)},
            },
        )

    frozen_now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_handler) as client:
        summary = run_collect_x(
            event_path,
            limit=10,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: frozen_now,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert requested_max == [10]
    assert summary.reads_this_run == 10
    assert summary.reads_total == 210
    assert summary.stop_reason == "cap_reached"
    assert get_reads("acme-widget-hub", "x", client=fs) == 210


# ---------------------------------------------------------------------------
# 4. Incremental since_id cursor & pagination behavior
# ---------------------------------------------------------------------------


def test_incremental_since_id_cursor_persisted_and_reused_on_next_run(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path,
        official_posts=[],
        thread_ids=[],
        official_accounts=["AcmeCorp"],
        core_keywords=["acme widget hub"],
        context_keywords=[],
    )
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()

    # Pre-seed a backfill cursor under "official" to confirm zero collision
    set_cursor("acme-widget-hub", "x", "official", "backfill-done", step_done=True, client=fs)

    requests_seen: list[dict[str, str]] = []
    call_idx = 0
    official_fx = _load_fixture("x_backfill_official.json")
    keywords_fx = _load_fixture("x_backfill_keywords.json")

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_idx
        params = dict(request.url.params)
        requests_seen.append(params)
        call_idx += 1
        if call_idx == 1:
            return httpx.Response(200, json=official_fx)
        if call_idx == 2:
            return httpx.Response(200, json=keywords_fx)
        # Run 2 responses: newer post for official, empty for keywords
        if "from:AcmeCorp" in params["query"]:
            newer = _make_post("1000000000000000999", text="Follow-up official update")
            return httpx.Response(
                200,
                json={"data": [newer], "meta": {"newest_id": "1000000000000000999", "result_count": 1}},
            )
        return httpx.Response(200, json={"meta": {"result_count": 0}})

    t1 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 18, 12, 15, 0, tzinfo=timezone.utc)

    with _make_mock_client(_handler) as client:
        s1 = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: t1,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )
        s2 = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: t2,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert s1.stop_reason == "completed"
    assert s1.reads_this_run == 2
    assert s2.stop_reason == "completed"
    assert s2.reads_this_run == 1
    assert s2.reads_total == 3

    # Run 1 had no cursor -> used start_time & end_time, no since_id
    assert "since_id" not in requests_seen[0]
    assert "start_time" in requests_seen[0] and "end_time" in requests_seen[0]
    assert "since_id" not in requests_seen[1]

    # Run 2 had saved cursors -> used since_id and omitted start_time/end_time
    assert requests_seen[2]["since_id"] == "1000000000000000401"
    assert "start_time" not in requests_seen[2]
    assert "end_time" not in requests_seen[2]
    assert requests_seen[3]["since_id"] == "1000000000000000501"

    # Official cursor advanced to 999; keywords cursor stayed at 501 despite 0 results
    cur_official = get_cursor("acme-widget-hub", "x", "live:official:acmecorp", client=fs)
    assert cur_official is not None
    assert cur_official.cursor == "1000000000000000999"
    assert cur_official.step_done is False

    # Backfill cursor remained untouched
    backfill_cur = get_cursor("acme-widget-hub", "x", "official", client=fs)
    assert backfill_cur is not None and backfill_cur.step_done is True


def test_multipage_preserves_highest_snowflake_id_as_cursor(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path,
        official_posts=[],
        thread_ids=[],
        official_accounts=["AcmeCorp"],
        core_keywords=["acme"],
        context_keywords=[],
    )
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()

    def _handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if "from:AcmeCorp" in params["query"]:
            if "next_token" not in params:
                # Page 1 has higher IDs
                return httpx.Response(
                    200,
                    json={
                        "data": [_make_post("2000000000000000300"), _make_post("2000000000000000250")],
                        "meta": {"newest_id": "2000000000000000300", "next_token": "p2", "result_count": 2},
                    },
                )
            # Page 2 has older (smaller) IDs
            return httpx.Response(
                200,
                json={
                    "data": [_make_post("2000000000000000200")],
                    "meta": {"newest_id": "2000000000000000200", "result_count": 1},
                },
            )
        return httpx.Response(200, json={"meta": {"result_count": 0}})

    t1 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_handler) as client:
        summary = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: t1,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert summary.reads_this_run == 3
    cur = get_cursor("acme-widget-hub", "x", "live:official:acmecorp", client=fs)
    assert cur is not None
    assert cur.cursor == "2000000000000000300"


def test_stale_cursor_older_than_7_days_falls_back_to_time_bounds(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path,
        official_posts=[],
        thread_ids=[],
        official_accounts=["AcmeCorp"],
        core_keywords=["acme"],
        context_keywords=[],
    )
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()

    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    stale_ts = now - timedelta(days=8)
    set_cursor(
        "acme-widget-hub",
        "x",
        "live:official:acmecorp",
        "1000000000000000001",
        step_done=False,
        updated_at=stale_ts,
        client=fs,
    )

    seen_params: list[dict[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json={"meta": {"result_count": 0}})

    with _make_mock_client(_handler) as client:
        run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: now,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    official_req = [p for p in seen_params if "from:AcmeCorp" in p["query"]][0]
    assert "since_id" not in official_req
    assert "start_time" in official_req
    parsed_start = datetime.fromisoformat(official_req["start_time"].replace("Z", "+00:00"))
    assert parsed_start >= now - timedelta(days=7)


# ---------------------------------------------------------------------------
# 5. RawItem schema, unmodified payload, and forbidden X params
# ---------------------------------------------------------------------------


def test_raw_items_written_with_api_captured_by_and_unmodified_payload(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path,
        official_posts=[],
        thread_ids=[],
        official_accounts=[],
        core_keywords=["acme widget hub"],
        context_keywords=[],
    )
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()
    fixture = _load_fixture("x_backfill_keywords.json")
    expected_payload = copy.deepcopy(fixture["data"][0])

    seen_requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json=fixture)

    frozen_now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_handler) as client:
        summary = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: frozen_now,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
            run_id="test-run-42",
        )

    assert summary.rows_written == 1
    assert summary.rows_rejected == 0
    assert len(bq.rows) == 1
    row = bq.rows[0]
    assert row["event_id"] == "acme-widget-hub"
    assert row["source"] == "x"
    assert row["source_id"] == expected_payload["id"]
    assert row["captured_by"] == "api"
    assert row["run_id"] == "test-run-42"
    assert row["query_id"].startswith("live:keywords:")
    assert row["payload"] == expected_payload

    assert len(seen_requests) == 1
    req = seen_requests[0]
    assert req.url.path == "/2/tweets/search/recent"
    assert req.url.params["tweet.fields"] == EXPECTED_TWEET_FIELDS
    assert "expansions" not in req.url.params
    assert "user.fields" not in req.url.params


# ---------------------------------------------------------------------------
# 6. Window bounds & window.live behavior
# ---------------------------------------------------------------------------


def test_exits_cleanly_with_zero_requests_when_window_closed_and_not_live(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    # t0 = Nov 16 13:37 UTC, post_hours = 24 -> window ends Nov 17 13:37 UTC
    event_path = _write_synthetic_event(
        tmp_path,
        t0_utc="2026-11-16T13:37:16Z",
        post_hours=24,
        live=False,
    )
    bq = RecordingBQClient()
    fs = FakeFirestoreClient()

    def _fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Should not call X when window is closed: {request.url}")

    after_window = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_fail_if_called) as client:
        summary = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: after_window,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert summary.stop_reason == "window_closed"
    assert summary.reads_this_run == 0
    assert summary.rows_written == 0
    assert bq.calls == 0

    done_logs = [r for r in _parse_json_logs(capsys.readouterr().out) if r.get("event") == "collect_x_done"]
    assert len(done_logs) == 1
    assert done_logs[0]["stop_reason"] == "window_closed"


def test_continues_past_post_hours_when_window_live_is_true(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path,
        t0_utc="2026-11-10T13:37:16Z",
        post_hours=24,
        live=True,
        official_posts=[],
        thread_ids=[],
        official_accounts=["AcmeCorp"],
        core_keywords=["acme"],
        context_keywords=[],
    )
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()
    seen_bounds: list[tuple[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_bounds.append((request.url.params["start_time"], request.url.params["end_time"]))
        return httpx.Response(200, json={"meta": {"result_count": 0}})

    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_handler) as client:
        summary = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: now,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert summary.stop_reason == "completed"
    assert len(seen_bounds) == 2
    for start_iso, end_iso in seen_bounds:
        start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        assert start_dt >= now - timedelta(days=7)
        assert end_dt == now - timedelta(seconds=30)


# ---------------------------------------------------------------------------
# 7. HTTP 429, 5xx retries, and 401/403 per-query skip
# ---------------------------------------------------------------------------


def test_http_429_waits_via_injectable_sleeper_and_retries(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path,
        official_posts=[],
        thread_ids=[],
        official_accounts=["AcmeCorp"],
        core_keywords=["acme"],
        context_keywords=[],
    )
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()
    sleeps: list[float] = []
    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    reset_ts = str(int(now.timestamp()) + 42)
    attempts = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"x-rate-limit-reset": reset_ts}, text="rate limited")
        return httpx.Response(
            200,
            json={"data": [_make_post("1000000000000000777")], "meta": {"newest_id": "1000000000000000777"}},
        )

    with _make_mock_client(_handler) as client:
        summary = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: now,
            sleep_fn=sleeps.append,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert summary.stop_reason == "completed"
    assert sleeps == [42.0]
    assert summary.reads_this_run == 2


def test_http_5xx_retries_up_to_3_times_then_succeeds(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path,
        official_posts=[],
        thread_ids=[],
        official_accounts=[],
        core_keywords=["acme"],
        context_keywords=[],
    )
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()
    sleeps: list[float] = []
    calls = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 3:
            return httpx.Response(503, text="service unavailable")
        return httpx.Response(
            200,
            json={"data": [_make_post("1000000000000000888")], "meta": {"newest_id": "1000000000000000888"}},
        )

    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_handler) as client:
        summary = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: now,
            sleep_fn=sleeps.append,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert calls == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert summary.stop_reason == "completed"
    assert summary.reads_this_run == 1


def test_http_5xx_fails_after_3_retries_and_logs_error_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.collect_x.main import CollectXSettings, XApiError, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path,
        official_posts=[],
        thread_ids=[],
        official_accounts=[],
        core_keywords=["acme"],
        context_keywords=[],
    )
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()
    sleeps: list[float] = []
    calls = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="bad gateway")

    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_handler) as client:
        with pytest.raises(XApiError, match="HTTP 502"):
            run_collect_x(
                event_path,
                http_client=client,
                bq_client=bq,
                firestore_client=fs,
                now_fn=lambda: now,
                sleep_fn=sleeps.append,
                settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
            )

    assert calls == 4  # 1 initial + 3 retries
    assert sleeps == [1.0, 2.0, 4.0]
    done_logs = [r for r in _parse_json_logs(capsys.readouterr().out) if r.get("event") == "collect_x_done"]
    assert len(done_logs) == 1
    assert done_logs[0]["stop_reason"] == "error"


@pytest.mark.parametrize("status_code", [401, 403])
def test_http_401_and_403_skip_only_failing_query_and_continue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], status_code: int
) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path,
        official_posts=[],
        thread_ids=[],
        official_accounts=["AcmeCorp"],
        core_keywords=["acme widget hub"],
        context_keywords=[],
    )
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()
    keywords_fx = _load_fixture("x_backfill_keywords.json")

    def _handler(request: httpx.Request) -> httpx.Response:
        if "from:AcmeCorp" in request.url.params["query"]:
            return httpx.Response(status_code, text="forbidden or unauthorized")
        return httpx.Response(200, json=keywords_fx)

    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_handler) as client:
        summary = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: now,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert summary.stop_reason == "completed"
    assert summary.queries_skipped_auth == ["live:official:acmecorp"]
    assert summary.reads_this_run == 1
    assert summary.rows_written == 1

    logs = _parse_json_logs(capsys.readouterr().out)
    auth_logs = [r for r in logs if r.get("event") == "collect_x_query_auth_error"]
    assert len(auth_logs) == 1
    assert auth_logs[0]["status_code"] == status_code
    assert auth_logs[0]["query_id"] == "live:official:acmecorp"
    # Never log the bearer token
    assert "secret-token" not in json.dumps(logs)


# ---------------------------------------------------------------------------
# 8. CLI entrypoint & static compliance checks
# ---------------------------------------------------------------------------


def test_cli_main_dry_run_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from services.collect_x.main import main

    event_path = _write_synthetic_event(tmp_path)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    code = main(["--event", str(event_path), "--dry-run"])
    assert code == 0


def test_cli_main_refuses_missing_token_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.collect_x.main import main

    event_path = _write_synthetic_event(tmp_path)
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    code = main(["--event", str(event_path)])
    assert code == 1
    logs = _parse_json_logs(capsys.readouterr().out)
    assert any(r.get("event") == "collect_x_refused" for r in logs)


def test_service_directory_has_no_forbidden_params_or_event_literals() -> None:
    assert SERVICE_DIR.is_dir()
    forbidden_patterns = [
        r"sarvam",
        r"expansions",
        r"user\.fields",
        r"/2/users",
        r"verdict|is_true|is_false|truth_score|credibility_score|risk_score",
        r"\bbots?\b|is_bot|bot_score|sockpuppet|troll|fake_account|inauthentic",
    ]
    for path in SERVICE_DIR.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        content = path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            match = re.search(pattern, content, flags=re.IGNORECASE)
            assert match is None, f"Forbidden pattern {pattern!r} matched in {path}: {match.group(0)!r}"


def test_dry_run_plan_uses_existing_since_id_cursor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path, official_posts=[], thread_ids=[], official_accounts=["AcmeCorp"], context_keywords=[]
    )
    fs = FakeFirestoreClient()
    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    set_cursor(
        "acme-widget-hub",
        "x",
        "live:official:acmecorp",
        "1000000000000000444",
        step_done=False,
        updated_at=now - timedelta(minutes=15),
        client=fs,
    )

    run_collect_x(
        event_path,
        dry_run=True,
        firestore_client=fs,
        now_fn=lambda: now,
        settings=CollectXSettings(X_BEARER_TOKEN=None),
    )

    plan_logs = [
        r for r in _parse_json_logs(capsys.readouterr().out) if r.get("event") == "collect_x_dry_run_plan"
    ]
    official_plan = [r for r in plan_logs[0]["requests"] if r["query_id"] == "live:official:acmecorp"][0]
    assert official_plan["params"]["since_id"] == "1000000000000000444"
    assert "start_time" not in official_plan["params"]


def test_http_429_retry_after_header_and_transport_error_retry(tmp_path: Path) -> None:
    from services.collect_x.main import CollectXSettings, run_collect_x

    event_path = _write_synthetic_event(
        tmp_path,
        official_posts=[],
        thread_ids=[],
        official_accounts=[],
        core_keywords=["acme"],
        context_keywords=[],
    )
    fs = FakeFirestoreClient()
    bq = RecordingBQClient()
    sleeps: list[float] = []
    calls = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("transient network drop", request=request)
        if calls == 2:
            return httpx.Response(429, headers={"retry-after": "7"}, text="slow down")
        return httpx.Response(
            200,
            json={"data": [_make_post("1000000000000000612")], "meta": {"newest_id": "1000000000000000612"}},
        )

    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    with _make_mock_client(_handler) as client:
        summary = run_collect_x(
            event_path,
            http_client=client,
            bq_client=bq,
            firestore_client=fs,
            now_fn=lambda: now,
            sleep_fn=sleeps.append,
            settings=CollectXSettings(X_BEARER_TOKEN="secret-token"),
        )

    assert summary.stop_reason == "completed"
    assert summary.reads_this_run == 1
    assert sleeps == [1.0, 7.0]
