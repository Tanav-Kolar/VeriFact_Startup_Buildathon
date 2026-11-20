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


# ---------------------------------------------------------------------------
# Phase C / Spec 08 Full Read-Only API Tests (Frames, Snapshot, BQ, Spanner)
# ---------------------------------------------------------------------------


@pytest.fixture
def rich_event_and_frames(tmp_path: Path) -> tuple[Path, Path]:
    """Creates synthetic event config plus synthetic frames and snapshot JSON files."""
    events_dir = tmp_path / "events"
    frames_dir = tmp_path / "frames"
    events_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    event_yaml = """
event_id: synth-launch-2026
name: "Synthetic Launch Event"
t0:
  utc: "2026-11-16T12:00:00Z"
  source_url: "https://example.com/post/100"
window:
  pre_hours: 2
  post_hours: 6
  live: false
official:
  accounts: ["OfficialAcct"]
  posts: ["100"]
  urls: ["https://example.com/official"]
  spokespeople: ["Pat Speaker"]
entities:
  org:
    acme: ["Acme"]
  product: {}
  related: []
keywords:
  core: ["SynthLaunch"]
  context: []
  exclude: []
languages: ["en"]
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
    - id: tp-response-1
      label: "Official blog clarification"
      at_utc: "2026-11-16T14:00:00Z"
      confirmed: true
      form: "blog post"
seed_storylines:
  - id: launch-claims
    name: "Launch benchmark claims"
    seeds: ["Throughput benchmark numbers"]
  - id: pricing-talk
    name: "Pricing and hardware discussion"
    seeds: ["Pricing tiers and hardware"]
claims_to_check:
  - id: claim-throughput
    text: "Platform delivers 10k tokens per second"
conflicts_of_interest:
  - person: "Alex Investor"
    relationship: "Partner at Acme Ventures (early backer)"
privacy:
  min_followers_to_name: 10000
"""
    (events_dir / "synth-launch-2026.yaml").write_text(event_yaml.strip(), encoding="utf-8")

    long_article_text = (
        "Independent benchmark testing confirmed that the new platform delivers 10k tokens per second "
        "under sustained synthetic load across multiple regions. "
        + ("Additional technical methodology details and hardware configurations were reviewed. " * 6)
    )
    assert len(long_article_text) > 350

    frames_payload = [
        {
            "synthetic": True,
            "hour": 0,
            "nodes_added": [
                {
                    "id": "x:100",
                    "type": "post",
                    "platform": "x",
                    "kind": "post",
                    "created_at": "2026-11-16T12:10:00Z",
                    "hour": 0,
                    "storyline_id": "launch-claims",
                    "reach": 50000,
                    "is_official": True,
                    "self_reported": True,
                    "conflict": None,
                    "lang": "en",
                    "label": "OfficialAcct",
                },
                {
                    "id": "x:101",
                    "type": "post",
                    "platform": "x",
                    "kind": "reply",
                    "created_at": "2026-11-16T12:20:00Z",
                    "hour": 0,
                    "storyline_id": "launch-claims",
                    "reach": 1200,
                    "is_official": False,
                    "self_reported": False,
                    "conflict": {
                        "person": "Alex Investor",
                        "relationship": "Partner at Acme Ventures (early backer)",
                    },
                    "lang": "en",
                    "label": "Account A1",
                },
            ],
            "edges_added": [
                {
                    "source": "x:101",
                    "target": "x:100",
                    "type": "replied_to",
                    "created_at": "2026-11-16T12:20:00Z",
                    "hour": 0,
                }
            ],
            "storyline_counts": {"launch-claims": 2, "pricing-talk": 0, "unassigned": 0},
        },
        {
            "synthetic": True,
            "hour": 1,
            "nodes_added": [
                {
                    "id": "hn:200",
                    "type": "post",
                    "platform": "hn",
                    "kind": "story",
                    "created_at": "2026-11-16T13:15:00Z",
                    "hour": 1,
                    "storyline_id": "launch-claims",
                    "reach": 800,
                    "is_official": False,
                    "self_reported": False,
                    "conflict": None,
                    "lang": "en",
                    "label": "Account A2",
                },
                {
                    "id": "x:102",
                    "type": "post",
                    "platform": "x",
                    "kind": "post",
                    "created_at": "2026-11-16T13:02:00Z",
                    "hour": 1,
                    "storyline_id": "pricing-talk",
                    "reach": 300,
                    "is_official": False,
                    "self_reported": False,
                    "conflict": None,
                    "lang": "en",
                    "label": "Account A3",
                },
                {
                    "id": "x:103",
                    "type": "post",
                    "platform": "x",
                    "kind": "post",
                    "created_at": "2026-11-16T13:04:00Z",
                    "hour": 1,
                    "storyline_id": "pricing-talk",
                    "reach": 250,
                    "is_official": False,
                    "self_reported": False,
                    "conflict": None,
                    "lang": "en",
                    "label": "Account A4",
                },
                {
                    "id": "x:104",
                    "type": "post",
                    "platform": "x",
                    "kind": "post",
                    "created_at": "2026-11-16T13:06:00Z",
                    "hour": 1,
                    "storyline_id": "pricing-talk",
                    "reach": 200,
                    "is_official": False,
                    "self_reported": False,
                    "conflict": None,
                    "lang": "en",
                    "label": "Account A5",
                },
            ],
            "edges_added": [
                {
                    "source": "hn:200",
                    "target": "x:100",
                    "type": "links_to",
                    "created_at": "2026-11-16T13:15:00Z",
                    "hour": 1,
                }
            ],
            "storyline_counts": {"launch-claims": 3, "pricing-talk": 3, "unassigned": 0},
        },
        {
            "synthetic": True,
            "hour": 2,
            "nodes_added": [
                {
                    "id": "news:300",
                    "type": "article",
                    "platform": "news",
                    "kind": "article",
                    "created_at": "2026-11-16T14:30:00Z",
                    "hour": 2,
                    "storyline_id": "launch-claims",
                    "reach": 15000,
                    "is_official": False,
                    "self_reported": False,
                    "conflict": None,
                    "lang": "en",
                    "label": "TechDaily",
                },
                {
                    "id": "x:105",
                    "type": "post",
                    "platform": "x",
                    "kind": "post",
                    "created_at": "2026-11-16T14:40:00Z",
                    "hour": 2,
                    "storyline_id": "launch-claims",
                    "reach": 45000,
                    "is_official": True,
                    "self_reported": True,
                    "conflict": None,
                    "lang": "en",
                    "label": "OfficialAcct",
                },
                {
                    "id": "x:106",
                    "type": "post",
                    "platform": "x",
                    "kind": "post",
                    "created_at": "2026-11-16T14:45:00Z",
                    "hour": 2,
                    "storyline_id": "unassigned",
                    "reach": 50,
                    "is_official": False,
                    "self_reported": False,
                    "conflict": None,
                    "lang": "en",
                    "label": "Account A6",
                },
            ],
            "edges_added": [],
            "storyline_counts": {"launch-claims": 5, "pricing-talk": 3, "unassigned": 1},
        },
    ]

    dup_text = "Check out the new synthetic hardware pricing breakdown table right now"
    snapshot_payload = {
        "synthetic": True,
        "items": [
            {
                "item_id": "x:100",
                "platform": "x",
                "kind": "post",
                "url": "https://example.com/post/100",
                "author_ref": "raw-secret-uid-100",
                "author_followers": 50000,
                "created_at": "2026-11-16T12:10:00Z",
                "collected_at": "2026-11-16T12:15:00Z",
                "text": "Our new platform delivers 10k tokens per second in internal benchmarks.",
                "lang": "en",
                "hours_from_t0": 0.17,
                "captured_by": "backfill",
                "reach": 50000,
            },
            {
                "item_id": "x:101",
                "platform": "x",
                "kind": "reply",
                "url": "https://example.com/post/101",
                "author_ref": "raw-secret-uid-101",
                "author_followers": 1200,
                "created_at": "2026-11-16T12:20:00Z",
                "collected_at": "2026-11-16T12:22:00Z",
                "text": "Alex Investor says the platform delivers 10k tokens per second easily.",
                "lang": "en",
                "parent_id": "x:100",
                "hours_from_t0": 0.33,
                "captured_by": "backfill",
                "reach": 1200,
            },
            {
                "item_id": "hn:200",
                "platform": "hn",
                "kind": "story",
                "url": "https://news.ycombinator.com/item?id=200",
                "author_ref": "hn_private_user_200",
                "author_followers": 0,
                "created_at": "2026-11-16T13:15:00Z",
                "collected_at": "2026-11-16T13:18:00Z",
                "text": "Our independent test saw much lower numbers than 10k tokens per second.",
                "lang": "en",
                "links": ["https://example.com/post/100"],
                "hours_from_t0": 1.25,
                "captured_by": "api",
                "reach": 800,
            },
            {
                "item_id": "x:102",
                "platform": "x",
                "kind": "post",
                "url": "https://example.com/post/102",
                "author_ref": "raw-secret-uid-102",
                "created_at": "2026-11-16T13:02:00Z",
                "collected_at": "2026-11-16T13:05:00Z",
                "text": dup_text,
                "lang": "en",
                "hours_from_t0": 1.03,
                "captured_by": "api",
                "reach": 300,
            },
            {
                "item_id": "x:103",
                "platform": "x",
                "kind": "post",
                "url": "https://example.com/post/103",
                "author_ref": "raw-secret-uid-103",
                "created_at": "2026-11-16T13:04:00Z",
                "collected_at": "2026-11-16T13:05:00Z",
                "text": dup_text + "!",
                "lang": "en",
                "hours_from_t0": 1.07,
                "captured_by": "api",
                "reach": 250,
            },
            {
                "item_id": "x:104",
                "platform": "x",
                "kind": "post",
                "url": "https://example.com/post/104",
                "author_ref": "raw-secret-uid-104",
                "created_at": "2026-11-16T13:06:00Z",
                "collected_at": "2026-11-16T13:08:00Z",
                "text": dup_text,
                "lang": "en",
                "hours_from_t0": 1.10,
                "captured_by": "api",
                "reach": 200,
            },
            {
                "item_id": "news:300",
                "platform": "news",
                "kind": "article",
                "url": "https://techdaily.example.com/synth-launch",
                "author_ref": "techdaily-staff-writer",
                "created_at": "2026-11-16T14:30:00Z",
                "collected_at": "2026-11-16T14:35:00Z",
                "text": long_article_text,
                "lang": "en",
                "hours_from_t0": 2.5,
                "captured_by": "api",
                "time_approximate": True,
                "reach": 15000,
            },
            {
                "item_id": "x:105",
                "platform": "x",
                "kind": "post",
                "url": "https://example.com/post/105",
                "author_ref": "raw-secret-uid-100",
                "author_followers": 50000,
                "created_at": "2026-11-16T14:40:00Z",
                "collected_at": "2026-11-16T14:41:00Z",
                "text": "Official clarification blog post published on throughput methodology.",
                "lang": "en",
                "hours_from_t0": 2.67,
                "captured_by": "api",
                "reach": 45000,
            },
            {
                "item_id": "x:106",
                "platform": "x",
                "kind": "post",
                "url": "https://example.com/post/106",
                "author_ref": "raw-secret-uid-106",
                "created_at": "2026-11-16T14:45:00Z",
                "collected_at": "2026-11-16T14:46:00Z",
                "text": "General brief mention without specific storyline.",
                "lang": "en",
                "hours_from_t0": 2.75,
                "captured_by": "api",
                "reach": 50,
            },
        ],
        "enrichments": [
            {
                "item_id": "x:100",
                "model_id": "gemini-2.5-flash-lite",
                "prompt_version": "v1",
                "lang": "en",
                "storyline_id": "launch-claims",
                "storyline_similarity": 0.91,
                "off_topic": False,
                "is_official": True,
                "conflict_person": None,
                "claims": [
                    {
                        "claim_id": "claim-throughput",
                        "stance": "supports",
                        "evidence_span": "platform delivers 10k tokens per second",
                        "votes": ["supports", "supports", "supports"],
                        "agreement": 1.0,
                    }
                ],
                "entities": ["acme"],
            },
            {
                "item_id": "x:101",
                "model_id": "gemini-2.5-flash-lite",
                "prompt_version": "v1",
                "lang": "en",
                "storyline_id": "launch-claims",
                "storyline_similarity": 0.85,
                "off_topic": False,
                "is_official": False,
                "conflict_person": "Alex Investor",
                "claims": [
                    {
                        "claim_id": "claim-throughput",
                        "stance": "supports",
                        "evidence_span": "platform delivers 10k tokens per second easily",
                        "votes": ["supports", "supports", "neutral"],
                        "agreement": 0.67,
                    },
                    {
                        "claim_id": "claim-throughput",
                        "stance": "supports",
                        "evidence_span": "FABRICATED SPAN NOT IN TEXT",
                        "votes": ["supports", "supports", "supports"],
                        "agreement": 1.0,
                    },
                ],
                "entities": ["acme"],
            },
            {
                "item_id": "hn:200",
                "model_id": "gemini-2.5-flash-lite",
                "prompt_version": "v1",
                "lang": "en",
                "storyline_id": "launch-claims",
                "storyline_similarity": 0.82,
                "off_topic": False,
                "is_official": False,
                "conflict_person": None,
                "claims": [
                    {
                        "claim_id": "claim-throughput",
                        "stance": "contradicts",
                        "evidence_span": "much lower numbers than 10k tokens per second",
                        "votes": ["contradicts", "contradicts", "contradicts"],
                        "agreement": 1.0,
                    }
                ],
                "entities": [],
            },
            {
                "item_id": "x:102",
                "model_id": "gemini-2.5-flash-lite",
                "prompt_version": "v1",
                "lang": "en",
                "storyline_id": "pricing-talk",
                "off_topic": False,
                "is_official": False,
                "conflict_person": None,
                "claims": [],
                "entities": [],
            },
            {
                "item_id": "x:103",
                "model_id": "gemini-2.5-flash-lite",
                "prompt_version": "v1",
                "lang": "en",
                "storyline_id": "pricing-talk",
                "off_topic": False,
                "is_official": False,
                "conflict_person": None,
                "claims": [],
                "entities": [],
            },
            {
                "item_id": "x:104",
                "model_id": "gemini-2.5-flash-lite",
                "prompt_version": "v1",
                "lang": "en",
                "storyline_id": "pricing-talk",
                "off_topic": False,
                "is_official": False,
                "conflict_person": None,
                "claims": [],
                "entities": [],
            },
            {
                "item_id": "news:300",
                "model_id": "gemini-2.5-flash-lite",
                "prompt_version": "v1",
                "lang": "en",
                "storyline_id": "launch-claims",
                "storyline_similarity": 0.89,
                "off_topic": False,
                "is_official": False,
                "conflict_person": None,
                "claims": [
                    {
                        "claim_id": "claim-throughput",
                        "stance": "supports",
                        "evidence_span": "platform delivers 10k tokens per second",
                        "votes": ["supports", "supports", "supports"],
                        "agreement": 1.0,
                    }
                ],
                "entities": ["acme"],
            },
            {
                "item_id": "x:105",
                "model_id": "gemini-2.5-flash-lite",
                "prompt_version": "v1",
                "lang": "en",
                "storyline_id": "launch-claims",
                "off_topic": False,
                "is_official": True,
                "conflict_person": None,
                "claims": [],
                "entities": ["acme"],
            },
            {
                "item_id": "x:106",
                "model_id": "gemini-2.5-flash-lite",
                "prompt_version": "v1",
                "lang": "en",
                "storyline_id": "unassigned",
                "off_topic": False,
                "is_official": False,
                "conflict_person": None,
                "claims": [],
                "entities": [],
            },
        ],
        "account_map": [
            {"platform": "x", "author_ref": "raw-secret-uid-101", "account_n": 1},
            {"platform": "hn", "author_ref": "hn_private_user_200", "account_n": 2},
            {"platform": "x", "author_ref": "raw-secret-uid-102", "account_n": 3},
            {"platform": "x", "author_ref": "raw-secret-uid-103", "account_n": 4},
            {"platform": "x", "author_ref": "raw-secret-uid-104", "account_n": 5},
            {"platform": "x", "author_ref": "raw-secret-uid-106", "account_n": 6},
        ],
        "gold": {
            "assignment_accuracy": 0.86,
            "stance_macro_f1": 0.82,
            "run_date": "2026-11-16T18:00:00Z",
        },
    }

    (frames_dir / "synth-launch-2026.frames.json").write_text(
        json.dumps(frames_payload), encoding="utf-8"
    )
    (frames_dir / "synth-launch-2026.snapshot.json").write_text(
        json.dumps(snapshot_payload), encoding="utf-8"
    )
    return events_dir, frames_dir


