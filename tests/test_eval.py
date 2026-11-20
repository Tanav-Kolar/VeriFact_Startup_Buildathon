"""Tests for the Gold-Set Evaluation Harness (eval/) and /trust eval wiring."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from eval.run import (  # noqa: E402
    STORYLINE_ACCURACY_TARGET,
    compute_cohens_kappa,
    compute_guardrail_summary,
    compute_mean_agreement,
    compute_stance_macro_f1,
    compute_storyline_accuracy,
    evaluate_gold_set,
    load_enrichments_file,
    load_gold_jsonl,
    main as eval_main,
)
from services.api.main import Settings, create_app  # noqa: E402
from services.api.store import FramesStore, _load_latest_eval_gold  # noqa: E402

FIXTURE_GOLD_PATH = REPO_ROOT / "tests" / "fixtures" / "eval_gold_sample.jsonl"


def _write_synthetic_event_yaml(directory: Path, event_id: str = "synth-eval-event") -> Path:
    """Create a valid synthetic event YAML config for eval tests."""
    directory.mkdir(parents=True, exist_ok=True)
    yaml_text = f"""
event_id: {event_id}
name: "Synthetic Eval Launch"
t0:
  utc: "2026-11-16T12:00:00Z"
  source_url: "https://example.com/post/1"
window:
  pre_hours: 12
  post_hours: 48
  live: false
official:
  accounts: ["SynthOrg"]
  posts: ["1"]
  urls: ["https://example.com/launch"]
  spokespeople: ["Alex Rivera"]
keywords:
  core: ["synthetic launch"]
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
seed_storylines:
  - id: pricing-claims
    name: "Pricing and throughput claims"
    seeds: ["Benchmark throughput claim"]
  - id: open-weights
    name: "Open weights licensing"
    seeds: ["Open weights Apache license"]
claims_to_check:
  - id: claim-throughput
    text: "Platform delivers 10k tokens per second"
    self_reported: true
  - id: claim-open-license
    text: "Weights are released under Apache 2.0"
    self_reported: false
