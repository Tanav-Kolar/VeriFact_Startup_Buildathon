"""Release Guardrails Regression Test Suite (Spec 06, 07, 08, 09; PRD §8; evaluation.md §3).

Tests all automated release guardrails:
  1. No future leakage across random `at_hour` values (`created_at <= t0 + at_hour`).
  2. Every SUPPORTS/CONTRADICTS edge and claim card `evidence_span` is a verbatim substring of `items.text`.
  3. Privacy: zero `author_ref` in API JSON or Spanner properties; excerpt <= 300 chars.
  4. Multi-event generality (Release Criteria #14 & #15): loading second event YAML runs
     queries, frame_builder, spanner_graph, and API end-to-end with zero code changes,
     and omits disabled sources.
  5. Codebase guardrail scan: zero banned verdict/bot/causal/suppression words and
     zero event-specific literals in production code.

All fixtures marked `synthetic: true`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest
import yaml

from services.api.main import Settings, create_app
from services.enrich.main import verify_and_aggregate_claims
from services.frame_builder.main import (
    _coerce_account_lookup,
    build_frames,
    compute_claim_cards,
    compute_storylines,
)
from verifact_core.config import EventConfig, load_event
from verifact_core.queries import build_queries
from verifact_core.spanner_graph import build_graph_mutations

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_SECOND_EVENT = REPO_ROOT / "tests" / "fixtures" / "behavior_second_event.yaml"

# Dynamically construct banned token lists so this test file does not create false positive hits
BANNED_VERDICT_TOKENS = [
    "".join(["ver", "dict"]),
    "".join(["is_", "true"]),
    "".join(["is_", "false"]),
    "".join(["truth", "_score"]),
    "".join(["credibility", "_score"]),
    "".join(["risk", "_score"]),
]

BANNED_BOT_TOKENS = [
    "".join(["is_", "bot"]),
    "".join(["bot", "_score"]),
    "".join(["sock", "puppet"]),
    "".join(["tr", "oll"]),
    "".join(["fake_", "account"]),
    "".join(["in", "authentic"]),
]

BANNED_CAUSAL_TOKENS = [
    "".join(["cau", "sed"]),
    "".join(["led", " to"]),
    "".join(["resulted", " in"]),
]

BANNED_SUPPRESSION_TOKENS = [
    "".join(["take", "down"]),
    "".join(["take", "_down"]),
    "".join(["flag_", "content"]),
    "".join(["mass_", "reply"]),
    "".join(["report_", "to"]),
    "".join(["report", "-to"]),
]


@pytest.fixture
def guardrail_event_config() -> EventConfig:
    """Synthetic event configuration for guardrail testing."""
    raw = yaml.safe_load(FIXTURE_SECOND_EVENT.read_text(encoding="utf-8"))
    return EventConfig.model_validate(raw)


# ===========================================================================
# 1. No future leakage across random at_hour values
# ===========================================================================
def test_guardrail_no_future_leakage_across_at_hours(guardrail_event_config: EventConfig) -> None:
    """PRD Criterion #3 & evaluation.md §3: at any at_hour, no node or edge has created_at > at_hour."""
    cfg = guardrail_event_config
    t0_dt = cfg.t0.utc

    # Generate synthetic items from hour -5 to hour +25
    items = []
    enrichments = []
    for h in range(-5, 26):
        item_id = f"hn:item-{h + 100}"
        item_dt = t0_dt + timedelta(hours=h)
        items.append({
            "synthetic": True,
            "item_id": item_id,
            "platform": "hn",
            "created_at": item_dt.isoformat().replace("+00:00", "Z"),
            "author_ref": f"hn_user_{h}",
            "text": f"Synthetic outage report update at hour {h}.",
        })
        enrichments.append({
            "synthetic": True,
            "item_id": item_id,
            "storyline_id": "dns-routing-failure" if h % 2 == 0 else "service-restoration",
            "claims": [
                {
                    "claim_id": "root-cause-bgp",
                    "stance": "supports",
                    "evidence_span": "Synthetic outage report",
                }
            ],
        })

    account_lookup = _coerce_account_lookup(None, items)
    now_dt = t0_dt + timedelta(hours=30)

    # Test random at_hour sample across timeline
    frames = build_frames(cfg, items, enrichments, account_lookup, now=now_dt)
    for at_hour in (-3, 0, 4, 12, 20):
        cutoff_dt = t0_dt + timedelta(hours=at_hour)

        # Check frame nodes and edges up to at_hour
        nodes_up_to_h = [node for f in frames if f["hour"] <= at_hour for node in f["nodes_added"]]
        edges_up_to_h = [edge for f in frames if f["hour"] <= at_hour for edge in f["edges_added"]]

        for node in nodes_up_to_h:
            created_str = node.get("created_at")
            if created_str:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                assert created_dt <= cutoff_dt, (
                    f"Future leakage in node {node['id']}: created_at={created_dt} > cutoff={cutoff_dt}"
                )
            if node.get("hour") is not None:
                assert node["hour"] <= at_hour, (
                    f"Future leakage in node {node['id']}: hour={node['hour']} > at_hour={at_hour}"
                )

        for edge in edges_up_to_h:
            if edge.get("hour") is not None:
                assert edge["hour"] <= at_hour, (
                    f"Future leakage in edge {edge}: hour={edge['hour']} > at_hour={at_hour}"
                )

        # Check claim cards: no evidence item created after cutoff
        claim_cards = compute_claim_cards(cfg, items, enrichments, at_hour=at_hour)
        for card in claim_cards:
            for ev in card.get("evidence", []):
                ev_created = datetime.fromisoformat(ev["created_at"].replace("Z", "+00:00"))
                assert ev_created <= cutoff_dt, (
                    f"Future leakage in claim evidence {ev['item_id']}: {ev_created} > {cutoff_dt}"
                )

        # Check storylines: volume_by_hour strictly caps at at_hour
        storylines = compute_storylines(cfg, items, enrichments, account_lookup, at_hour=at_hour)
        for sl in storylines:
            for vol in sl.get("volume_by_hour", []):
                assert vol["hour"] <= at_hour, (
                    f"Future leakage in storyline volume {sl['id']}: hour={vol['hour']} > {at_hour}"
                )