@pytest.fixture
def rich_client(rich_event_and_frames: tuple[Path, Path]) -> TestClient:
    events_dir, frames_dir = rich_event_and_frames
    settings = Settings(
        events_dir=str(events_dir),
        frames_dir=str(frames_dir),
        graph_backend="frames",
        cors_origins="http://localhost:5173",
        sse_heartbeat_seconds=0.1,
    )
    return TestClient(create_app(settings))


def test_frames_range_filtering_and_shape(rich_client: TestClient) -> None:
    all_frames = rich_client.get("/events/synth-launch-2026/frames").json()
    assert [f["hour"] for f in all_frames] == [0, 1, 2]
    assert set(all_frames[0].keys()) == {"hour", "nodes_added", "edges_added", "storyline_counts"}

    sub = rich_client.get("/events/synth-launch-2026/frames?from_hour=1&to_hour=1").json()
    assert [f["hour"] for f in sub] == [1]
    assert len(sub[0]["nodes_added"]) == 4
    assert len(sub[0]["edges_added"]) == 1


def test_storylines_and_at_hour_cutoff(rich_client: TestClient) -> None:
    # At hour 0, only x:100 and x:101 exist in launch-claims (nothing in pricing-talk or unassigned)
    r0 = rich_client.get("/events/synth-launch-2026/storylines?at_hour=0")
    assert r0.status_code == 200
    by_id_0 = {s["id"]: s for s in r0.json()}
    assert by_id_0["launch-claims"]["count"] == 2
    assert by_id_0["launch-claims"]["first_seen"] == {
        "item_id": "x:100",
        "platform": "x",
        "hour": 0,
    }
    assert by_id_0["launch-claims"]["platforms_reached"] == [{"platform": "x", "hour": 0}]
    assert by_id_0["pricing-talk"]["count"] == 0
    assert by_id_0["pricing-talk"]["first_seen"] is None
    assert by_id_0["unassigned"]["count"] == 0

    # At hour 2, all items are visible including HN & news platforms and unassigned
    r2 = rich_client.get("/events/synth-launch-2026/storylines?at_hour=2")
    assert r2.status_code == 200
    by_id_2 = {s["id"]: s for s in r2.json()}
    lc = by_id_2["launch-claims"]
    assert lc["count"] == 5
    assert lc["platforms_reached"] == [
        {"platform": "x", "hour": 0},
        {"platform": "hn", "hour": 1},
        {"platform": "news", "hour": 2},
    ]
    assert lc["stance_mix"] == {"supports": 3, "contradicts": 1, "neutral": 0}
    assert lc["agreement"] is not None and lc["agreement"] > 0.8
    assert len(lc["top_amplifiers"]) >= 1
    assert lc["top_amplifiers"][0]["label"] == "OfficialAcct"
    # Privacy: never leak author_ref anywhere
    assert "raw-secret-uid" not in r2.text
    assert by_id_2["unassigned"]["count"] == 1


