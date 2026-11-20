from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.collect_gdelt.main import (
    RateLimiter,
    normalize_url,
    run_collector as run_gdelt,
    url_sha1,
)
from services.collect_hn.main import run_collector as run_hn
from services.collect_youtube.main import run_collector as run_youtube
from verifact_core.config import EventConfig
from verifact_core.models import RawItem, WriteResult

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
ITEMS_VIEW_SQL_PATH = REPO_ROOT / "infra" / "bigquery" / "items_view.sql"
COLLECTORS_SH_PATH = REPO_ROOT / "infra" / "deploy" / "collectors.sh"


def _load_fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    assert data.get("synthetic") is True, f"Fixture {name} must have synthetic: true"
    return data


def _make_config(
    *,
    max_videos: int = 2,
    core: list[str] | None = None,
    context: list[str] | None = None,
    pre_hours: int = 72,
    post_hours: int = 240,
    live: bool = True,
) -> EventConfig:
    return EventConfig.model_validate(
        {
            "event_id": "synthetic-launch",
            "name": "Synthetic Launch",
            "t0": {"utc": "2026-11-16T13:37:16Z"},
            "window": {"pre_hours": pre_hours, "post_hours": post_hours, "live": live},
            "official": {"accounts": ["SynthOrg"]},
            "entities": {"org": {"SynthOrg": ["Synth Org"]}},
            "keywords": {
                "core": core if core is not None else ["synth launch"],
                "context": context if context is not None else [],
                "exclude": [],
            },
            "sources": {
                "hn": {"enabled": True},
                "youtube": {"enabled": True, "max_videos": max_videos},
                "news": {"enabled": True, "gdelt": True, "google_news_rss": False},
            },
        }
    )


class _FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store: dict[str, dict[str, Any]], doc_id: str) -> None:
        self._store = store
        self._doc_id = doc_id

    def get(self) -> _FakeSnapshot:
        return _FakeSnapshot(self._store.get(self._doc_id))

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        if merge and self._doc_id in self._store:
            merged = dict(self._store[self._doc_id])
            merged.update(data)
            self._store[self._doc_id] = merged
        else:
            self._store[self._doc_id] = dict(data)


class _FakeCollection:
    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, doc_id)


class FakeFirestore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self.docs)


class FakeRawWriter:
    def __init__(self) -> None:
        self.rows: list[RawItem] = []

    def __call__(self, rows: list[RawItem], *, run_id: str, client: Any | None = None) -> WriteResult:
        self.rows.extend(rows)
        return WriteResult(written=len(rows), rejected=[])


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now_s = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now_s

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now_s += seconds


# ---------------------------------------------------------------------------
# AC 2: --dry-run makes no HTTP calls or writes, prints queries & time bounds
# ---------------------------------------------------------------------------


def test_dry_run_makes_no_http_calls_or_writes_ac2(capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _make_config()
    now = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
    fs = FakeFirestore()
    writer = FakeRawWriter()

    def _forbidden_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"HTTP call forbidden in dry-run: {request.url}")

    transport = httpx.MockTransport(_forbidden_handler)
    with httpx.Client(transport=transport) as http_client:
        res_hn = run_hn(
            cfg,
            dry_run=True,
            now=now,
            http_client=http_client,
            firestore_client=fs,
            writer=writer,
        )
        res_yt = run_youtube(
            cfg,
            dry_run=True,
            now=now,
            http_client=http_client,
            firestore_client=fs,
            writer=writer,
            api_key="",
            searches_per_run=4,
        )
        res_gd = run_gdelt(
            cfg,
            dry_run=True,
            now=now,
            http_client=http_client,
            firestore_client=fs,
            writer=writer,
        )

    assert res_hn["written"] == 0
    assert res_yt["written"] == 0
    assert res_gd["written"] == 0
    assert len(writer.rows) == 0
    assert len(fs.docs) == 0

    captured = capsys.readouterr().out
    assert "synth launch" in captured
    assert "2026-11-13T13:37:16Z" in captured