# ===========================================================================
# 2. Verbatim evidence spans
# ===========================================================================
def test_guardrail_evidence_spans_are_verbatim_substrings(guardrail_event_config: EventConfig) -> None:
    """Principle P1.2 & PRD Criterion #7: evidence span MUST be exact substring of items.text.

    If span fails substring check, edge and claim output MUST be dropped.
    """
    cfg = guardrail_event_config
    valid_text = "Outage traced to upstream BGP misconfiguration in central cluster."

    # 1. Stance aggregation drops non-substring spans
    hallucinated_outputs = [
        {"claims": [{"claim_id": "root-cause-bgp", "stance": "supports", "evidence_span": "Hallucinated span not in text"}]},
        {"claims": [{"claim_id": "root-cause-bgp", "stance": "supports", "evidence_span": "Hallucinated span not in text"}]},
        {"claims": [{"claim_id": "root-cause-bgp", "stance": "supports", "evidence_span": "Hallucinated span not in text"}]},
    ]
    verified_bad = verify_and_aggregate_claims(valid_text, hallucinated_outputs, {"root-cause-bgp"})
    assert len(verified_bad) == 0, "Non-substring evidence span must be dropped per P1.2"

    # Exact substring span is retained
    exact_outputs = [
        {"claims": [{"claim_id": "root-cause-bgp", "stance": "supports", "evidence_span": "upstream BGP misconfiguration"}]},
        {"claims": [{"claim_id": "root-cause-bgp", "stance": "supports", "evidence_span": "upstream BGP misconfiguration"}]},
        {"claims": [{"claim_id": "root-cause-bgp", "stance": "supports", "evidence_span": "upstream BGP misconfiguration"}]},
    ]
    verified_good = verify_and_aggregate_claims(valid_text, exact_outputs, {"root-cause-bgp"})
    assert len(verified_good) == 1
    assert verified_good[0]["evidence_span"] == "upstream BGP misconfiguration"
    assert verified_good[0]["evidence_span"] in valid_text

    # 2. Spanner Graph edge mutations verify exact substring
    items = [
        {
            "synthetic": True,
            "item_id": "hn:post-1",
            "platform": "hn",
            "created_at": "2026-11-18T11:00:00Z",
            "author_ref": "eng_lead",
            "text": valid_text,
        }
    ]
    enrichments = [
        {
            "synthetic": True,
            "item_id": "hn:post-1",
            "storyline_id": "dns-routing-failure",
            "claims": [
                {
                    "claim_id": "root-cause-bgp",
                    "stance": "supports",
                    "evidence_span": "upstream BGP misconfiguration",
                }
            ],
        }
    ]
    batch = build_graph_mutations(cfg, items, enrichments, None)
    for edge in batch.edges:
        if edge.edge_label in {"SUPPORTS", "CONTRADICTS"}:
            span = edge.properties.get("evidence_span")
            assert span is not None
            assert span in valid_text, f"Spanner edge span {span!r} must be substring of {valid_text!r}"

    # 3. Frame builder claim cards verify exact substring
    cards = compute_claim_cards(cfg, items, enrichments)
    for card in cards:
        for ev in card.get("evidence", []):
            assert ev["span"] in valid_text, f"Claim card span {ev['span']!r} must be substring of text"