def test_storyline_detail_volume_and_jumps_respect_at_hour(rich_client: TestClient) -> None:
    # At hour 0: no cross-platform jump yet
    d0 = rich_client.get("/events/synth-launch-2026/storylines/launch-claims?at_hour=0").json()
    assert d0["volume_by_hour"] == [{"hour": 0, "count": 2}]
    assert d0["jumps"] == []

    # At hour 1: HN -> X cross-platform jump appears
    d1 = rich_client.get("/events/synth-launch-2026/storylines/launch-claims?at_hour=1").json()
    assert d1["volume_by_hour"] == [{"hour": 0, "count": 2}, {"hour": 1, "count": 1}]
    assert d1["jumps"] == [{"from_item": "hn:200", "to_item": "x:100", "hour": 1}]


def test_item_detail_excerpt_limit_no_author_ref_and_p12_span_check(
    rich_client: TestClient,
) -> None:
    # Long article excerpt must be <= 300 chars (P5.5) and never expose author_ref
    resp = rich_client.get("/events/synth-launch-2026/items/news:300")
    assert resp.status_code == 200
    item = resp.json()
    assert len(item["excerpt"]) <= 300
    assert "author_ref" not in item
    assert "techdaily-staff-writer" not in resp.text
    assert item["created_at"] == "2026-11-16T14:30:00Z"
    assert item["collected_at"] == "2026-11-16T14:35:00Z"
    assert item["collection_lag_s"] == 300
    assert item["platform"] == "news"
    assert item["storyline_id"] == "launch-claims"
    assert "time-approximate" in item["tags"]
    assert item["claims"] == [
        {
            "claim_id": "claim-throughput",
            "stance": "supports",
            "span": "platform delivers 10k tokens per second",
        }
    ]

    # Item with a fabricated span (x:101) must drop the unverified span (P1.2)
    resp_101 = rich_client.get("/events/synth-launch-2026/items/x:101")
    assert resp_101.status_code == 200
    item_101 = resp_101.json()
    assert "author_ref" not in item_101
    assert "raw-secret-uid" not in resp_101.text
    assert len(item_101["claims"]) == 1
    assert item_101["claims"][0]["span"] == "platform delivers 10k tokens per second easily"