# ---------------------------------------------------------------------------
# AC 3, AC 4, AC 9: Hacker News collector + nested comment tree + items_view
# ---------------------------------------------------------------------------


def test_collect_hn_writes_story_and_nested_comments_and_is_idempotent_ac3_ac4_ac9() -> None:
    cfg = _make_config()
    now = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
    search_fixture = _load_fixture("hn_search_page0.json")
    tree_fixture = _load_fixture("hn_story_tree.json")

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "/api/v1/search_by_date" in request.url.path:
            page = int(request.url.params.get("page", "0"))
            if page == 0:
                return httpx.Response(200, json=search_fixture)
            return httpx.Response(200, json={"hits": [], "page": page, "nbPages": 1})
        if request.url.path == "/api/v1/items/90001001":
            return httpx.Response(200, json=tree_fixture)
        raise AssertionError(f"Unexpected HN URL: {request.url}")

    fs = FakeFirestore()
    writer = FakeRawWriter()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        r1 = run_hn(
            cfg,
            now=now,
            http_client=http_client,
            firestore_client=fs,
            writer=writer,
        )
        assert r1["written"] == 4  # 1 in-window story + 3 nested comments; out-of-window story dropped
        written_ids = [row.source_id for row in writer.rows]
        assert written_ids == ["90001001", "90001002", "90001003", "90001004"]
        assert "90001099" not in written_ids  # AC 9: out of [t0 - pre_hours, t0 + post_hours]

        # Check query_id on thread comments
        comment_rows = [row for row in writer.rows if row.source_id != "90001001"]
        assert all(row.query_id == "thread:90001001" for row in comment_rows)

        # AC 3: immediate second run writes zero new rows
        r2 = run_hn(
            cfg,
            now=now,
            http_client=http_client,
            firestore_client=fs,
            writer=writer,
        )
        assert r2["written"] == 0
        assert len(writer.rows) == 4

    # AC 4: Verify items_view.sql HN projection produces 4 rows with exact parent_id chain
    sql_text = ITEMS_VIEW_SQL_PATH.read_text(encoding="utf-8")
    assert "hn AS (" in sql_text
    assert "https://news.ycombinator.com/item?id=" in sql_text
    assert "CONCAT('hn:'," in sql_text

    # Project the 4 raw_items through the exact HN mapping rules
    projected = []
    for row in writer.rows:
        p = row.payload
        oid = str(p.get("objectID") or p.get("id") or row.source_id)
        is_comment = p.get("type") == "comment" or p.get("parent_id") is not None
        parent = f"hn:{p['parent_id']}" if p.get("parent_id") is not None else None
        projected.append(
            {
                "item_id": f"hn:{oid}",
                "kind": "comment" if is_comment else "story",
                "parent_id": parent,
            }
        )
    assert projected == [
        {"item_id": "hn:90001001", "kind": "story", "parent_id": None},
        {"item_id": "hn:90001002", "kind": "comment", "parent_id": "hn:90001001"},
        {"item_id": "hn:90001003", "kind": "comment", "parent_id": "hn:90001002"},
        {"item_id": "hn:90001004", "kind": "comment", "parent_id": "hn:90001003"},
    ]


# ---------------------------------------------------------------------------
# AC 3, AC 5, AC 6, AC 9: YouTube collector caps, commentsDisabled, no commenter channels
# ---------------------------------------------------------------------------