# ===========================================================================
# 3. Privacy: zero author_ref leaks and <= 300 char excerpts
# ===========================================================================
def test_guardrail_privacy_no_author_ref_leaks_and_short_excerpts(
    guardrail_event_config: EventConfig,
    tmp_path: Path,
) -> None:
    """Principle P5 & evaluation.md §3: zero author_ref in API JSON or Spanner nodes; excerpts <= 300 chars."""
    cfg = guardrail_event_config
    long_text = "Detailed incident statement with extensive elaboration: " + ("A" * 500)
    secret_private_handle = "confidential_engineer_handle_xyz"

    items = [
        {
            "synthetic": True,
            "item_id": "x:incident-1",
            "platform": "x",
            "created_at": "2026-11-18T10:15:00Z",
            "author_ref": secret_private_handle,
            "author_followers": 80,  # Below 5000 threshold
            "text": long_text,
        }
    ]
    enrichments = [
        {
            "synthetic": True,
            "item_id": "x:incident-1",
            "storyline_id": "dns-routing-failure",
            "claims": [],
        }
    ]

    # Check Spanner Graph node properties
    account_lookup = _coerce_account_lookup(None, items)
    batch = build_graph_mutations(cfg, items, enrichments, account_lookup)
    for node in batch.nodes:
        # 1. author_ref must NEVER be in node.properties
        assert "author_ref" not in node.properties, f"author_ref leaked in node properties: {node}"
        # 2. String values in properties must respect <= 300 char limit
        for k, v in node.properties.items():
            if isinstance(v, str):
                assert len(v) <= 300, f"Property {k} exceeded 300 chars: {len(v)}"
        # 3. Private account handle must never appear as label or display
        display = node.properties.get("display") or node.properties.get("label", "")
        assert secret_private_handle not in display
        if node.label == "Account" and not node.properties.get("is_public_figure", False):
            assert re.match(r"^Account A\d+$", display)

    # Check Frame builder snapshots and API endpoints
    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    event_file = events_dir / f"{cfg.event_id}.yaml"
    event_file.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False), encoding="utf-8")

    frames_dir = tmp_path / "frames" / cfg.event_id
    frames_dir.mkdir(parents=True, exist_ok=True)
    frames = build_frames(cfg, items, enrichments, account_lookup, now=cfg.t0.utc + timedelta(hours=5))
    for frame in frames:
        hour = frame["hour"]
        (frames_dir / f"frame_{hour:03d}.json").write_text(json.dumps(frame), encoding="utf-8")

    app = create_app(Settings(events_dir=str(events_dir), frames_dir=str(tmp_path / "frames"), graph_backend="frames"))
    client = TestClient(app)

    # Scan API responses
    endpoints = [
        f"/events/{cfg.event_id}",
        f"/events/{cfg.event_id}/frames",
        f"/events/{cfg.event_id}/storylines",
        f"/events/{cfg.event_id}/trust",
    ]
    for ep in endpoints:
        resp = client.get(ep)
        assert resp.status_code == 200, f"Endpoint {ep} failed: {resp.text}"
        body_text = resp.text
        # Zero author_ref in any response key
        assert '"author_ref"' not in body_text, f"author_ref leaked in {ep} response"
        # Secret private handle never exposed
        assert secret_private_handle not in body_text, f"Private handle leaked in {ep} response"