""".strip()
    path = directory / f"{event_id}.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return path


def _sample_enrichments() -> list[dict[str, Any]]:
    """Synthetic enrichments matching tests/fixtures/eval_gold_sample.jsonl."""
    return [
        {
            "synthetic": True,
            "item_id": "x:9001",
            "lang": "en",
            "storyline_id": "pricing-claims",
            "text": "Our platform hits 10k tokens per second in benchmarks today.",
            "claims": [
                {
                    "claim_id": "claim-throughput",
                    "stance": "supports",
                    "evidence_span": "10k tokens per second in benchmarks",
                    "votes": ["supports", "supports", "supports"],
                    "agreement": 1.0,
                }
            ],
        },
        {
            "synthetic": True,
            "item_id": "x:9002",
            "lang": "en",
            "storyline_id": "pricing-claims",
            "text": "An independent test only saw 2k tokens per second on the same workload.",
            "claims": [
                {
                    "claim_id": "claim-throughput",
                    "stance": "contradicts",
                    "evidence_span": "independent test only saw 2k tokens per second",
                    "votes": ["contradicts", "contradicts", "neutral"],
                    "agreement": 0.6667,
                }
            ],
        },
        {
            "synthetic": True,
            "item_id": "hn:9003",
            "lang": "en",
            "storyline_id": "open-weights",
            "text": "Confirmed: weights released under Apache 2.0 in the repo.",
            "claims": [
                {
                    "claim_id": "claim-open-license",
                    "stance": "supports",
                    "evidence_span": "weights released under Apache 2.0",
                    "votes": ["supports", "supports", "supports"],
                    "agreement": 1.0,
                }
            ],
        },
        {
            "synthetic": True,
            "item_id": "x:9004",
            "lang": "hi-en",
            "storyline_id": "pricing-claims",
            "text": "bhai ye speed bilkul inflated lag rahi hai honestly",
            "claims": [
                {
                    "claim_id": "claim-throughput",
                    "stance": "contradicts",
                    "evidence_span": "bhai ye speed bilkul inflated lag rahi hai",
                    "votes": ["contradicts", "contradicts", "contradicts"],
                    "agreement": 1.0,
                }
            ],
        },
        {
            "synthetic": True,
            "item_id": "x:9005",
            "lang": "hi-Latn",
            "storyline_id": "open-weights",
            "text": "license details abhi check kar rahe hain",
            "claims": [
                {
                    "claim_id": "claim-open-license",
                    "stance": "neutral",
                    "evidence_span": "license details abhi check kar rahe hain",
                    "votes": ["neutral", "neutral", "supports"],
                    "agreement": 0.6667,
                }
            ],
        },
        {
            "synthetic": True,
            "item_id": "yt:9006",
            "lang": "hi",
            "storyline_id": "pricing-claims",  # deliberate mismatch (gold is unassigned)
            "text": "naye release ka overview video",
            "claims": [],
        },
        {
            "synthetic": True,
            "item_id": "news:9007",
            "lang": "en",
            "storyline_id": "open-weights",
            "text": "The published license restricts commercial redistribution above threshold.",
            "claims": [
                {
                    "claim_id": "claim-open-license",
                    "stance": "contradicts",
                    "evidence_span": "license restricts commercial redistribution",
                    "votes": ["contradicts", "contradicts", "contradicts"],
                    "agreement": 1.0,
                }
            ],
        },
        {
            "synthetic": True,
            "item_id": "x:9008",
            "lang": "en",
            "storyline_id": "unassigned",
            "text": "congrats to the engineering team on shipping!",
            "claims": [
                {
                    "claim_id": "claim-throughput",
                    "stance": "neutral",
                    "evidence_span": "congrats to the engineering team on shipping",
                    "votes": ["neutral", "neutral", "neutral"],
                    "agreement": 1.0,
                }
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Pure Metric Function Tests
# ---------------------------------------------------------------------------


def test_compute_storyline_accuracy_above_target() -> None:
    gold = [
        {"item_id": "x:1", "storyline_id": "a"},
        {"item_id": "x:2", "storyline_id": "b"},
        {"item_id": "x:3", "storyline_id": "unassigned"},
        {"item_id": "x:4", "storyline_id": "off-topic"},
    ]
    enrichments = {
        "x:1": {"item_id": "x:1", "storyline_id": "a"},
        "x:2": {"item_id": "x:2", "storyline_id": "b"},
        "x:3": {"item_id": "x:3", "storyline_id": "Unassigned"},
        "x:4": {"item_id": "x:4", "storyline_id": "unassigned", "off_topic": True},
    }

    accuracy, below_target, note = compute_storyline_accuracy(gold, enrichments)

    assert accuracy == 1.0
    assert below_target is False
    assert note is None


def test_compute_storyline_accuracy_below_target_sets_plain_note() -> None:
    gold = [
        {"item_id": "x:1", "storyline_id": "a"},
        {"item_id": "x:2", "storyline_id": "b"},
        {"item_id": "x:3", "storyline_id": "c"},
    ]
    enrichments = {
        "x:1": {"item_id": "x:1", "storyline_id": "a"},
        "x:2": {"item_id": "x:2", "storyline_id": "wrong"},
        "x:3": {"item_id": "x:3", "storyline_id": "wrong"},
    }

    accuracy, below_target, note = compute_storyline_accuracy(gold, enrichments)

    assert accuracy == pytest.approx(1 / 3, abs=1e-4)
    assert accuracy < STORYLINE_ACCURACY_TARGET
    assert below_target is True
    assert isinstance(note, str)
    assert "70%" in note


def test_compute_storyline_accuracy_empty_gold() -> None:
    accuracy, below_target, note = compute_storyline_accuracy([], {})
    assert accuracy is None
    assert below_target is False
    assert note is None


def test_compute_stance_macro_f1_perfect_and_imperfect() -> None:
    gold = [
        {"item_id": "x:1", "claims": [{"claim_id": "c1", "stance": "supports"}]},
        {"item_id": "x:2", "claims": [{"claim_id": "c1", "stance": "contradicts"}]},
        {"item_id": "x:3", "claims": [{"claim_id": "c1", "stance": "neutral"}]},
    ]
    perfect = {
        "x:1": {"claims": [{"claim_id": "c1", "stance": "supports"}]},
        "x:2": {"claims": [{"claim_id": "c1", "stance": "contradicts"}]},
        "x:3": {"claims": [{"claim_id": "c1", "stance": "neutral"}]},
    }
    assert compute_stance_macro_f1(gold, perfect) == 1.0

    # Missing predicted claim defaults to neutral
    partial = {
        "x:1": {"claims": [{"claim_id": "c1", "stance": "supports"}]},
        "x:2": {"claims": []},  # defaults to neutral -> FN for contradicts, FP for neutral
        "x:3": {"claims": [{"claim_id": "c1", "stance": "neutral"}]},
    }
    # supports F1 = 1.0, contradicts F1 = 0.0, neutral F1 = 2/3 -> macro = (1 + 0 + 2/3) / 3 = 5/9
    assert compute_stance_macro_f1(gold, partial) == pytest.approx(5 / 9, abs=1e-4)
    assert compute_stance_macro_f1([], {}) is None


def test_compute_mean_agreement_from_float_and_votes() -> None:
    gold = [
        {"item_id": "x:1", "claims": [{"claim_id": "c1", "stance": "supports"}]},
        {"item_id": "x:2", "claims": [{"claim_id": "c1", "stance": "contradicts"}]},
    ]
    enrichments = {
        "x:1": {"claims": [{"claim_id": "c1", "stance": "supports", "agreement": 1.0}]},
        "x:2": {
            "claims": [
                {
                    "claim_id": "c1",
                    "stance": "contradicts",
                    "votes": ["contradicts", "contradicts", "neutral"],
                }
            ]
        },
    }
    mean_agr = compute_mean_agreement(gold, enrichments)
    assert mean_agr == pytest.approx((1.0 + 2 / 3) / 2, abs=1e-4)
    assert compute_mean_agreement([], {}) is None


def test_compute_cohens_kappa_cases() -> None:
    # Perfect agreement across 2 classes
    perfect = [
        {"annotator_a": "s1", "annotator_b": "s1"},
        {"annotator_a": "s2", "annotator_b": "s2"},
        {"annotator_a": "s1", "annotator_b": "s1"},
        {"annotator_a": "s2", "annotator_b": "s2"},
    ]
    assert compute_cohens_kappa(perfect) == 1.0

    # Dict-shaped annotator labels
    dict_rows = [
        {"annotator_a": {"storyline_id": "s1"}, "annotator_b": {"storyline_id": "s1"}},
        {"annotator_a": {"storyline_id": "s2"}, "annotator_b": {"storyline_id": "s2"}},
    ]
    assert compute_cohens_kappa(dict_rows) == 1.0

    # Unanimous single class -> 1.0
    unanimous = [
        {"annotator_a": "s1", "annotator_b": "s1"},
        {"annotator_a": "s1", "annotator_b": "s1"},
    ]
    assert compute_cohens_kappa(unanimous) == 1.0

    # Missing annotators -> None
    assert compute_cohens_kappa([{"item_id": "x:1"}]) is None


def test_compute_guardrail_summary_detects_violations() -> None:
    banned_token_a = "".join(("verd", "ict"))
    banned_token_b = "".join(("is_", "b", "ot"))
    enrichments = [
        {
            "item_id": "x:1",
            "text": "Exact quote appears right here in text.",
            "claims": [
                {
                    "claim_id": "claim-known",
                    "stance": "supports",
                    "evidence_span": "Exact quote appears",
                },
                {
                    "claim_id": "claim-unknown",
                    "stance": "contradicts",
                    "evidence_span": "hallucinated span not in text",
                },
            ],
            "note": f"bad {banned_token_a} and {banned_token_b}",
        }
    ]
    summary = compute_guardrail_summary(
        enrichments,
        known_claim_ids={"claim-known"},
    )
    assert summary["checked_spans"] == 2
    assert summary["verified_spans"] == 1
    assert summary["span_exact_match_rate"] == 0.5
    assert summary["total_claims"] == 2
    assert summary["unknown_claims"] == 1
    assert summary["unknown_claim_rate"] == 0.5
    assert summary["banned_label_count"] == 2


# ---------------------------------------------------------------------------
# End-to-End Evaluation & CLI Tests
# ---------------------------------------------------------------------------


def test_evaluate_gold_set_with_fixture_and_hinglish_subset() -> None:
    gold_items = load_gold_jsonl(FIXTURE_GOLD_PATH)
    enrichments = _sample_enrichments()
    result = evaluate_gold_set(
        event_id="synth-eval-event",
        gold_items=gold_items,
        enrichments=enrichments,
        known_claim_ids={"claim-throughput", "claim-open-license"},
        run_date="2026-11-20",
    )

    assert result["event_id"] == "synth-eval-event"
    assert result["run_date"] == "2026-11-20"
    assert result["gold_count"] == 8
    # 7 of 8 storyline assignments match (yt:9006 is mismatched)
    assert result["assignment_accuracy"] == pytest.approx(7 / 8, abs=1e-4)
    assert result["below_target"] is False
    assert result["note"] is None
    assert result["stance_macro_f1"] == 1.0
    assert result["mean_agreement"] is not None
    assert result["cohens_kappa"] is not None

    # Hinglish subset: x:9004 (hi-en), x:9005 (hi-Latn), yt:9006 (hi) -> 3 items, 2/3 match
    hinglish = result["hinglish"]
    assert hinglish["count"] == 3
    assert hinglish["assignment_accuracy"] == pytest.approx(2 / 3, abs=1e-4)
    assert hinglish["stance_macro_f1"] == 1.0
    assert hinglish["mean_agreement"] == pytest.approx((1.0 + 0.6667) / 2, abs=1e-4)

    # Guardrails clean on sample enrichments
    guardrails = result["guardrails"]
    assert guardrails["span_exact_match_rate"] == 1.0
    assert guardrails["unknown_claim_rate"] == 0.0
    assert guardrails["banned_label_count"] == 0


def test_cli_writes_timestamped_result_and_supports_dry_run(tmp_path: Path) -> None:
    event_path = _write_synthetic_event_yaml(tmp_path / "events", "synth-eval-event")
    enr_path = tmp_path / "enrichments.jsonl"
    enr_path.write_text(
        "\n".join(json.dumps(row) for row in _sample_enrichments()) + "\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "results"

    # Dry run does not write file
    rc_dry = eval_main(
        [
            "--event",
            str(event_path),
            "--gold",
            str(FIXTURE_GOLD_PATH),
            "--enrichments",
            str(enr_path),
            "--output-dir",
            str(out_dir),
            "--dry-run",
        ]
    )
    assert rc_dry == 0
    assert not out_dir.exists()

    # Real run writes <event_id>_<YYYY-MM-DD>.json
    rc_real = eval_main(
        [
            "--event",
            str(event_path),
            "--gold",
            str(FIXTURE_GOLD_PATH),
            "--enrichments",
            str(enr_path),
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc_real == 0
    written = list(out_dir.glob("synth-eval-event_*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["event_id"] == "synth-eval-event"
    assert payload["gold_count"] == 8
    assert payload["assignment_accuracy"] == pytest.approx(7 / 8, abs=1e-4)
    assert "hinglish" in payload


def test_load_enrichments_snapshot_json_attaches_item_text(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snap.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "synthetic": True,
                "items": [{"item_id": "x:1", "text": "Hello verbatim span world"}],
                "enrichments": [
                    {
                        "item_id": "x:1",
                        "storyline_id": "pricing-claims",
                        "claims": [
                            {
                                "claim_id": "claim-throughput",
                                "stance": "supports",
                                "evidence_span": "verbatim span",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rows = load_enrichments_file(snapshot_path)
    assert len(rows) == 1
    assert rows[0]["text"] == "Hello verbatim span world"


def test_cli_returns_nonzero_when_gold_missing(tmp_path: Path) -> None:
    event_path = _write_synthetic_event_yaml(tmp_path / "events", "synth-eval-event")
    rc = eval_main(
        [
            "--event",
            str(event_path),
            "--gold",
            str(tmp_path / "missing.jsonl"),
            "--enrichments",
            str(tmp_path / "also_missing.jsonl"),
        ]
    )
    assert rc != 0


# ---------------------------------------------------------------------------
# /trust EVAL_RESULTS_DIR Wiring Tests
# ---------------------------------------------------------------------------


def test_trust_reads_latest_eval_result_from_env_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_dir = tmp_path / "events"
    _write_synthetic_event_yaml(events_dir, "synth-eval-event")
    results_dir = tmp_path / "eval_results"
    results_dir.mkdir(parents=True, exist_ok=True)

    # Older run
    (results_dir / "synth-eval-event_2026-11-18.json").write_text(
        json.dumps(
            {
                "event_id": "synth-eval-event",
                "run_date": "2026-11-18",
                "assignment_accuracy": 0.65,
                "stance_macro_f1": 0.58,
            }
        ),
        encoding="utf-8",
    )
    # Newer run
    (results_dir / "synth-eval-event_2026-11-20.json").write_text(
        json.dumps(
            {
                "event_id": "synth-eval-event",
                "run_date": "2026-11-20",
                "assignment_accuracy": 0.875,
                "stance_macro_f1": 0.8125,
            }
        ),
        encoding="utf-8",
    )
    # Corrupt file that should be safely ignored
    (results_dir / "synth-eval-event_2026-11-21.json").write_text("{bad json", encoding="utf-8")

    monkeypatch.setenv("EVAL_RESULTS_DIR", str(results_dir))
    client = TestClient(
        create_app(
            Settings(
                events_dir=str(events_dir),
                graph_backend="frames",
                sse_heartbeat_seconds=0.1,
            )
        )
    )
    resp = client.get("/events/synth-eval-event/trust")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gold"] == {
        "assignment_accuracy": 0.875,
        "stance_macro_f1": 0.8125,
        "run_date": "2026-11-20",
    }


def test_trust_returns_null_gold_when_no_eval_result_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_dir = tmp_path / "events"
    _write_synthetic_event_yaml(events_dir, "synth-no-eval")
    empty_results = tmp_path / "empty_results"
    empty_results.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EVAL_RESULTS_DIR", str(empty_results))

    client = TestClient(
        create_app(
            Settings(
                events_dir=str(events_dir),
                graph_backend="frames",
                sse_heartbeat_seconds=0.1,
            )
        )
    )
    resp = client.get("/events/synth-no-eval/trust")
    assert resp.status_code == 200
    assert resp.json()["gold"] is None


def test_trust_prefers_snapshot_gold_override_over_eval_results_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_dir = tmp_path / "events"
    _write_synthetic_event_yaml(events_dir, "synth-override")
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    (frames_dir / "synth-override.snapshot.json").write_text(
        json.dumps(
            {
                "synthetic": True,
                "gold": {
                    "assignment_accuracy": 0.91,
                    "stance_macro_f1": 0.88,
                    "run_date": "2026-11-19",
                },
            }
        ),
        encoding="utf-8",
    )
    results_dir = tmp_path / "eval_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "synth-override_2026-11-20.json").write_text(
        json.dumps(
            {
                "event_id": "synth-override",
                "run_date": "2026-11-20",
                "assignment_accuracy": 0.55,
                "stance_macro_f1": 0.50,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EVAL_RESULTS_DIR", str(results_dir))

    store = FramesStore(frames_dir=frames_dir)
    trust = store.get_trust("synth-override", {"sources": {}})
    assert trust["gold"]["assignment_accuracy"] == 0.91
    assert trust["gold"]["run_date"] == "2026-11-19"


def test_load_latest_eval_gold_rejects_unsafe_event_id(tmp_path: Path) -> None:
    assert _load_latest_eval_gold("../escape", eval_results_dir=tmp_path) is None