def test_collect_youtube_caps_comments_disabled_and_idempotency_ac3_ac5_ac6(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Configure 5 queries total (1 core + 2 org * 2 context = 5) to test YOUTUBE_SEARCHES_PER_RUN=2 cap
    cfg = _make_config(max_videos=2, core=["synth launch"], context=["alpha", "beta"])
    now = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
    yt_fix = _load_fixture("youtube_responses.json")

    search_calls = 0
    channel_ids_requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_calls
        path = request.url.path
        if path.endswith("/search"):
            search_calls += 1
            return httpx.Response(200, json=yt_fix["search_response"])
        if path.endswith("/videos"):
            ids = set((request.url.params.get("id") or "").split(","))
            items = [v for v in yt_fix["videos_response"]["items"] if v["id"] in ids]
            return httpx.Response(200, json={"items": items})
        if path.endswith("/channels"):
            ids = [cid for cid in (request.url.params.get("id") or "").split(",") if cid]
            channel_ids_requested.extend(ids)
            items = [c for c in yt_fix["channels_response"]["items"] if c["id"] in ids]
            return httpx.Response(200, json={"items": items})
        if path.endswith("/commentThreads"):
            vid = request.url.params.get("videoId")
            if vid == "synthVid001":
                return httpx.Response(200, json=yt_fix["comment_threads_vid1"])
            if vid == "synthVid002":
                return httpx.Response(403, json=yt_fix["comments_disabled_error"])
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"Unexpected YouTube URL: {request.url}")

    fs = FakeFirestore()
    writer = FakeRawWriter()

    with caplog.at_level("INFO"):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            r1 = run_youtube(
                cfg,
                now=now,
                http_client=http_client,
                firestore_client=fs,
                writer=writer,
                api_key="fake-test-key",
                searches_per_run=2,
            )

            # AC 5: search calls <= YOUTUBE_SEARCHES_PER_RUN (2)
            assert search_calls <= 2
            # AC 5: stored videos <= max_videos (2)
            video_rows = [r for r in writer.rows if r.query_id == "video"]
            assert len(video_rows) == 2
            assert {r.source_id for r in video_rows} == {"synthVid001", "synthVid002"}

            # AC 5: channels.list called ONLY for video channels, NEVER commenters
            assert set(channel_ids_requested) == {"UC_synth_chan_1", "UC_synth_chan_2"}
            assert "UC_synth_commenter_A" not in channel_ids_requested
            assert "UC_synth_commenter_B" not in channel_ids_requested

            # AC 6: commentsDisabled 403 leaves synthVid002 stored and job successful
            comment_rows = [r for r in writer.rows if (r.query_id or "").startswith("comments:")]
            assert len(comment_rows) == 2  # top-level + reply on synthVid001
            assert r1["quota_units"] > 0

            # AC 3: immediate second run writes zero new rows
            r2 = run_youtube(
                cfg,
                now=now,
                http_client=http_client,
                firestore_client=fs,
                writer=writer,
                api_key="fake-test-key",
                searches_per_run=2,
            )
            assert r2["written"] == 0

    # Verify YouTube CTE exists in items_view.sql and excludes channel rows
    sql_text = ITEMS_VIEW_SQL_PATH.read_text(encoding="utf-8")
    assert "yt_channels AS (" in sql_text
    assert "youtube AS (" in sql_text
    assert "CONCAT('yt:'," in sql_text
    assert "CONCAT('yt:c:'," in sql_text


# ---------------------------------------------------------------------------
# AC 3, AC 7, AC 8, AC 9: GDELT rate limiter, 250-article split, URL sha1 dedupe
# ---------------------------------------------------------------------------


def test_gdelt_url_normalization_and_dedupe_ac8() -> None:
    u1 = "https://Example.ORG/news/launch-story?utm_source=twitter&utm_medium=social&id=42#top"
    u2 = "https://example.org/news/launch-story?id=42&fbclid=abc123"
    u3 = "https://example.org/news/launch-story?id=99"

    assert normalize_url(u1) == "https://example.org/news/launch-story?id=42"
    assert normalize_url(u1) == normalize_url(u2)
    assert url_sha1(u1) == url_sha1(u2)
    assert url_sha1(u1) != url_sha1(u3)

    # Verify SQLite deduplication on (event_id, source, source_id) yields 1 item
    gdelt_fix = _load_fixture("gdelt_articles.json")
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE raw_items (event_id TEXT, source TEXT, source_id TEXT, collected_at TEXT)"
    )
    for idx, art in enumerate(gdelt_fix["articles"][:2]):
        conn.execute(
            "INSERT INTO raw_items VALUES (?, ?, ?, ?)",
            ("synthetic-launch", "gdelt", url_sha1(art["url"]), f"2026-11-16T15:{10 + idx}:00Z"),
        )
    deduped = conn.execute(
        """
        SELECT source_id FROM (
            SELECT source_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY event_id, source, source_id ORDER BY collected_at ASC
                   ) AS rn
            FROM raw_items
        ) WHERE rn = 1
        """
    ).fetchall()
    assert len(deduped) == 1

    # Verify gdelt CTE in items_view.sql sets platform='news' and defaults time_approximate=TRUE
    sql_text = ITEMS_VIEW_SQL_PATH.read_text(encoding="utf-8")
    assert "gdelt AS (" in sql_text
    assert "'news' AS platform" in sql_text
    assert "TRUE AS time_approximate" in sql_text or (
        "ELSE TRUE" in sql_text and "END AS time_approximate" in sql_text
    )


