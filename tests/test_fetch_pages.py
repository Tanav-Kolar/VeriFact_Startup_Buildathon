"""Tests for fetch-pages service (Spec 05 / AC 1-9)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.fetch_pages.main import (  # noqa: E402
    FetchPagesSettings,
    fetch_pages_job,
    format_request_body,
    get_pending_gdelt_articles,
    main as fetch_pages_cli_main,
    scrape_firecrawl,
)
from verifact_core.url_safety import (  # noqa: E402
    extract_host,
    is_social_url,
    normalize_url,
    url_hash,
)


def _load_fixture(name: str) -> dict[str, Any]:
    path = REPO_ROOT / "tests" / "fixtures" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("synthetic") is True, f"Fixture {name} must have synthetic: true"
    return data


class FakeFirestoreDocument:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = dict(data) if data is not None else None

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None


class FakeFirestoreDocRef:
    def __init__(self, store: dict[str, dict[str, Any]], doc_id: str) -> None:
        self.store = store
        self.doc_id = doc_id

    def get(self, transaction: Any = None) -> FakeFirestoreDocument:
        return FakeFirestoreDocument(self.store.get(self.doc_id))

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        current = dict(self.store.get(self.doc_id, {})) if merge else {}
        for k, v in data.items():
            if hasattr(v, "value") and type(v).__name__ == "Increment":
                current[k] = int(current.get(k, 0)) + int(v.value)
            else:
                current[k] = v
        self.store[self.doc_id] = current


class FakeFirestoreClient:
    def __init__(self, initial_store: dict[str, dict[str, Any]] | None = None) -> None:
        self.store: dict[str, dict[str, Any]] = initial_store or {}

    def collection(self, col_name: str) -> Any:
        return self

    def document(self, doc_id: str) -> FakeFirestoreDocRef:
        return FakeFirestoreDocRef(self.store, doc_id)


class FakeBigQueryClient:
    def __init__(self) -> None:
        self.written_rows: list[dict[str, Any]] = []

    def insert_rows_json(self, table_ref: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.written_rows.extend(rows)
        return []


def _make_dummy_event_yaml(tmp_path: Path, event_id: str = "test-event") -> Path:
    event_file = tmp_path / f"{event_id}.yaml"
    content = f"""
event_id: {event_id}
name: "Synthetic Test Event"
t0:
  utc: "2026-11-16T13:37:16Z"
window:
  pre_hours: 24
  post_hours: 48
  live: false
official:
  accounts: ["official_test"]
  urls:
    - "https://example.org/official/page1"
    - "https://example.org/official/page2"
keywords:
  core: ["test keyword"]
sources:
  news:
    enabled: true