def test_claim_card_at_hour_self_reported_and_conflict_disclosure(
    rich_client: TestClient,
) -> None:
    # At hour 0: official post + investor-with-conflict support only -> supported + self_reported=True
    c0 = rich_client.get("/events/synth-launch-2026/claims/claim-throughput?at_hour=0").json()
    assert c0["status"] == "supported"
    assert c0["counts"] == {"supporting": 2, "contradicting": 0, "neutral": 0}
    assert c0["self_reported"] is True
    assert c0["self_reported_note"] == "Company statement, not independently confirmed"
    # Fabricated span from x:101 must be dropped (only 2 evidence entries, not 3)
    assert len(c0["evidence"]) == 2
    ev_by_id = {e["item_id"]: e for e in c0["evidence"]}
    assert ev_by_id["x:100"]["source_type"] == "company_statement"
    assert ev_by_id["x:101"]["source_type"] == "investor_with_conflict"
    assert ev_by_id["x:101"]["conflict"] == {
        "person": "Alex Investor",
        "relationship": "Partner at Acme Ventures (early backer)",
    }

    # At hour 1: HN contradicts -> mixed, still self_reported=True (no independent supporter yet)
    c1 = rich_client.get("/events/synth-launch-2026/claims/claim-throughput?at_hour=1").json()
    assert c1["status"] == "mixed"
    assert c1["counts"] == {"supporting": 2, "contradicting": 1, "neutral": 0}
    assert c1["self_reported"] is True

    # At hour 2: independent news article supports -> mixed, self_reported becomes False (P2.2)
    c2 = rich_client.get("/events/synth-launch-2026/claims/claim-throughput?at_hour=2").json()
    assert c2["status"] == "mixed"
    assert c2["counts"] == {"supporting": 3, "contradicting": 1, "neutral": 0}
    assert c2["self_reported"] is False
    assert c2["self_reported_note"] is None