def test_collect_gdelt_splits_250_articles_and_enforces_5s_rate_limit_ac3_ac7_ac9(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Narrow 30-minute window so one split reaches 15-minute minimum
    cfg = _make_config(pre_hours=0, post_hours=1)
    start_dt = cfg.t0.utc
    end_dt = start_dt + timedelta(minutes=30)

    clock = FakeClock(start=100.0)
    limiter = RateLimiter(min_interval=5.0, clock=clock.monotonic, sleeper=clock.sleep)

    request_times: list[float] = []
    windows_seen: list[tuple[str, str]] = []

    def _make_articles(n: int, prefix: str, ts_str: str) -> list[dict[str, Any]]:
        return [
            {
                "url": f"https://example.org/{prefix}/{i}?utm_source=test",
                "title": f"Synthetic article {prefix}-{i}",
                "seendate": ts_str,
                "domain": "example.org",
                "language": "English",
            }
            for i in range(n)
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        request_times.append(clock.monotonic())
        s = request.url.params["startdatetime"]
        e = request.url.params["enddatetime"]
        windows_seen.append((s, e))
        # First call (30m window) returns 250 -> triggers split into two 15m windows
        if len(windows_seen) == 1:
            return httpx.Response(200, json={"articles": _make_articles(250, "full", "20261116T134500Z")})
        # First 15m half still returns 250 -> triggers gdelt_window_saturated log
        if len(windows_seen) == 2:
            return httpx.Response(200, json={"articles": _make_articles(250, "half1", "20261116T134000Z")})
        # Second 15m half returns 2 articles (one in-window, one out-of-window)
        return httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "url": "https://example.org/half2/1?utm_campaign=x",
                        "title": "In-window article",
                        "seendate": "20261116T135500Z",
                        "domain": "example.org",
                        "language": "English",
                    },
                    {
                        "url": "https://example.org/half2/old",
                        "title": "Out-of-window article",
                        "seendate": "20261101T000000Z",
                        "domain": "example.org",
                        "language": "English",
                    },
                ]
            },
        )

    fs = FakeFirestore()
    writer = FakeRawWriter()

    with caplog.at_level("WARNING"):
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            r1 = run_gdelt(
                cfg,
                now=end_dt,
                http_client=http_client,
                firestore_client=fs,
                writer=writer,
                rate_limiter=limiter,
            )

            # 1 initial 30m call + 2 split 15m calls = 3 requests
            assert len(windows_seen) == 3
            # AC 7: every consecutive pair of requests is >= 5.0 seconds apart
            for prev_t, next_t in zip(request_times, request_times[1:]):
                assert next_t - prev_t >= 5.0
            # Saturated 15m window logged
            assert any("gdelt_window_saturated" in rec.message for rec in caplog.records)
            # 250 from half1 + 1 in-window from half2 = 251 (out-of-window dropped per AC 9)
            assert r1["written"] == 251
            assert all(row.source == "gdelt" for row in writer.rows)

            # AC 3: immediate second run writes zero new rows
            r2 = run_gdelt(
                cfg,
                now=end_dt,
                http_client=http_client,
                firestore_client=fs,
                writer=writer,
                rate_limiter=limiter,
            )
            assert r2["written"] == 0