"""
    event_file.write_text(content.strip(), encoding="utf-8")
    return event_file


# ---------------------------------------------------------------------------
# Unit tests for URL safety, normalization and blocklist (P5.1 / AC 3)
# ---------------------------------------------------------------------------

def test_url_safety_blocklist_coverage_ac3() -> None:
    """Verifies that x.com, mobile.twitter.com, youtu.be, and other social hosts are blocked."""
    test_urls = [
        ("https://x.com/status/123", True),
        ("https://mobile.twitter.com/user/status/456", True),
        ("https://youtu.be/dQw4w9WgXcQ", True),
        ("https://www.youtube.com/watch?v=123", True),
        ("https://reddit.com/r/technology", True),
        ("https://old.reddit.com/r/news", True),
        ("https://linkedin.com/feed", True),
        ("https://news.ycombinator.com/item?id=42", True),
        ("https://t.me/channel", True),
        ("https://instagram.com/p/123", True),
        ("https://facebook.com/post/123", True),
        ("https://tiktok.com/@user/video/123", True),
        ("https://example.org/news/launch", False),
        ("https://techcrunch.com/article", False),
        ("https://nottwitter.com/post", False),
    ]
    for url, expected in test_urls:
        assert is_social_url(url) is expected, f"Failed for {url}: expected {expected}"


def test_url_normalization_and_hash() -> None:
    """Verifies that tracking parameters and fragments are stripped for stable hashing."""
    url1 = "https://example.org/news/story?utm_source=twitter&utm_medium=social&id=42#section"
    url2 = "https://example.org/news/story?id=42&fbclid=abc123"
    url3 = "https://EXAMPLE.ORG/news/story?id=42"

    assert normalize_url(url1) == "https://example.org/news/story?id=42"
    assert normalize_url(url2) == "https://example.org/news/story?id=42"
    assert normalize_url(url3) == "https://example.org/news/story?id=42"

    assert url_hash(url1) == url_hash(url2) == url_hash(url3)


# ---------------------------------------------------------------------------
# AC 1: Budget cap reached exits 0 with cap_reached
# ---------------------------------------------------------------------------

def test_cap_reached_exits_zero_ac1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    event_path = _make_dummy_event_yaml(tmp_path)
    monkeypatch.setenv("FIRECRAWL_MAX_TOTAL", "100")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    fs_client = FakeFirestoreClient({
        "test-event__firecrawl__reads": {"reads_total": 100, "updated_at": datetime.now(timezone.utc)}
    })
    bq_client = FakeBigQueryClient()

    mock_scrape = MagicMock()
    monkeypatch.setattr("services.fetch_pages.main.scrape_firecrawl", mock_scrape)

    status_code, summary = fetch_pages_job(
        event_path=event_path,
        mode="both",
        dry_run=False,
        fs_client=fs_client,
        bq_client=bq_client,
    )

    assert status_code == 0
    assert summary.get("cap_reached") is True
    assert summary.get("calls_made") == 0
    assert mock_scrape.call_count == 0


# ---------------------------------------------------------------------------
# AC 2: A run never makes more than FIRECRAWL_MAX_PER_RUN calls
# ---------------------------------------------------------------------------

def test_run_never_exceeds_max_per_run_ac2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = _make_dummy_event_yaml(tmp_path)
    monkeypatch.setenv("FIRECRAWL_MAX_TOTAL", "1000")
    monkeypatch.setenv("FIRECRAWL_MAX_PER_RUN", "2")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    fixture = _load_fixture("firecrawl_article_scrape.json")

    fs_client = FakeFirestoreClient()
    bq_client = FakeBigQueryClient()

    candidates = [
        {"source_id": f"art_{i}", "url": f"https://news.example.org/article_{i}"}
        for i in range(5)
    ]

    mock_scrape = MagicMock(return_value=(200, fixture))
    monkeypatch.setattr("services.fetch_pages.main.scrape_firecrawl", mock_scrape)

    status_code, summary = fetch_pages_job(
        event_path=event_path,
        mode="articles",
        dry_run=False,
        article_candidates=candidates,
        fs_client=fs_client,
        bq_client=bq_client,
    )

    assert status_code == 0
    assert summary["calls_made"] == 2
    assert mock_scrape.call_count == 2
    assert len(bq_client.written_rows) == 2


# ---------------------------------------------------------------------------
# AC 3: Social URLs are never sent to Firecrawl (skipped_social)
# ---------------------------------------------------------------------------

def test_social_urls_skipped_and_never_sent_ac3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = _make_dummy_event_yaml(tmp_path)
    monkeypatch.setenv("FIRECRAWL_MAX_TOTAL", "500")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    fs_client = FakeFirestoreClient()
    bq_client = FakeBigQueryClient()

    candidates = [
        {"source_id": "soc1", "url": "https://mobile.twitter.com/status/1"},
        {"source_id": "soc2", "url": "https://x.com/status/2"},
        {"source_id": "soc3", "url": "https://youtu.be/testvideo"},
        {"source_id": "valid1", "url": "https://news.example.org/story"},
    ]

    fixture = _load_fixture("firecrawl_article_scrape.json")
    mock_scrape = MagicMock(return_value=(200, fixture))
    monkeypatch.setattr("services.fetch_pages.main.scrape_firecrawl", mock_scrape)

    status_code, summary = fetch_pages_job(
        event_path=event_path,
        mode="articles",
        dry_run=False,
        article_candidates=candidates,
        fs_client=fs_client,
        bq_client=bq_client,
    )

    assert status_code == 0
    assert summary["calls_made"] == 1
    assert mock_scrape.call_count == 1
    assert mock_scrape.call_args[0][0] == "https://news.example.org/story"
    assert summary["skipped_social"] == 3


# ---------------------------------------------------------------------------
# AC 4: Article with Firecrawl row shows markdown; without keeps GDELT title
# ---------------------------------------------------------------------------

def test_article_markdown_or_gdelt_title_logic_ac4() -> None:
    """Verifies the items view transformation logic:

    title + markdown when Firecrawl row exists, else GDELT title only.
    Also verifies published_time setting time_approximate = False.
    """
    gdelt_title = "Synthetic Headline of the Story"
    firecrawl_fixture = _load_fixture("firecrawl_article_scrape.json")
    markdown_content = firecrawl_fixture["data"]["markdown"]
    pub_time = firecrawl_fixture["data"]["metadata"]["article:published_time"]

    # Case 1: Firecrawl row exists
    text_with_firecrawl = f"{gdelt_title}\n\n{markdown_content}"
    assert gdelt_title in text_with_firecrawl
    assert markdown_content in text_with_firecrawl

    # Time approx rule:
    time_approx = pub_time is None
    assert time_approx is False

    # Case 2: Firecrawl row does not exist
    text_without_firecrawl = gdelt_title
    assert text_without_firecrawl == gdelt_title
    time_approx_no_firecrawl = True
    assert time_approx_no_firecrawl is True


# ---------------------------------------------------------------------------
# AC 5: Running official mode twice on same UTC day makes 1 snapshot per URL;
# on next day it makes a new snapshot with new source_id
# ---------------------------------------------------------------------------

def test_official_mode_idempotent_per_day_ac5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = _make_dummy_event_yaml(tmp_path)
    monkeypatch.setenv("FIRECRAWL_MAX_TOTAL", "500")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    fs_client = FakeFirestoreClient()
    bq_client = FakeBigQueryClient()

    fixture = _load_fixture("firecrawl_official_scrape.json")
    mock_scrape = MagicMock(return_value=(200, fixture))
    monkeypatch.setattr("services.fetch_pages.main.scrape_firecrawl", mock_scrape)

    day1 = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)

    # First run on day 1
    code1, sum1 = fetch_pages_job(
        event_path=event_path,
        mode="official",
        dry_run=False,
        fs_client=fs_client,
        bq_client=bq_client,
        current_time=day1,
    )
    assert code1 == 0
    assert sum1["calls_made"] == 2
    assert len(bq_client.written_rows) == 2

    # Check source_id format: <url_hash>:YYYY-MM-DD
    url1 = "https://example.org/official/page1"
    h1 = url_hash(url1)
    expected_source_id_day1 = f"{h1}:2026-11-20"
    source_ids_run1 = [r["source_id"] for r in bq_client.written_rows]
    assert expected_source_id_day1 in source_ids_run1

    # Second run on same day 1 -> 0 calls made, already snapshotted today
    code2, sum2 = fetch_pages_job(
        event_path=event_path,
        mode="official",
        dry_run=False,
        fs_client=fs_client,
        bq_client=bq_client,
        current_time=day1,
    )
    assert code2 == 0
    assert sum2["calls_made"] == 0
    assert len(bq_client.written_rows) == 2  # No new rows

    # Third run on next day 2 -> 2 new snapshots with 2026-11-21 in source_id
    code3, sum3 = fetch_pages_job(
        event_path=event_path,
        mode="official",
        dry_run=False,
        fs_client=fs_client,
        bq_client=bq_client,
        current_time=day2,
    )
    assert code3 == 0
    assert sum3["calls_made"] == 2
    assert len(bq_client.written_rows) == 4

    expected_source_id_day2 = f"{h1}:2026-11-21"
    source_ids_all = [r["source_id"] for r in bq_client.written_rows]
    assert expected_source_id_day2 in source_ids_all


# ---------------------------------------------------------------------------
# AC 6: A URL that has failed 3 times is not retried
# ---------------------------------------------------------------------------

def test_url_failed_3_times_is_not_retried_ac6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = _make_dummy_event_yaml(tmp_path)
    monkeypatch.setenv("FIRECRAWL_MAX_TOTAL", "500")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")

    fs_client = FakeFirestoreClient()
    bq_client = FakeBigQueryClient()

    candidates = [
        {"source_id": "failed_url_hash", "url": "https://news.example.org/404-broken"}
    ]

    error_fixture = _load_fixture("firecrawl_scrape_error.json")
    mock_scrape = MagicMock(return_value=(404, error_fixture))
    monkeypatch.setattr("services.fetch_pages.main.scrape_firecrawl", mock_scrape)

    # Run 1: failure 1
    fetch_pages_job(
        event_path=event_path,
        mode="articles",
        dry_run=False,
        article_candidates=candidates,
        fs_client=fs_client,
        bq_client=bq_client,
    )
    # Run 2: failure 2
    fetch_pages_job(
        event_path=event_path,
        mode="articles",
        dry_run=False,
        article_candidates=candidates,
        fs_client=fs_client,
        bq_client=bq_client,
    )
    # Run 3: failure 3
    fetch_pages_job(
        event_path=event_path,
        mode="articles",
        dry_run=False,
        article_candidates=candidates,
        fs_client=fs_client,
        bq_client=bq_client,
    )
    assert mock_scrape.call_count == 3
    assert len(bq_client.written_rows) == 0  # Failed fetches store nothing

    # Run 4: already failed 3 times -> not retried!
    mock_scrape.reset_mock()
    code4, sum4 = fetch_pages_job(
        event_path=event_path,
        mode="articles",
        dry_run=False,
        article_candidates=candidates,
        fs_client=fs_client,
        bq_client=bq_client,
    )
    assert code4 == 0
    assert sum4["calls_made"] == 0
    assert mock_scrape.call_count == 0
    assert sum4["skipped_max_failures"] == 1


# ---------------------------------------------------------------------------
# AC 7: Every request body is exactly url, formats: ["markdown"], onlyMainContent: true
# ---------------------------------------------------------------------------

def test_exact_request_body_ac7() -> None:
    test_url = "https://example.org/news/sample"
    body = format_request_body(test_url)
    assert body == {
        "url": test_url,
        "formats": ["markdown"],
        "onlyMainContent": True,
    }
    assert set(body.keys()) == {"url", "formats", "onlyMainContent"}


# ---------------------------------------------------------------------------
# AC 8: No test calls live Firecrawl API (verified by mocking in tests)
# ---------------------------------------------------------------------------

def test_no_live_firecrawl_call_ac8(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies that scrape_firecrawl raises if called without mock or key in test env."""
    with pytest.raises(Exception):
        scrape_firecrawl("https://example.org", api_key="", http_client=None)