def test_signals_neutral_wording_and_no_account_ids(rich_client: TestClient) -> None:
    resp = rich_client.get("/events/synth-launch-2026/signals")
    assert resp.status_code == 200
    signals = resp.json()
    assert len(signals) == 1
    sig = signals[0]
    assert set(sig.keys()) == {"storyline_id", "n_items", "window_minutes", "text"}
    assert sig["storyline_id"] == "pricing-talk"
    assert sig["n_items"] == 3
    assert sig["window_minutes"] == 4
    assert sig["text"] == (
        "3 near-identical posts within 4 minutes. "
        "This can reflect organic copy-paste or coordination; no account is labelled."
    )
    assert "Account A" not in resp.text
    assert "raw-secret-uid" not in resp.text


def test_before_after_real_volumes_and_disclaimer(rich_client: TestClient) -> None:
    resp = rich_client.get("/events/synth-launch-2026/before-after?turning_point=tp-response-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["note"] == "Descriptive comparison, not a causal estimate."
    assert data["confirmed"] is True
    assert data["form"] == "blog post"
    by_id = {row["storyline_id"]: row for row in data["storylines"]}
    assert by_id["launch-claims"]["volume_before_per_h"] > 0
    assert by_id["launch-claims"]["volume_after_per_h"] > 0
    assert by_id["launch-claims"]["addressed"] is True
    assert by_id["pricing-talk"]["volume_before_per_h"] > 0
    assert by_id["pricing-talk"]["volume_after_per_h"] == 0.0
    assert by_id["pricing-talk"]["addressed"] is False


def test_brief_computed_from_data_and_cached(rich_client: TestClient, client: TestClient) -> None:
    # Empty event brief never invents numbers
    empty_resp = client.get("/events/test-synthetic-launch/brief")
    assert empty_resp.status_code == 200
    empty_brief = empty_resp.json()
    assert empty_brief["summary"] == []
    assert empty_brief["evidence_to_cite"] == []
    assert "Descriptive comparison, not a causal estimate." in empty_brief["method_and_limits"]

    # Populated event brief cites real item_ids and returns cached result on repeat call
    r1 = rich_client.get("/events/synth-launch-2026/brief")
    assert r1.status_code == 200
    b1 = r1.json()
    for required_key in (
        "summary",
        "storylines",
        "claims",
        "watch",
        "evidence_to_cite",
        "method_and_limits",
        "coverage",
        "generated_at",
        "model_id",
        "prompt_version",
    ):
        assert required_key in b1

    assert len(b1["summary"]) >= 1
    for sentence in b1["summary"]:
        assert isinstance(sentence["item_ids"], list)
        assert len(sentence["item_ids"]) >= 1
    assert b1["model_id"] == "gemini-2.5-flash-lite"
    assert b1["prompt_version"] == "v1"
    assert len(b1["evidence_to_cite"]) >= 1
    assert "raw-secret-uid" not in r1.text

    # Cached response returns identical generated_at
    r2 = rich_client.get("/events/synth-launch-2026/brief")
    assert r2.status_code == 200
    assert r2.json()["generated_at"] == b1["generated_at"]


def test_trust_with_real_data(rich_client: TestClient) -> None:
    resp = rich_client.get("/events/synth-launch-2026/trust")
    assert resp.status_code == 200
    data = resp.json()
    assert data["coverage"]["x"] == {"count": 7, "cap": 500}
    assert data["coverage"]["hn"] == {"count": 1}
    assert data["coverage"]["news"] == {"count": 1}
    assert data["unassigned_count"] == 1
    assert data["backfilled_share"] == pytest.approx(2 / 9, abs=1e-3)
    assert data["gold"]["assignment_accuracy"] == 0.86


def test_spanner_backend_returns_identical_shapes_and_respects_kill_switch(
    rich_event_and_frames: tuple[Path, Path],
) -> None:
    from services.api.store import SpannerStore

    events_dir, frames_dir = rich_event_and_frames
    frames_data = json.loads((frames_dir / "synth-launch-2026.frames.json").read_text())

    all_nodes = [n for f in frames_data for n in f["nodes_added"]]
    all_edges = [e for f in frames_data for e in f["edges_added"]]

    # Enrich mock Spanner nodes/edges with item text_excerpt + claim edges
    mock_nodes = list(all_nodes)
    for n in mock_nodes:
        if n["id"] == "x:100":
            n["text_excerpt"] = "Our new platform delivers 10k tokens per second in internal benchmarks."
            n["url"] = "https://example.com/post/100"
        elif n["id"] == "hn:200":
            n["text_excerpt"] = "Our independent test saw much lower numbers than 10k tokens per second."
            n["url"] = "https://news.ycombinator.com/item?id=200"

    mock_edges = list(all_edges) + [
        {
            "source": "x:100",
            "target": "claim-throughput",
            "type": "supports",
            "created_at": "2026-11-16T12:10:00Z",
            "hour": 0,
            "evidence_span": "platform delivers 10k tokens per second",
            "agreement": 1.0,
        },
        {
            "source": "hn:200",
            "target": "claim-throughput",
            "type": "contradicts",
            "created_at": "2026-11-16T13:15:00Z",
            "hour": 1,
            "evidence_span": "much lower numbers than 10k tokens per second",
            "agreement": 1.0,
        },
    ]

    def fake_spanner_query(event_id: str, at_hour: int, t0_utc: str, *, database=None):
        assert event_id == "synth-launch-2026"
        filtered_nodes = [n for n in mock_nodes if int(n.get("hour", 0)) <= at_hour]
        filtered_edges = [e for e in mock_edges if int(e.get("hour", 0)) <= at_hour]
        return filtered_nodes, filtered_edges

    # 1. Kill-switch off (SPANNER_LIVE=false) -> falls back to frames backend
    kill_settings = Settings(
        events_dir=str(events_dir),
        frames_dir=str(frames_dir),
        graph_backend="spanner",
        spanner_live=False,
    )
    kill_client = TestClient(create_app(kill_settings))
    assert kill_client.get("/healthz").json() == {"ok": True, "graph_backend": "frames"}

    # 2. Spanner live on (GRAPH_BACKEND=spanner, SPANNER_LIVE=true) -> uses SpannerStore
    spanner_store = SpannerStore(query_fn=fake_spanner_query)
    live_settings = Settings(
        events_dir=str(events_dir),
        graph_backend="spanner",
        spanner_live=True,
    )
    spanner_client = TestClient(create_app(live_settings, store=spanner_store))
    assert spanner_client.get("/healthz").json() == {"ok": True, "graph_backend": "spanner"}

    sp_frames = spanner_client.get("/events/synth-launch-2026/frames?from_hour=0&to_hour=1").json()
    assert [f["hour"] for f in sp_frames] == [0, 1]
    assert set(sp_frames[0].keys()) == {"hour", "nodes_added", "edges_added", "storyline_counts"}

    sp_storylines = spanner_client.get("/events/synth-launch-2026/storylines?at_hour=1").json()
    sp_by_id = {s["id"]: s for s in sp_storylines}
    assert sp_by_id["launch-claims"]["count"] == 3
    assert sp_by_id["launch-claims"]["first_seen"]["item_id"] == "x:100"

    sp_claim = spanner_client.get("/events/synth-launch-2026/claims/claim-throughput?at_hour=1").json()
    assert sp_claim["status"] == "mixed"
    assert sp_claim["counts"] == {"supporting": 1, "contradicting": 1, "neutral": 0}

    sp_item = spanner_client.get("/events/synth-launch-2026/items/x:100").json()
    assert sp_item["platform"] == "x"
    assert len(sp_item["excerpt"]) <= 300
    assert "author_ref" not in sp_item


def test_bigquery_frames_store_queries_use_explicit_columns_and_event_filter(
    rich_event_and_frames: tuple[Path, Path],
) -> None:
    from services.api.store import FramesStore

    events_dir, _ = rich_event_and_frames
    executed_queries: list[str] = []

    class FakeQueryJob:
        def __init__(self, rows):
            self._rows = rows

        def result(self):
            return self._rows

    class FakeBQClient:
        def query(self, sql: str, job_config=None):
            executed_queries.append(sql)
            assert "SELECT *" not in sql.upper()
            assert "event_id = @event_id" in sql
            if ".frames`" in sql:
                return FakeQueryJob(
                    [
                        {
                            "hour": 0,
                            "nodes_added": json.dumps(
                                [
                                    {
                                        "id": "x:100",
                                        "type": "post",
                                        "platform": "x",
                                        "kind": "post",
                                        "created_at": "2026-11-16T12:10:00Z",
                                        "hour": 0,
                                        "storyline_id": "launch-claims",
                                        "reach": 500,
                                        "is_official": True,
                                        "self_reported": True,
                                        "conflict": None,
                                        "lang": "en",
                                        "label": "OfficialAcct",
                                    }
                                ]
                            ),
                            "edges_added": json.dumps([]),
                            "storyline_counts": json.dumps({"launch-claims": 1, "unassigned": 0}),
                        }
                    ]
                )
            return FakeQueryJob([])

    bq_store = FramesStore(
        frames_dir=None,
        gcp_project="test-proj",
        bq_dataset="verifact",
        bq_client=FakeBQClient(),
    )
    settings = Settings(events_dir=str(events_dir), graph_backend="frames")
    bq_client_http = TestClient(create_app(settings, store=bq_store))

    frames = bq_client_http.get("/events/synth-launch-2026/frames").json()
    assert len(frames) == 1
    assert frames[0]["hour"] == 0
    assert len(executed_queries) >= 1


def test_frame_builder_public_snapshot_compatibility(
    rich_event_and_frames: tuple[Path, Path], tmp_path: Path
) -> None:
    events_dir, frames_dir = rich_event_and_frames
    fb_dir = tmp_path / "fb_out"
    fb_dir.mkdir(parents=True, exist_ok=True)
    # Copy frames.json and write a frame-builder-style public snapshot (no enrichments array)
    (fb_dir / "synth-launch-2026.frames.json").write_text(
        (frames_dir / "synth-launch-2026.frames.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    fb_snapshot = {
        "synthetic": True,
        "event_id": "synth-launch-2026",
        "built_at": "2026-11-16T15:00:00Z",
        "signals": [
            {
                "storyline_id": "pricing-talk",
                "n_items": 3,
                "window_minutes": 4,
                "text": (
                    "3 near-identical posts within 4 minutes. "
                    "This can reflect organic copy-paste or coordination; no account is labelled."
                ),
            }
        ],
        "trust": {
            "coverage": {"x": {"count": 1, "cap": 500}},
            "gold": None,
            "unassigned_count": 0,
            "backfilled_share": 0.5,
        },
        "items": [
            {
                "item_id": "x:100",
                "created_at": "2026-11-16T12:10:00Z",
                "collected_at": "2026-11-16T12:15:00Z",
                "collection_lag_s": 300,
                "platform": "x",
                "url": "https://example.com/post/100",
                "excerpt": "Our new platform delivers 10k tokens per second in internal benchmarks.",
                "storyline_id": "launch-claims",
                "claims": [
                    {
                        "claim_id": "claim-throughput",
                        "stance": "supports",
                        "span": "platform delivers 10k tokens per second",
                    }
                ],
                "reach": 50000,
                "label": "OfficialAcct",
                "is_official": True,
                "self_reported": True,
                "conflict": None,
                "tags": ["x", "lang:en", "self-reported"],
            }
        ],
    }
    (fb_dir / "synth-launch-2026.snapshot.json").write_text(
        json.dumps(fb_snapshot), encoding="utf-8"
    )
    fb_client = TestClient(
        create_app(Settings(events_dir=str(events_dir), frames_dir=str(fb_dir)))
    )

    item = fb_client.get("/events/synth-launch-2026/items/x:100").json()
    assert item["storyline_id"] == "launch-claims"
    assert item["claims"][0]["claim_id"] == "claim-throughput"
    assert "lang:en" in item["tags"]
    assert "author_ref" not in item

    claim = fb_client.get("/events/synth-launch-2026/claims/claim-throughput?at_hour=0").json()
    assert claim["status"] == "supported"
    assert claim["self_reported"] is True

    signals = fb_client.get("/events/synth-launch-2026/signals").json()
    assert len(signals) == 1
    assert signals[0]["n_items"] == 3

    trust = fb_client.get("/events/synth-launch-2026/trust").json()
    assert trust["backfilled_share"] == 0.5


def test_live_stream_when_live_ingestion_enabled(
    rich_event_and_frames: tuple[Path, Path],
) -> None:
    events_dir, frames_dir = rich_event_and_frames
    live_client = TestClient(
        create_app(
            Settings(
                events_dir=str(events_dir),
                frames_dir=str(frames_dir),
                live_ingestion=True,
                sse_heartbeat_seconds=0.1,
            )
        )
    )
    with live_client.stream("GET", "/events/synth-launch-2026/live") as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line]
    joined = "\n".join(lines)
    assert '"live": true' in joined
    assert "event: storyline_counts" in joined
    assert "event: item_added" in joined