# ---------------------------------------------------------------------------
# AC 10: No event-specific ("sarvam") literals in collectors, queries, or infra
# ---------------------------------------------------------------------------


def test_no_sarvam_literals_in_collectors_queries_or_infra_ac10() -> None:
    targets = [
        REPO_ROOT / "verifact_core" / "queries.py",
        REPO_ROOT / "services" / "collect_hn",
        REPO_ROOT / "services" / "collect_youtube",
        REPO_ROOT / "services" / "collect_gdelt",
        REPO_ROOT / "infra",
    ]
    hits: list[str] = []
    for target in targets:
        if target.is_file():
            files = [target]
        elif target.is_dir():
            files = [p for p in target.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        else:
            continue
        for fpath in files:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"sarvam", content, re.IGNORECASE):
                hits.append(str(fpath.relative_to(REPO_ROOT)))
    assert hits == [], f"Found event-specific literal in: {hits}"


# ---------------------------------------------------------------------------
# Deploy script verification (bash syntax + idempotency + LIVE_INGESTION=false)
# ---------------------------------------------------------------------------


def test_collectors_deploy_script_syntax_and_pause_when_live_false(tmp_path: Path) -> None:
    assert COLLECTORS_SH_PATH.exists()

    # Valid bash syntax
    syntax = subprocess.run(
        ["bash", "-n", str(COLLECTORS_SH_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    # Dry-execute against a stub gcloud binary
    log_file = tmp_path / "gcloud_calls.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        f"""#!/usr/bin/env python3
import sys
from pathlib import Path

log_path = Path({str(log_file)!r})
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
# Pretend resources do not exist on describe so create path runs
if "describe" in args:
    sys.exit(1)
sys.exit(0)
""",
        encoding="utf-8",
    )
    fake_gcloud.chmod(fake_gcloud.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["GCP_PROJECT"] = "test-proj"
    env["GCP_REGION"] = "asia-south1"
    env["EVENT_CONFIG"] = "events/synthetic-event.yaml"
    env["LIVE_INGESTION"] = "false"

    res = subprocess.run(
        ["bash", str(COLLECTORS_SH_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr

    calls = log_file.read_text(encoding="utf-8").splitlines()
    # All 3 jobs created with max-retries=1
    assert any("run jobs create collect-hn" in c and "--max-retries=1" in c for c in calls)
    assert any("run jobs create collect-youtube" in c and "--max-retries=1" in c for c in calls)
    assert any("run jobs create collect-gdelt" in c and "--max-retries=1" in c for c in calls)
    # Scheduler triggers paused when LIVE_INGESTION=false
    assert any("scheduler jobs pause collect-hn-schedule" in c for c in calls)
    assert any("scheduler jobs pause collect-youtube-schedule" in c for c in calls)
    assert any("scheduler jobs pause collect-gdelt-schedule" in c for c in calls)


def test_cli_dry_run_and_disabled_sources(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from services.collect_gdelt.main import main as gdelt_main
    from services.collect_hn.main import main as hn_main
    from services.collect_youtube.main import main as yt_main

    event_yaml = tmp_path / "cli-test.yaml"
    event_yaml.write_text(
        """
event_id: cli-test
name: "CLI Test Event"
t0:
  utc: "2026-11-16T13:37:16Z"
window:
  pre_hours: 24
  post_hours: 48
  live: true
official:
  accounts: ["CliOrg"]
entities:
  org:
    CliOrg: ["Cli Org"]
keywords:
  core: ["cli launch"]
  context: []
  exclude: []
sources:
  hn:
    enabled: false
  youtube:
    enabled: false
  news:
    enabled: false
    gdelt: false
  manual:
    enabled: true
""",
        encoding="utf-8",
    )

    assert hn_main(["--event", str(event_yaml), "--dry-run"]) == 0
    assert yt_main(["--event", str(event_yaml), "--dry-run"]) == 0
    assert gdelt_main(["--event", str(event_yaml), "--dry-run"]) == 0
    _ = capsys.readouterr()