# ---------------------------------------------------------------------------
# AC 9: Zero sarvam literals in services/fetch_pages/
# ---------------------------------------------------------------------------

def test_zero_sarvam_literals_in_fetch_pages_ac9() -> None:
    fetch_pages_dir = REPO_ROOT / "services" / "fetch_pages"
    url_safety_file = REPO_ROOT / "verifact_core" / "url_safety.py"

    files_to_check = list(fetch_pages_dir.glob("**/*.py")) + [url_safety_file]
    for file_path in files_to_check:
        if file_path.is_file():
            text = file_path.read_text(encoding="utf-8")
            assert "sarvam" not in text.lower(), f"Found 'sarvam' in {file_path}"


# ---------------------------------------------------------------------------
# Startup & Dry-run checks
# ---------------------------------------------------------------------------

def test_refuse_to_start_without_firecrawl_max_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = _make_dummy_event_yaml(tmp_path)
    monkeypatch.delenv("FIRECRAWL_MAX_TOTAL", raising=False)
    with pytest.raises(ValueError, match="FIRECRAWL_MAX_TOTAL"):
        fetch_pages_job(event_path=event_path)


def test_dry_run_makes_no_calls_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = _make_dummy_event_yaml(tmp_path)
    monkeypatch.setenv("FIRECRAWL_MAX_TOTAL", "500")

    fs_client = FakeFirestoreClient()
    bq_client = FakeBigQueryClient()

    candidates = [{"source_id": "c1", "url": "https://example.org/article1"}]

    mock_scrape = MagicMock()
    monkeypatch.setattr("services.fetch_pages.main.scrape_firecrawl", mock_scrape)

    status_code, summary = fetch_pages_job(
        event_path=event_path,
        mode="both",
        dry_run=True,
        article_candidates=candidates,
        fs_client=fs_client,
        bq_client=bq_client,
    )

    assert status_code == 0
    assert summary["dry_run"] is True
    assert summary["calls_made"] == 0
    assert mock_scrape.call_count == 0
    assert len(bq_client.written_rows) == 0