# ===========================================================================
# 4. Multi-event generality & disabled source omission
# ===========================================================================
def test_guardrail_multi_event_generality_and_disabled_source(tmp_path: Path) -> None:
    """PRD Release Criteria #14 & #15: Second event config runs collection & shows own graph with zero code changes.

    Disabling a source in config removes its platform chip from sources list.
    """
    # 1. Load second synthetic event fixture
    raw_yaml = FIXTURE_SECOND_EVENT.read_text(encoding="utf-8")
    assert "# synthetic: true" in raw_yaml

    events_dir = tmp_path / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    target_event_yaml = events_dir / "synthetic-cloud-outage.yaml"
    target_event_yaml.write_text(raw_yaml, encoding="utf-8")

    # Load with verifact_core.config.load_event
    cfg = load_event(target_event_yaml)
    assert cfg.event_id == "synthetic-cloud-outage"
    assert cfg.name == "Synthetic Cloud Outage Incident"

    # Verify Criterion #15: YouTube is disabled
    assert cfg.sources.youtube is not None
    assert cfg.sources.youtube.enabled is False
    assert cfg.sources.hn.enabled is True
    assert cfg.sources.x.enabled is True

    # 2. build_queries works dynamically for the second event
    queries_hn = build_queries(cfg, "hn")
    assert len(queries_hn) > 0
    query_texts = [q.text.lower() for q in queries_hn]
    assert any("synthetic cloud outage" in qt or "syntheticcloud down" in qt for qt in query_texts)

    # 3. Frame builder runs end-to-end for second event with zero code changes
    items = [
        {
            "synthetic": True,
            "item_id": "hn:outage-1",
            "platform": "hn",
            "created_at": "2026-11-18T10:30:00Z",
            "author_ref": "sysadmin_99",
            "text": "Outage traced to upstream BGP misconfiguration in DNS nodes.",
        }
    ]
    enrichments = [
        {
            "synthetic": True,
            "item_id": "hn:outage-1",
            "storyline_id": "dns-routing-failure",
            "claims": [
                {
                    "claim_id": "root-cause-bgp",
                    "stance": "supports",
                    "evidence_span": "Outage traced to upstream BGP misconfiguration",
                }
            ],
        }
    ]
    frames = build_frames(cfg, items, enrichments, None, now=cfg.t0.utc + timedelta(hours=2))
    assert len(frames) > 0

    # 4. Spanner graph mutations build cleanly for second event
    batch = build_graph_mutations(cfg, items, enrichments, None)
    assert len(batch.nodes) > 0
    for node in batch.nodes:
        assert node.event_id == "synthetic-cloud-outage"

    # 5. API serves second event and omits disabled source (Release Criterion #15)
    frames_dir = tmp_path / "frames" / cfg.event_id
    frames_dir.mkdir(parents=True, exist_ok=True)
    for frame in frames:
        hour = frame["hour"]
        (frames_dir / f"frame_{hour:03d}.json").write_text(json.dumps(frame), encoding="utf-8")

    app = create_app(Settings(events_dir=str(events_dir), frames_dir=str(tmp_path / "frames"), graph_backend="frames"))
    client = TestClient(app)

    # Check /events includes synthetic-cloud-outage
    events_resp = client.get("/events")
    assert events_resp.status_code == 200
    event_ids = [e["event_id"] for e in events_resp.json()]
    assert "synthetic-cloud-outage" in event_ids

    # Check /events/synthetic-cloud-outage details
    detail_resp = client.get("/events/synthetic-cloud-outage")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["name"] == "Synthetic Cloud Outage Incident"

    # Criterion #15: Disabling a source removes its platform chip from sources
    sources = detail["sources"]
    assert "hn" in sources
    assert "x" in sources
    assert "news" in sources
    assert "youtube" not in sources, "Disabled source 'youtube' must NOT appear in API sources list"


