"""Tests for the minimal read-only FastAPI api service."""

import json
from pathlib import Path
import sys
import pytest

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from services.api.main import Settings, create_app  # noqa: E402


@pytest.fixture
def synthetic_events_dir(tmp_path: Path) -> Path:
    """Creates a temporary events directory with a synthetic event YAML."""
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    event_yaml_content = """
event_id: test-synthetic-launch
name: "Test Synthetic Event Launch"
t0:
  utc: "2026-11-16T12:00:00Z"
  source_url: "https://example.com/post/100"
window:
  pre_hours: 24
  post_hours: 48
  live: false
official:
  accounts: ["TestAcct"]
  posts: ["100"]
  urls: ["https://example.com/event"]
  spokespeople: ["Test Speaker"]
sources:
  x:
    enabled: true
    max_posts: 500
  hn:
    enabled: true
  youtube:
    enabled: false
  news:
    enabled: true
turning_points:
  detect_official_response: true
  manual:
    - id: tp-clarification
      label: "Official Statement Clarification"
      at_utc: null
      confirmed: false
      form: null
seed_storylines:
  - id: official-announcement
    name: "Official announcement details"
    seeds: []
  - id: community-discussion
    name: "Community technical discussion"
    seeds: []
claims_to_check:
  - id: spec-claim-1
    text: "Claimed benchmark throughput"
    self_reported: true
"""
    event_file = events_dir / "test-synthetic-launch.yaml"
    event_file.write_text(event_yaml_content.strip(), encoding="utf-8")
    return events_dir


@pytest.fixture
def client(synthetic_events_dir: Path) -> TestClient:
    """Test client configured with synthetic events directory."""
    settings = Settings(
        events_dir=str(synthetic_events_dir),
        graph_backend="frames",
        cors_origins="http://localhost:5173",
        sse_heartbeat_seconds=0.1,
    )
    app = create_app(settings)
    return TestClient(app)


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "graph_backend": "frames"}


def test_list_events(client: TestClient) -> None:
    response = client.get("/events")
    assert response.status_code == 200
    events = response.json()
    assert isinstance(events, list)
    assert len(events) == 1
    assert events[0]["event_id"] == "test-synthetic-launch"
    assert events[0]["name"] == "Test Synthetic Event Launch"
    assert events[0]["t0_utc"] == "2026-11-16T12:00:00Z"


def test_get_event_header(client: TestClient) -> None:
    response = client.get("/events/test-synthetic-launch")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Test Synthetic Event Launch"
    assert data["t0_utc"] == "2026-11-16T12:00:00Z"
    assert "test-synthetic-launch.yaml" in data["config_file"]
    assert set(data["sources"]) == {"x", "hn", "news"}
    assert len(data["turning_points"]) == 1
    assert data["turning_points"][0]["id"] == "tp-clarification"
    assert data["turning_points"][0]["confirmed"] is False
    assert isinstance(data["now_hour"], int)


def test_get_event_not_found(client: TestClient) -> None:
    response = client.get("/events/nonexistent-event")
    assert response.status_code == 404
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == "not_found"


def test_get_frames(client: TestClient) -> None:
    response = client.get("/events/test-synthetic-launch/frames")
    assert response.status_code == 200
    assert response.json() == []


def test_get_storylines(client: TestClient) -> None:
    response = client.get("/events/test-synthetic-launch/storylines")
    assert response.status_code == 200
    storylines = response.json()
    assert isinstance(storylines, list)
    # 2 seed storylines + 1 unassigned
    assert len(storylines) == 3

    seed_ids = [s["id"] for s in storylines]
    assert "official-announcement" in seed_ids
    assert "community-discussion" in seed_ids
    assert "unassigned" in seed_ids

    for s in storylines:
        assert s["count"] == 0
        assert s["first_seen"] is None
        assert s["platforms_reached"] == []


def test_get_single_storyline(client: TestClient) -> None:
    response = client.get("/events/test-synthetic-launch/storylines/official-announcement")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "official-announcement"
    assert data["name"] == "Official announcement details"
    assert data["count"] == 0
    assert data["volume_by_hour"] == []
    assert data["jumps"] == []


def test_get_single_storyline_not_found(client: TestClient) -> None:
    response = client.get("/events/test-synthetic-launch/storylines/unknown-storyline")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"


def test_get_claim_card(client: TestClient) -> None:
    response = client.get("/events/test-synthetic-launch/claims/spec-claim-1")
    assert response.status_code == 200
    data = response.json()
    assert data["claim_id"] == "spec-claim-1"
    assert data["text"] == "Claimed benchmark throughput"
    assert data["status"] == "unverified"
    assert data["counts"] == {"supporting": 0, "contradicting": 0, "neutral": 0}
    assert data["self_reported"] is True
    assert data["self_reported_note"] == "Company statement, not independently confirmed"
    assert data["evidence"] == []
    assert data["deep_link"] == "/e/test-synthetic-launch?claim=spec-claim-1"


def test_get_before_after(client: TestClient) -> None:
    response = client.get("/events/test-synthetic-launch/before-after")
    assert response.status_code == 200
    data = response.json()
    assert data["note"] == "Descriptive comparison, not a causal estimate."
    assert data["confirmed"] is False
    assert data["form"] is None
    assert data["turning_point"]["id"] == "tp-clarification"
    assert isinstance(data["storylines"], list)
    assert len(data["storylines"]) == 2
    for s in data["storylines"]:
        assert s["volume_before_per_h"] == 0.0
        assert s["volume_after_per_h"] == 0.0
        assert s["addressed"] is False


def test_get_before_after_specific_turning_point(client: TestClient) -> None:
    response = client.get(
        "/events/test-synthetic-launch/before-after?turning_point=tp-clarification"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["turning_point"]["id"] == "tp-clarification"
    assert data["confirmed"] is False


def test_get_before_after_unknown_turning_point(client: TestClient) -> None:
    response = client.get(
        "/events/test-synthetic-launch/before-after?turning_point=nonexistent-tp"
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"


def test_get_trust(client: TestClient) -> None:
    response = client.get("/events/test-synthetic-launch/trust")
    assert response.status_code == 200
    data = response.json()
    assert data["gold"] is None
    assert data["unassigned_count"] == 0
    assert data["backfilled_share"] is None
    coverage = data["coverage"]
    assert coverage["x"] == {"count": 0, "cap": 500}
    assert coverage["hn"] == {"count": 0}
    assert coverage["news"] == {"count": 0}
    assert "youtube" not in coverage  # disabled in synthetic config


def test_live_stream_sse(client: TestClient) -> None:
    with client.stream("GET", "/events/test-synthetic-launch/live") as response:
        assert response.status_code == 200
        lines = []
        for line in response.iter_lines():
            if line:
                lines.append(line)
            if len(lines) >= 4:
                break

        # Expect event: status and data: {"live": false}
        assert any("event: status" in line for line in lines)
        status_line = next(line for line in lines if line.startswith("data: ") and "live" in line)
        data = json.loads(status_line.replace("data: ", ""))
        assert data["live"] is False