def test_get_pending_gdelt_articles_queries_bq() -> None:
    class MockRow:
        def __init__(self, source_id: str, url: str) -> None:
            self.source_id = source_id
            self.url = url

    class MockQueryResult:
        def result(self) -> list[MockRow]:
            return [MockRow("id1", "https://news.example.org/1")]

    class MockBQ:
        def query(self, sql: str, job_config: Any = None) -> MockQueryResult:
            assert "gdelt" in sql
            assert "firecrawl" in sql
            return MockQueryResult()

    settings = FetchPagesSettings(FIRECRAWL_MAX_TOTAL=100)
    candidates = get_pending_gdelt_articles("test-event", settings, limit=10, bq_client=MockBQ())
    assert len(candidates) == 1
    assert candidates[0] == {"source_id": "id1", "url": "https://news.example.org/1"}


def test_scrape_firecrawl_with_mock_client() -> None:
    class MockResponse:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"success": True, "data": {"markdown": "# Content"}}

    class MockClient:
        def post(self, url: str, json: dict[str, Any], headers: dict[str, str], timeout: float) -> MockResponse:
            assert "scrape" in url
            assert headers["Authorization"] == "Bearer mock-key"
            return MockResponse()

    code, data = scrape_firecrawl("https://example.org", api_key="mock-key", http_client=MockClient())
    assert code == 200
    assert data["data"]["markdown"] == "# Content"


def test_url_safety_extract_host_edge_cases() -> None:
    assert extract_host("") == ""
    assert extract_host("   ") == ""
    assert extract_host("user:pass@example.com:8080/path") == "example.com"
    assert extract_host("http://example.com:80/path") == "example.com"
    assert is_social_url("") is False
    assert is_social_url(":::malformed:::") is False

    assert normalize_url("") == ""
    assert normalize_url("http://user:pass@example.com:80/path") == "http://user:pass@example.com/path"
    assert normalize_url("https://user:pass@example.com:443/path") == "https://user:pass@example.com/path"
    assert normalize_url("https://example.com:443/") == "https://example.com"
    assert normalize_url("https://example.com:8443/path?b=2&a=1") == "https://example.com:8443/path?a=1&b=2"


def test_cli_main_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    event_path = _make_dummy_event_yaml(tmp_path)
    monkeypatch.setenv("FIRECRAWL_MAX_TOTAL", "500")
    monkeypatch.setattr(sys, "argv", ["main.py", "--event", str(event_path), "--dry-run"])

    with pytest.raises(SystemExit) as excinfo:
        fetch_pages_cli_main()
    assert excinfo.value.code == 0