# ===========================================================================
# 5. Codebase guardrail scan
# ===========================================================================
def test_guardrail_codebase_greps_clean() -> None:
    """Principles.md checklist & stack-and-conventions.md: Zero banned verdict/bot/causal/suppression or sarvam literals."""
    scan_dirs = [
        REPO_ROOT / "services",
        REPO_ROOT / "verifact_core",
        REPO_ROOT / "infra",
        REPO_ROOT / "web" / "src",
    ]

    target_extensions = {".py", ".ts", ".tsx", ".js", ".html", ".sql", ".sh"}

    sarvam_violations = []
    verdict_violations = []
    bot_violations = []

    for sdir in scan_dirs:
        if not sdir.exists():
            continue
        for fpath in sdir.rglob("*"):
            if not fpath.is_file() or fpath.suffix not in target_extensions:
                continue
            # Skip caches and build outputs
            parts = fpath.parts
            if any(p in {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "dist"} for p in parts):
                continue

            content = fpath.read_text(encoding="utf-8", errors="ignore")
            rel_path = fpath.relative_to(REPO_ROOT)

            # 1. Zero event-specific literal in production code
            for match in re.finditer(r"\bsarvam\b", content, re.IGNORECASE):
                sarvam_violations.append(f"{rel_path}:{match.start()}")

            # 2. Zero verdict tokens in production code
            for token in BANNED_VERDICT_TOKENS:
                for match in re.finditer(re.escape(token), content, re.IGNORECASE):
                    verdict_violations.append(f"{rel_path}:{token}")

            # 3. Zero bot tokens in production code
            for token in BANNED_BOT_TOKENS:
                for match in re.finditer(re.escape(token), content, re.IGNORECASE):
                    bot_violations.append(f"{rel_path}:{token}")

    assert len(sarvam_violations) == 0, f"Found sarvam literals in production code: {sarvam_violations[:5]}"
    assert len(verdict_violations) == 0, f"Found banned verdict tokens in production code: {verdict_violations[:5]}"
    assert len(bot_violations) == 0, f"Found banned bot tokens in production code: {bot_violations[:5]}"

    # 4. Zero causal & suppression words in services/api/ and web/src/
    causal_suppression_dirs = [
        REPO_ROOT / "services" / "api",
        REPO_ROOT / "web" / "src",
    ]
    causal_violations = []
    suppression_violations = []

    for csdir in causal_suppression_dirs:
        if not csdir.exists():
            continue
        for fpath in csdir.rglob("*"):
            if not fpath.is_file() or fpath.suffix not in target_extensions:
                continue
            parts = fpath.parts
            if any(p in {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "dist"} for p in parts):
                continue

            content = fpath.read_text(encoding="utf-8", errors="ignore")
            rel_path = fpath.relative_to(REPO_ROOT)

            for token in BANNED_CAUSAL_TOKENS:
                for match in re.finditer(re.escape(token), content, re.IGNORECASE):
                    causal_violations.append(f"{rel_path}:{token}")

            for token in BANNED_SUPPRESSION_TOKENS:
                for match in re.finditer(re.escape(token), content, re.IGNORECASE):
                    suppression_violations.append(f"{rel_path}:{token}")

    assert len(causal_violations) == 0, f"Found causal tokens in api/web: {causal_violations[:5]}"
    assert len(suppression_violations) == 0, f"Found suppression tokens in api/web: {suppression_violations[:5]}"
