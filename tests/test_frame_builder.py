"""TDD unit and integration tests for services.frame_builder (Spec 08)."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from services.frame_builder.main import (
    build_frames,
    build_snapshot,
    compute_before_after,
    compute_claim_cards,
    compute_neutral_signals,
    compute_storylines,
    compute_trust,
    main,
    run_frame_builder,
)
from verifact_core.config import EventConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "frame_builder_sample.json"
FRAMES_SCHEMA_PATH = REPO_ROOT / "infra" / "bigquery" / "frames.json"


@pytest.fixture
def sample_bundle() -> dict[str, Any]:
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert raw.get("synthetic") is True
    return raw


@pytest.fixture
def sample_config(sample_bundle: dict[str, Any]) -> EventConfig:
    return EventConfig.model_validate(sample_bundle["config"])


def test_frames_schema_matches_data_model() -> None:
    schema_doc = json.loads(FRAMES_SCHEMA_PATH.read_text(encoding="utf-8"))
    fields = schema_doc["schema"]["fields"]
    by_name = {f["name"]: f["type"] for f in fields}
    assert by_name == {
        "event_id": "STRING",
        "hour": "INT64",
        "built_at": "TIMESTAMP",
        "nodes_added": "JSON",
        "edges_added": "JSON",
        "storyline_counts": "JSON",
    }
    assert schema_doc["clustering"]["fields"] == ["event_id", "hour"]


def test_build_frames_hour_range_and_node_shape(
    sample_config: EventConfig,
    sample_bundle: dict[str, Any],
) -> None:
    now = datetime(2026, 11, 16, 18, 0, 0, tzinfo=timezone.utc)  # T+6h
    frames = build_frames(
        sample_config,
        sample_bundle["items"],
        sample_bundle["enrichments"],
        sample_bundle["account_map"],
        now=now,
    )

    # pre_hours=2, now_hour=6 (clamped by post_hours=10 since live=False) -> -2..6 = 9 frames
    hours = [f["hour"] for f in frames]
    assert hours == list(range(-2, 7))

    required_node_keys = {
        "id",
        "type",
        "platform",
        "kind",
        "created_at",
        "hour",
        "storyline_id",
        "reach",
        "is_official",
        "self_reported",
        "conflict",
        "lang",
        "label",
    }

    all_nodes: list[dict[str, Any]] = []
    for frame in frames:
        assert frame["event_id"] == sample_config.event_id
        assert frame["built_at"].endswith("Z")
        for node in frame["nodes_added"]:
            assert set(node.keys()) == required_node_keys
            assert "author_ref" not in node
            assert "user_private_99" not in json.dumps(node)
            assert len(str(node["label"])) <= 300
            assert node["hour"] <= frame["hour"]
            all_nodes.append(node)

    nodes_by_id = {n["id"]: n for n in all_nodes}
    assert "x:1000" in nodes_by_id
    assert nodes_by_id["x:1000"]["is_official"] is True
    assert nodes_by_id["x:1000"]["self_reported"] is True
    assert nodes_by_id["x:1000"]["label"] == "OfficialOrg"

    # Private account with 450 followers (< 50000 threshold) gets stable pseudonym Account A2
    assert nodes_by_id["x:1001"]["label"] == "Account A2"

    # Configured conflict person is named and carries inline conflict disclosure
    assert nodes_by_id["x:1002"]["label"] == "Jordan Investor"
    assert nodes_by_id["x:1002"]["conflict"] == {
        "person": "Jordan Investor",
        "relationship": "Partner at Horizon Ventures, an investor in OfficialOrg",
    }


def test_build_frames_edges_placed_in_later_endpoint_hour(
    sample_config: EventConfig,
    sample_bundle: dict[str, Any],
) -> None:
    now = datetime(2026, 11, 16, 18, 0, 0, tzinfo=timezone.utc)
    frames = build_frames(
        sample_config,
        sample_bundle["items"],
        sample_bundle["enrichments"],
        sample_bundle["account_map"],
        now=now,
    )

    seen_node_ids: set[str] = set()
    all_edges: list[dict[str, Any]] = []
    required_edge_keys = {"source", "target", "type", "created_at", "hour"}

    for frame in frames:
        for node in frame["nodes_added"]:
            seen_node_ids.add(node["id"])
        for edge in frame["edges_added"]:
            assert set(edge.keys()) == required_edge_keys
            assert edge["hour"] == frame["hour"]
            # Neither source nor target can refer to a future node not yet added by frame.hour
            assert edge["source"] in seen_node_ids
            assert edge["target"] in seen_node_ids
            all_edges.append(edge)

    # Check reply edge x:1001 -> x:1000 at hour 1
    reply_edges = [
        e for e in all_edges if e["source"] == "x:1001" and e["target"] == "x:1000" and e["type"] == "replied_to"
    ]
    assert len(reply_edges) == 1
    assert reply_edges[0]["hour"] == 1

    # Check explicit link edge hn:2000 -> x:1001 at hour 3
    link_edges = [
        e for e in all_edges if e["source"] == "hn:2000" and e["target"] == "x:1001" and e["type"] == "links_to"
    ]
    assert len(link_edges) == 1
    assert link_edges[0]["hour"] == 3


def test_build_frames_cumulative_storyline_counts(
    sample_config: EventConfig,
    sample_bundle: dict[str, Any],
) -> None:
    now = datetime(2026, 11, 16, 18, 0, 0, tzinfo=timezone.utc)
    frames = build_frames(
        sample_config,
        sample_bundle["items"],
        sample_bundle["enrichments"],
        sample_bundle["account_map"],
        now=now,
    )
    by_hour = {f["hour"]: f["storyline_counts"] for f in frames}

    # Before T0: all 0
    assert by_hour[-1] == {
        "official-benchmarks": 0,
        "public-hardware-grants": 0,
        "unassigned": 0,
    }
    # Hour 0: x:1000 in official-benchmarks
    assert by_hour[0]["official-benchmarks"] == 1
    assert by_hour[0]["public-hardware-grants"] == 0
    # Hour 1: x:1001 in public-hardware-grants
    assert by_hour[1]["public-hardware-grants"] == 1
    # Hour 4: x:1001, x:1002, hn:2000, gdelt:art3000 -> 4 in public-hardware-grants
    assert by_hour[4]["public-hardware-grants"] == 4
    assert by_hour[6]["public-hardware-grants"] == 4


def test_build_frames_never_leaks_long_article_text(
    sample_config: EventConfig,
    sample_bundle: dict[str, Any],
) -> None:
    items = copy.deepcopy(sample_bundle["items"])
    long_body = "LONG_ARTICLE_MARKER " * 40  # 800 chars
    items[-1]["text"] = long_body

    now = datetime(2026, 11, 16, 18, 0, 0, tzinfo=timezone.utc)
    frames = build_frames(
        sample_config,
        items,
        sample_bundle["enrichments"],
        sample_bundle["account_map"],
        now=now,
    )
    serialized = json.dumps(frames)
    assert long_body not in serialized
    for frame in frames:
        for node in frame["nodes_added"]:
            assert len(str(node["label"])) <= 300


def test_compute_neutral_signals_detects_burst_without_account_ids() -> None:
    items = [
        {
            "item_id": f"x:dup{i}",
            "platform": "x",
            "author_ref": f"secret_author_{i}",
            "created_at": f"2026-11-16T14:0{i % 5}:00Z",
            "text": "Identical viral message about the cluster capacity launch today!",
        }
        for i in range(6)
    ]
    enrichments = [
        {
            "item_id": f"x:dup{i}",
            "storyline_id": "public-hardware-grants",
        }
        for i in range(6)
    ]

    signals = compute_neutral_signals(items, enrichments)
    assert len(signals) == 1
    sig = signals[0]
    assert sig["storyline_id"] == "public-hardware-grants"
    assert sig["n_items"] == 6
    assert sig["window_minutes"] == 4
    assert (
        sig["text"]
        == "6 near-identical posts within 4 minutes. This can reflect organic copy-paste or coordination; no account is labelled."
    )
    serialized = json.dumps(signals)
    assert "secret_author" not in serialized
    assert "Account A" not in serialized


def test_compute_claim_cards_verifies_exact_spans_self_reported_and_conflicts(
    sample_config: EventConfig,
    sample_bundle: dict[str, Any],
) -> None:
    # Inject a forged non-substring span to verify P1.2 drops it
    enrichments = copy.deepcopy(sample_bundle["enrichments"])
    enrichments[1]["claims"].append(
        {
            "claim_id": "claim-five-x-speed",
            "stance": "contradicts",
            "evidence_span": "THIS SPAN IS NOT IN THE ITEM TEXT AT ALL",
            "votes": ["contradicts", "contradicts", "contradicts"],
            "agreement": 1.0,
        }
    )

    cards = compute_claim_cards(
        sample_config,
        sample_bundle["items"],
        enrichments,
    )
    by_id = {c["claim_id"]: c for c in cards}

    # claim-five-x-speed: only supported by x:1000 (official post -> self_reported = True)
    # The forged contradicting span on x:1001 was dropped!
    speed_card = by_id["claim-five-x-speed"]
    assert speed_card["status"] == "supported"
    assert speed_card["counts"] == {"supporting": 1, "contradicting": 0, "neutral": 0}
    assert speed_card["self_reported"] is True
    assert speed_card["self_reported_note"] == "Company statement, not independently confirmed"
    assert len(speed_card["evidence"]) == 1
    assert speed_card["evidence"][0]["source_type"] == "company_statement"

    # claim-private-hardware:
    # - x:1001 contradicts (social)
    # - x:1002 supports (investor_with_conflict -> Jordan Investor conflict attached)
    # - gdelt:art3000 supports (official spokesperson quoted -> company_statement)
    hw_card = by_id["claim-private-hardware"]
    assert hw_card["status"] == "mixed"
    assert hw_card["counts"] == {"supporting": 2, "contradicting": 1, "neutral": 0}

    ev_by_item = {e["item_id"]: e for e in hw_card["evidence"]}
    assert ev_by_item["x:1002"]["source_type"] == "investor_with_conflict"
    assert ev_by_item["x:1002"]["conflict"] == {
        "person": "Jordan Investor",
        "relationship": "Partner at Horizon Ventures, an investor in OfficialOrg",
    }
    assert ev_by_item["gdelt:art3000"]["source_type"] == "company_statement"

    # Test at_hour cutoff: at hour 1 (T+1h), only x:1000 and x:1001 exist
    cards_h1 = compute_claim_cards(
        sample_config,
        sample_bundle["items"],
        enrichments,
        at_hour=1,
    )
    hw_h1 = {c["claim_id"]: c for c in cards_h1}["claim-private-hardware"]
    assert hw_h1["status"] == "contradicted"
    assert hw_h1["counts"] == {"supporting": 0, "contradicting": 1, "neutral": 0}


def test_compute_before_after_descriptive_note_and_volumes(
    sample_config: EventConfig,
    sample_bundle: dict[str, Any],
) -> None:
    result = compute_before_after(
        sample_config,
        sample_bundle["items"],
        sample_bundle["enrichments"],
    )
    assert result["note"] == "Descriptive comparison, not a causal estimate."
    assert result["confirmed"] is False
    assert result["form"] == "press_statement"
    assert result["turning_point"]["id"] == "response-1"

    by_sid = {s["storyline_id"]: s for s in result["storylines"]}
    assert "official-benchmarks" in by_sid
    assert "public-hardware-grants" in by_sid
    assert by_sid["public-hardware-grants"]["volume_before_per_h"] > 0.0
    assert by_sid["public-hardware-grants"]["volume_after_per_h"] > 0.0
    assert by_sid["public-hardware-grants"]["addressed"] is True


def test_compute_storylines_and_trust_helpers(
    sample_config: EventConfig,
    sample_bundle: dict[str, Any],
) -> None:
    storylines = compute_storylines(
        sample_config,
        sample_bundle["items"],
        sample_bundle["enrichments"],
        sample_bundle["account_map"],
    )
    by_id = {s["id"]: s for s in storylines}
    assert "unassigned" in by_id
    grants = by_id["public-hardware-grants"]
    assert grants["count"] == 4
    assert grants["first_seen"] == {"item_id": "x:1001", "platform": "x", "hour": 1}
    platforms = [p["platform"] for p in grants["platforms_reached"]]
    assert platforms == ["x", "hn", "news"]
    # Explicit link jump from x:1001 to hn:2000 at hour 3
    assert any(j["from_item"] == "x:1001" and j["to_item"] == "hn:2000" and j["hour"] == 3 for j in grants["jumps"])
    # Private amplifier is pseudonymised
    amp_labels = [a["label"] for a in grants["top_amplifiers"]]
    assert "user_private_99" not in amp_labels

    trust = compute_trust(sample_config, sample_bundle["items"], sample_bundle["enrichments"])
    assert trust["coverage"]["x"] == {"count": 3, "cap": 500}
    assert trust["coverage"]["hn"] == {"count": 1}
    assert trust["coverage"]["news"] == {"count": 1}
    assert trust["unassigned_count"] == 0
    assert trust["backfilled_share"] == 0.4


def test_cli_and_run_frame_builder_writes_local_files_and_bq(
    tmp_path: Path,
    sample_bundle: dict[str, Any],
) -> None:
    event_yaml = tmp_path / "synthetic-cloud-launch.yaml"
    event_yaml.write_text(yaml.safe_dump(sample_bundle["config"]), encoding="utf-8")

    input_json = tmp_path / "input.json"
    input_json.write_text(json.dumps(sample_bundle), encoding="utf-8")
    out_dir = tmp_path / "out"

    class FakeBqClient:
        project = "test-proj"

        def __init__(self) -> None:
            self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

        def insert_rows_json(self, table_ref: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            self.inserted.append((table_ref, rows))
            return []

    fake_bq = FakeBqClient()
    summary = run_frame_builder(
        event_path=event_yaml,
        input_json=input_json,
        output_dir=out_dir,
        dry_run=False,
        bq_client=fake_bq,
        now=datetime(2026, 11, 16, 18, 0, 0, tzinfo=timezone.utc),
    )

    assert summary["frames_count"] == 9
    assert summary["written_to_bq"] == 9
    assert len(fake_bq.inserted) == 1
    assert fake_bq.inserted[0][0].endswith(".frames")

    frames_file = out_dir / "synthetic-cloud-launch.frames.json"
    snapshot_file = out_dir / "synthetic-cloud-launch.snapshot.json"
    assert frames_file.is_file()
    assert snapshot_file.is_file()

    snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert "signals" in snapshot
    assert "claims" in snapshot
    assert "before_after" in snapshot
    assert "storylines" in snapshot
    assert "trust" in snapshot
    assert isinstance(snapshot["items"], list)
    assert all("author_ref" not in it for it in snapshot["items"])

    direct_snapshot = build_snapshot(
        EventConfig.model_validate(sample_bundle["config"]),
        sample_bundle["items"],
        sample_bundle["enrichments"],
        sample_bundle["account_map"],
        now=datetime(2026, 11, 16, 18, 0, 0, tzinfo=timezone.utc),
    )
    assert direct_snapshot["event_id"] == "synthetic-cloud-launch"
    assert len(direct_snapshot["items"]) == 5

    # Also test CLI main() with --dry-run
    rc = main(
        [
            "--event",
            str(event_yaml),
            "--input-json",
            str(input_json),
            "--output-dir",
            str(tmp_path / "cli_out"),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert (tmp_path / "cli_out" / "synthetic-cloud-launch.frames.json").is_file()
