"""Unit and integration tests for Spec 06: enrich job, schemas, and write_rows."""

from __future__ import annotations

import copy
import json
import logging
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from verifact_core.bq import write_rows  # noqa: E402
from verifact_core.config import EnrichmentConfig, EventConfig, load_event  # noqa: E402
from verifact_core.models import Rejection, WriteResult  # noqa: E402

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
INFRA_BQ_DIR = REPO_ROOT / "infra" / "bigquery"
ENRICH_DIR = REPO_ROOT / "services" / "enrich"


def _load_fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    assert data.get("synthetic") is True, f"{name} must be marked synthetic: true"
    return data


def _write_event_yaml(tmp_path: Path, cfg_dict: dict[str, Any]) -> Path:
    path = tmp_path / f"{cfg_dict['event_id']}.yaml"
    path.write_text(yaml.safe_dump(cfg_dict, sort_keys=False), encoding="utf-8")
    return path


def _unit_vec(dim: int, hot_index: int, secondary_index: int | None = None, secondary_val: float = 0.0) -> list[float]:
    vec = [0.0] * dim
    vec[hot_index] = 1.0
    if secondary_index is not None:
        vec[secondary_index] = secondary_val
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec]


class FakeFirestoreDocSnapshot:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = copy.deepcopy(data) if data is not None else None

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeFirestoreDocRef:
    def __init__(self, store: dict[str, dict[str, Any]], key: str) -> None:
        self._store = store
        self._key = key

    def get(self) -> FakeFirestoreDocSnapshot:
        return FakeFirestoreDocSnapshot(self._store.get(self._key))

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        if merge and self._key in self._store:
            updated = dict(self._store[self._key])
            updated.update(data)
            self._store[self._key] = updated
        else:
            self._store[self._key] = dict(data)


class FakeFirestoreCollection:
    def __init__(self, store: dict[str, dict[str, Any]], prefix: str) -> None:
        self._store = store
        self._prefix = prefix

    def document(self, doc_id: str) -> FakeFirestoreDocRef:
        return FakeFirestoreDocRef(self._store, f"{self._prefix}/{doc_id}")


class FakeFirestoreClient:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> FakeFirestoreCollection:
        return FakeFirestoreCollection(self.store, name)


class FakeBigQueryClient:
    """In-memory BigQuery fake supporting parameterized queries and insert_rows_json."""

    def __init__(
        self,
        *,
        items: list[dict[str, Any]] | None = None,
        enrichments: list[dict[str, Any]] | None = None,
        storyline_centres: list[dict[str, Any]] | None = None,
        account_map: list[dict[str, Any]] | None = None,
        raw_items_official_authors: list[str] | None = None,
    ) -> None:
        self.project = "test-proj"
        self.items: list[dict[str, Any]] = copy.deepcopy(items or [])
        self.enrichments: list[dict[str, Any]] = copy.deepcopy(enrichments or [])
        self.storyline_centres: list[dict[str, Any]] = copy.deepcopy(storyline_centres or [])
        self.account_map: list[dict[str, Any]] = copy.deepcopy(account_map or [])
        self.raw_items_official_authors: list[str] = list(raw_items_official_authors or [])
        self.insert_calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.queries: list[str] = []

    def insert_rows_json(self, table: str, json_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        copied = copy.deepcopy(json_rows)
        self.insert_calls.append((table, copied))
        short_table = table.split(".")[-1]
        if short_table == "enrichments":
            self.enrichments.extend(copied)
        elif short_table == "storyline_centres":
            self.storyline_centres.extend(copied)
        elif short_table == "account_map":
            self.account_map.extend(copied)
        return []

    def query(self, sql: str, job_config: Any | None = None) -> Any:
        self.queries.append(sql)
        params: dict[str, Any] = {}
        if job_config is not None and getattr(job_config, "query_parameters", None):
            for p in job_config.query_parameters:
                params[p.name] = getattr(p, "value", getattr(p, "values", None))

        event_id = params.get("event_id")
        rows: list[dict[str, Any]] = []

        if "storyline_centres" in sql:
            rows = [
                r for r in self.storyline_centres
                if event_id is None or r.get("event_id") == event_id
            ]
        elif "account_map" in sql:
            seen: dict[tuple[str, str], int] = {}
            for r in self.account_map:
                if event_id is not None and r.get("event_id") != event_id:
                    continue
                key = (r["platform"], r["author_ref"])
                if key not in seen or r["account_n"] < seen[key]:
                    seen[key] = int(r["account_n"])
            rows = [
                {"platform": plat, "author_ref": aref, "account_n": n}
                for (plat, aref), n in seen.items()
            ]
        elif "raw_items" in sql:
            rows = [{"author_ref": a} for a in self.raw_items_official_authors]
        elif ".items`" in sql or "FROM `test-proj.verifact.items`" in sql or "items`" in sql:
            prompt_version = params.get("prompt_version")
            limit = int(params.get("limit", 500))
            enriched_ids = {
                r["item_id"]
                for r in self.enrichments
                if (event_id is None or r.get("event_id") == event_id)
                and (prompt_version is None or r.get("prompt_version") == prompt_version)
            }
            include_enriched = "LEFT JOIN" not in sql
            filtered = [
                r for r in self.items
                if (event_id is None or r.get("event_id") == event_id)
                and (include_enriched or r["item_id"] not in enriched_ids)
            ]
            filtered.sort(key=lambda r: str(r.get("created_at", "")))
            rows = filtered[:limit]

        job = MagicMock()
        job.result.return_value = rows
        return job


class FakeGenAIClient:
    """Deterministic offline Vertex AI fake counting embedding and LLM calls."""

    def __init__(
        self,
        *,
        dim: int = 8,
        embed_fn: Any | None = None,
        stance_fn: Any | None = None,
    ) -> None:
        self.dim = dim
        self.embed_calls: list[dict[str, Any]] = []
        self.generate_calls: list[dict[str, Any]] = []
        self._embed_fn = embed_fn
        self._stance_fn = stance_fn
        self.models = SimpleNamespace(
            embed_content=self._embed_content,
            generate_content=self._generate_content,
        )

    def _embed_content(self, *, model: str, contents: str, config: Any = None) -> Any:
        self.embed_calls.append({"model": model, "contents": contents, "config": config})
        if self._embed_fn is not None:
            vec = self._embed_fn(contents)
        else:
            lower = contents.lower()
            if "pricing" in lower or "price" in lower or "token" in lower or "nova api" in lower:
                vec = _unit_vec(self.dim, 0, 1, 0.2)
            elif "hardware" in lower or "cluster" in lower or "grant" in lower or "funding" in lower:
                vec = _unit_vec(self.dim, 1, 0, 0.2)
            else:
                vec = _unit_vec(self.dim, 2)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=vec)])

    def _generate_content(self, *, model: str, contents: str, config: Any = None) -> Any:
        self.generate_calls.append({"model": model, "contents": contents, "config": config})
        if self._stance_fn is not None:
            payload = self._stance_fn(contents, len(self.generate_calls))
        else:
            payload = {"lang": "en", "claims": []}
        usage = SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=30,
            total_token_count=130,
        )
        return SimpleNamespace(text=json.dumps(payload), usage_metadata=usage)


def _base_env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    defaults: dict[str, str | None] = {
        "GCP_PROJECT": "test-proj",
        "GCP_REGION": "asia-south1",
        "BQ_DATASET": "verifact",
        "GEMINI_MODEL": "gemini-flash-lite-test",
        "EMBEDDING_MODEL": "gemini-embedding-001",
        "EMBEDDING_DIM": "8",
        "PROMPT_VERSION": "v1",
        "ENRICH_MAX_ITEMS_PER_RUN": "500",
        "ENRICH_DAILY_CALL_CAP": "100",
        "LLM_CONCURRENCY": "4",
        "EMBED_CONCURRENCY": "4",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# Config, write_rows, BigQuery schemas, and create.sh tests
# ---------------------------------------------------------------------------


def test_enrichment_config_optional_and_parsed(tmp_path: Path) -> None:
    fixture = _load_fixture("enrich_items.json")
    cfg_path = _write_event_yaml(tmp_path, fixture["event_config"])
    cfg = load_event(cfg_path)
    assert isinstance(cfg.enrichment, EnrichmentConfig)
    assert cfg.enrichment.assign_threshold == 0.75
    assert cfg.enrichment.offtopic_floor == 0.45

    without_enrichment = copy.deepcopy(fixture["event_config"])
    del without_enrichment["enrichment"]
    cfg_path2 = _write_event_yaml(tmp_path, without_enrichment)
    cfg2 = load_event(cfg_path2)
    assert cfg2.enrichment is None


def test_write_rows_batches_retries_and_logs_rejections(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("GCP_PROJECT", "test-proj")
    monkeypatch.setenv("BQ_DATASET", "verifact")
    monkeypatch.setattr("verifact_core.bq.time.sleep", lambda _: None)

    mock_client = MagicMock()
    mock_client.insert_rows_json.return_value = []
    rows = [{"item_id": f"x:{i}", "event_id": "demo-launch"} for i in range(600)]
    res = write_rows("enrichments", rows, client=mock_client)
    assert res == WriteResult(written=600, rejected=[])
    assert mock_client.insert_rows_json.call_count == 2
    assert mock_client.insert_rows_json.call_args_list[0].args[0] == "test-proj.verifact.enrichments"
    assert [len(c.args[1]) for c in mock_client.insert_rows_json.call_args_list] == [500, 100]

    # Partial rejection logs bq_insert_error and retries only failed row
    fail_client = MagicMock()
    fail_client.insert_rows_json.side_effect = [
        [{"index": 1, "errors": [{"reason": "invalid", "message": "bad field"}]}],
        [{"index": 0, "errors": [{"reason": "invalid", "message": "bad field"}]}],
        [{"index": 0, "errors": [{"reason": "invalid", "message": "bad field"}]}],
    ]
    with caplog.at_level(logging.ERROR):
        res2 = write_rows(
            "enrichments",
            [{"item_id": "x:1"}, {"item_id": "x:bad"}],
            client=fail_client,
        )
    assert res2.written == 1
    assert res2.rejected == [
        Rejection(source="enrichments", source_id="x:bad", reason="invalid: bad field")
    ]
    assert any("bq_insert_error" in rec.getMessage() for rec in caplog.records)

    # Empty list makes zero calls
    empty_client = MagicMock()
    assert write_rows("enrichments", [], client=empty_client) == WriteResult(written=0, rejected=[])
    empty_client.insert_rows_json.assert_not_called()


def test_bigquery_schema_files_and_create_sh_idempotency(tmp_path: Path) -> None:
    enrich_schema = json.loads((INFRA_BQ_DIR / "enrichments.json").read_text(encoding="utf-8"))
    enrich_fields = {f["name"]: f for f in enrich_schema["schema"]["fields"]}
    for required_col in (
        "item_id",
        "event_id",
        "enriched_at",
        "model_id",
        "embedding_model_id",
        "prompt_version",
        "lang",
        "embedding",
        "storyline_id",
        "storyline_similarity",
        "second_storyline_id",
        "second_similarity",
        "off_topic",
        "claims",
        "entities",
        "is_official",
        "conflict_person",
    ):
        assert required_col in enrich_fields, f"Missing {required_col} in enrichments.json"
    assert enrich_schema["timePartitioning"] == {"type": "DAY", "field": "enriched_at"}
    assert enrich_schema["clustering"] == {"fields": ["event_id", "storyline_id"]}

    centres_schema = json.loads((INFRA_BQ_DIR / "storyline_centres.json").read_text(encoding="utf-8"))
    centres_names = [f["name"] for f in centres_schema["schema"]["fields"]]
    assert centres_names == [
        "event_id",
        "storyline_id",
        "embedding",
        "seed_hash",
        "embedding_model_id",
        "computed_at",
    ]
    assert centres_schema["clustering"] == {"fields": ["event_id"]}

    acct_schema = json.loads((INFRA_BQ_DIR / "account_map.json").read_text(encoding="utf-8"))
    acct_names = [f["name"] for f in acct_schema["schema"]["fields"]]
    assert acct_names == ["event_id", "platform", "author_ref", "account_n", "first_seen"]

    # Run create.sh twice against a fake bq CLI and confirm all 3 new tables are created idempotently
    state_file = tmp_path / "bq_state.json"
    state_file.write_text(json.dumps({"datasets": [], "tables": []}), encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_bq = fake_bin / "bq"
    fake_bq.write_text(
        f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
state_path = Path({str(state_file)!r})
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
if "show" in args:
    target = args[-1]
    bucket = "datasets" if "--dataset" in args else "tables"
    sys.exit(0 if target in state[bucket] else 1)
elif "mk" in args:
    if "--dataset" in args:
        target = args[-1]
        if target in state["datasets"]:
            sys.exit(1)
        state["datasets"].append(target)
    elif "--table" in args:
        target = args[-2]
        if target in state["tables"]:
            sys.exit(1)
        state["tables"].append(target)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    sys.exit(0)
elif "query" in args:
    sys.exit(0)
sys.exit(1)
""",
        encoding="utf-8",
    )
    fake_bq.chmod(fake_bq.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["GCP_PROJECT"] = "test-proj"
    env["GCP_REGION"] = "asia-south1"
    env["BQ_DATASET"] = "verifact"

    for _ in range(2):
        proc = subprocess.run(
            ["bash", str(INFRA_BQ_DIR / "create.sh")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr

    final_state = json.loads(state_file.read_text(encoding="utf-8"))
    for tbl in ("raw_items", "event_t0", "enrichments", "storyline_centres", "account_map"):
        assert f"test-proj:verifact.{tbl}" in final_state["tables"]


# ---------------------------------------------------------------------------
# AC 1: Startup validation
# ---------------------------------------------------------------------------


def test_ac1_refuses_storyline_without_seeds_and_missing_thresholds_or_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from services.enrich.main import main

    fixture = _load_fixture("enrich_items.json")

    # 1a. Missing seeds on a storyline
    cfg_no_seeds = copy.deepcopy(fixture["event_config"])
    cfg_no_seeds["seed_storylines"][1]["seeds"] = []
    path_no_seeds = _write_event_yaml(tmp_path, cfg_no_seeds)
    _base_env(monkeypatch)
    code = main(["--event", str(path_no_seeds)])
    assert code != 0
    captured1 = capsys.readouterr()
    out = captured1.out + captured1.err
    assert "hardware-funding" in out

    # 1b. Missing thresholds (without --report-similarities)
    cfg_no_thresh = copy.deepcopy(fixture["event_config"])
    cfg_no_thresh["enrichment"] = {"assign_threshold": None, "offtopic_floor": None}
    path_no_thresh = _write_event_yaml(tmp_path, cfg_no_thresh)
    _base_env(monkeypatch)
    code2 = main(["--event", str(path_no_thresh)])
    assert code2 != 0
    captured2 = capsys.readouterr()
    combined2 = captured2.out + captured2.err
    assert "assign_threshold" in combined2
    assert "offtopic_floor" in combined2

    # 1c. Missing ENRICH_DAILY_CALL_CAP
    path_valid = _write_event_yaml(tmp_path, fixture["event_config"])
    _base_env(monkeypatch, ENRICH_DAILY_CALL_CAP=None)
    code3 = main(["--event", str(path_valid)])
    assert code3 != 0
    captured3 = capsys.readouterr()
    assert "ENRICH_DAILY_CALL_CAP" in (captured3.out + captured3.err)


# ---------------------------------------------------------------------------
# AC 2: --report-similarities prints histogram and writes nothing
# ---------------------------------------------------------------------------


def test_ac2_report_similarities_prints_histogram_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from services.enrich.main import EnrichSettings, run_enrich

    fixture = _load_fixture("enrich_items.json")
    cfg_dict = copy.deepcopy(fixture["event_config"])
    # Thresholds intentionally unset — allowed with --report-similarities
    cfg_dict["enrichment"] = None
    cfg = EventConfig.model_validate(cfg_dict)

    _base_env(monkeypatch)
    settings = EnrichSettings()
    bq = FakeBigQueryClient(items=fixture["items"])
    fs = FakeFirestoreClient()
    genai = FakeGenAIClient(dim=8)

    summary = run_enrich(
        cfg,
        settings,
        limit=10,
        report_similarities=True,
        bq_client=bq,
        fs_client=fs,
        genai_client=genai,
    )

    out = capsys.readouterr().out
    assert "pricing-and-speed" in out
    assert "hardware-funding" in out
    assert "Histogram" in out or "histogram" in out.lower()
    assert bq.insert_calls == []
    assert bq.enrichments == []
    assert bq.storyline_centres == []
    assert bq.account_map == []
    assert len(genai.generate_calls) == 0
    assert summary.report_similarities is True


# ---------------------------------------------------------------------------
# AC 3: Second run with no new items makes zero model/embedding calls
# ---------------------------------------------------------------------------


def test_ac3_second_run_with_no_new_items_makes_zero_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.enrich.main import EnrichSettings, run_enrich

    fixture = _load_fixture("enrich_items.json")
    cfg = EventConfig.model_validate(fixture["event_config"])
    _base_env(monkeypatch)
    settings = EnrichSettings()

    bq = FakeBigQueryClient(items=fixture["items"][:2])
    fs = FakeFirestoreClient()
    genai = FakeGenAIClient(dim=8)

    run1 = run_enrich(cfg, settings, bq_client=bq, fs_client=fs, genai_client=genai)
    assert run1.items_done == 2
    assert len(bq.storyline_centres) == 2

    calls_after_run1_embed = len(genai.embed_calls)
    calls_after_run1_gen = len(genai.generate_calls)
    assert calls_after_run1_embed > 0
    assert calls_after_run1_gen > 0

    # Second run: all items already enriched & storyline centres cached by seed_hash
    run2 = run_enrich(cfg, settings, bq_client=bq, fs_client=fs, genai_client=genai)
    assert run2.items_done == 0
    assert run2.calls_made == 0
    assert len(genai.embed_calls) == calls_after_run1_embed
    assert len(genai.generate_calls) == calls_after_run1_gen


# ---------------------------------------------------------------------------
# AC 4: 10 identical texts -> 1 embedding call + 3 stance calls
# ---------------------------------------------------------------------------


def test_ac4_ten_identical_texts_make_one_embed_and_three_stance_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.enrich.main import EnrichSettings, compute_seed_hash, run_enrich

    fixture = _load_fixture("enrich_items.json")
    cfg = EventConfig.model_validate(fixture["event_config"])
    _base_env(monkeypatch)
    settings = EnrichSettings()

    # Pre-seed storyline_centres so we count only item calls cleanly
    centres = [
        {
            "event_id": cfg.event_id,
            "storyline_id": "pricing-and-speed",
            "embedding": _unit_vec(8, 0),
            "seed_hash": compute_seed_hash(cfg.seed_storylines[0].seeds, settings.embedding_model, 8),
            "embedding_model_id": settings.embedding_model,
            "computed_at": "2026-11-16T12:00:00Z",
        },
        {
            "event_id": cfg.event_id,
            "storyline_id": "hardware-funding",
            "embedding": _unit_vec(8, 1),
            "seed_hash": compute_seed_hash(cfg.seed_storylines[1].seeds, settings.embedding_model, 8),
            "embedding_model_id": settings.embedding_model,
            "computed_at": "2026-11-16T12:00:00Z",
        },
    ]

    identical_text = "NovaEngine pricing is 10 credits per million tokens on privately funded clusters."
    ten_items = [
        {
            "item_id": f"x:2000{i}",
            "event_id": cfg.event_id,
            "platform": "x",
            "kind": "post",
            "url": f"https://x.com/i/status/2000{i}",
            "author_ref": f"800{i}",
            "created_at": f"2026-11-16T12:{i:02d}:00Z",
            "collected_at": f"2026-11-16T12:{i:02d}:30Z",
            "text": identical_text,
            "lang": "en",
        }
        for i in range(10)
    ]

    bq = FakeBigQueryClient(items=ten_items, storyline_centres=centres)
    fs = FakeFirestoreClient()
    genai = FakeGenAIClient(dim=8)

    summary = run_enrich(cfg, settings, bq_client=bq, fs_client=fs, genai_client=genai)
    assert summary.items_done == 10
    assert len(bq.enrichments) == 10
    assert len(genai.embed_calls) == 1
    assert len(genai.generate_calls) == 3


# ---------------------------------------------------------------------------
# AC 5 & AC 6: Span verification, unknown claim_id, and 2-of-3 majority voting
# ---------------------------------------------------------------------------


def test_ac5_drops_invalid_evidence_span_and_unknown_claim_id() -> None:
    from services.enrich.main import verify_and_aggregate_claims

    responses = _load_fixture("enrich_responses.json")["stance_bad_span_and_unknown_claim"]
    item_text = "DemoCorp says NovaEngine uses privately funded clusters."
    valid_ids = {"private-clusters", "launch-price-10"}

    claims = verify_and_aggregate_claims(
        item_text,
        [responses["a"], responses["b"], responses["c"]],
        valid_ids,
    )
    assert claims == []

    # When run A has bad span but run B has valid span, keep claim with run B's valid span
    partial = [
        {
            "lang": "en",
            "claims": [
                {"claim_id": "private-clusters", "stance": "supports", "evidence_span": "not in text"}
            ],
        },
        {
            "lang": "en",
            "claims": [
                {"claim_id": "private-clusters", "stance": "supports", "evidence_span": "privately funded clusters"}
            ],
        },
        {"lang": "en", "claims": []},
    ]
    claims2 = verify_and_aggregate_claims(item_text, partial, valid_ids)
    assert len(claims2) == 1
    assert claims2[0]["evidence_span"] == "privately funded clusters"
    assert claims2[0]["evidence_span"] in item_text


def test_ac6_single_run_dropped_and_two_of_three_majority_kept() -> None:
    from services.enrich.main import verify_and_aggregate_claims

    fixtures = _load_fixture("enrich_responses.json")
    item_text = "NovaEngine runs on privately funded clusters at 10 credits per million tokens."
    valid_ids = {"private-clusters", "launch-price-10"}

    # 1 of 3 -> dropped
    single = fixtures["stance_single_run_only"]
    assert verify_and_aggregate_claims(
        item_text, [single["a"], single["b"], single["c"]], valid_ids
    ) == []

    # 2 of 3 agreeing -> stored with agreement 0.67 and absent for 3rd
    majority = fixtures["stance_valid_majority"]
    kept = verify_and_aggregate_claims(
        item_text, [majority["a"], majority["b"], majority["c"]], valid_ids
    )
    assert len(kept) == 1
    assert kept[0] == {
        "claim_id": "private-clusters",
        "stance": "supports",
        "evidence_span": "privately funded clusters",
        "votes": ["supports", "supports", "absent"],
        "agreement": 0.67,
    }


# ---------------------------------------------------------------------------
# AC 7: Prompt injection defence (behaviour case #7)
# ---------------------------------------------------------------------------


def test_ac7_prompt_injection_item_is_delimited_and_labelled_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.enrich.main import EnrichSettings, run_enrich

    fixture = _load_fixture("enrich_items.json")
    cfg = EventConfig.model_validate(fixture["event_config"])
    cfg_snapshot = cfg.model_dump()
    _base_env(monkeypatch)
    settings = EnrichSettings()

    inj_item = next(it for it in fixture["items"] if it["item_id"] == "x:100004")

    def _stance_fn(_prompt: str, _idx: int) -> dict[str, Any]:
        return {
            "lang": "en",
            "claims": [
                {
                    "claim_id": "private-clusters",
                    "stance": "supports",
                    "evidence_span": "privately funded clusters",
                }
            ],
        }

    bq = FakeBigQueryClient(items=[inj_item])
    fs = FakeFirestoreClient()
    genai = FakeGenAIClient(dim=8, stance_fn=_stance_fn)

    summary = run_enrich(cfg, settings, bq_client=bq, fs_client=fs, genai_client=genai)
    assert summary.items_done == 1
    assert len(bq.enrichments) == 1
    row = bq.enrichments[0]
    assert row["storyline_id"] == "hardware-funding"
    assert row["claims"][0]["claim_id"] == "private-clusters"
    assert row["claims"][0]["stance"] == "supports"
    assert row["claims"][0]["agreement"] == 1.0
    # Prompt must clearly delimit untrusted item text and declare it data, not instructions
    assert len(genai.generate_calls) == 3
    for call in genai.generate_calls:
        prompt = call["contents"]
        assert "<<<BEGIN_UNTRUSTED_ITEM_TEXT>>>" in prompt
        assert "<<<END_UNTRUSTED_ITEM_TEXT>>>" in prompt
        assert "data" in prompt.lower() and "instruction" in prompt.lower()
        assert call["config"].temperature == 0.0
    # Config unchanged
    assert cfg.model_dump() == cfg_snapshot


# ---------------------------------------------------------------------------
# AC 8: keywords.exclude -> off_topic & 0 stance calls
# ---------------------------------------------------------------------------


def test_ac8_exclude_keyword_marks_off_topic_and_skips_stance_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.enrich.main import EnrichSettings, run_enrich

    fixture = _load_fixture("enrich_items.json")
    cfg = EventConfig.model_validate(fixture["event_config"])
    _base_env(monkeypatch)
    settings = EnrichSettings()

    excluded_item = next(it for it in fixture["items"] if it["item_id"] == "hn:55001")
    # Even if embedding similarity is high to pricing-and-speed, exclude keyword forces off_topic
    def _embed_for_exclude(text: str) -> list[float]:
        lower = text.lower()
        if "hardware" in lower or "grant" in lower or "cluster" in lower:
            return _unit_vec(8, 1)
        return _unit_vec(8, 0, 1, 0.2)

    bq = FakeBigQueryClient(items=[excluded_item])
    fs = FakeFirestoreClient()
    genai = FakeGenAIClient(dim=8, embed_fn=_embed_for_exclude)

    summary = run_enrich(cfg, settings, bq_client=bq, fs_client=fs, genai_client=genai)
    assert summary.items_done == 1
    assert len(genai.generate_calls) == 0
    row = bq.enrichments[0]
    assert row["off_topic"] is True
    assert row["storyline_id"] == "unassigned"
    assert row["second_storyline_id"] == "hardware-funding"
    assert row["second_similarity"] is not None
    assert row["claims"] == []

    # Also test below offtopic_floor -> off_topic=True and 0 stance calls,
    # and between offtopic_floor and assign_threshold -> unassigned, off_topic=False
    floor_item = {
        "item_id": "hn:55002",
        "event_id": cfg.event_id,
        "platform": "hn",
        "kind": "comment",
        "url": "https://news.ycombinator.com/item?id=55002",
        "author_ref": "hn_user_2",
        "created_at": "2026-11-16T13:31:00Z",
        "collected_at": "2026-11-16T13:35:00Z",
        "text": "Completely unrelated discussion about gardening.",
        "lang": "en",
    }
    mid_item = {
        "item_id": "hn:55003",
        "event_id": cfg.event_id,
        "platform": "hn",
        "kind": "comment",
        "url": "https://news.ycombinator.com/item?id=55003",
        "author_ref": "hn_user_3",
        "created_at": "2026-11-16T13:32:00Z",
        "collected_at": "2026-11-16T13:35:00Z",
        "text": "Tangential mention of NovaEngine without a strong storyline match.",
        "lang": "en",
    }

    def _embed_for_floors(text: str) -> list[float]:
        lower = text.lower()
        if "gardening" in lower:
            return _unit_vec(8, 5)  # cosine 0.0 < 0.45 floor -> off_topic
        if "tangential" in lower:
            # cosine ~0.60 with pricing-and-speed (between 0.45 and 0.75) -> unassigned, not off_topic
            return _unit_vec(8, 0, 5, 1.3)
        if "hardware" in lower or "grant" in lower or "cluster" in lower:
            return _unit_vec(8, 1)
        return _unit_vec(8, 0)

    bq2 = FakeBigQueryClient(items=[floor_item, mid_item])
    genai2 = FakeGenAIClient(dim=8, embed_fn=_embed_for_floors)
    run_enrich(cfg, settings, bq_client=bq2, fs_client=FakeFirestoreClient(), genai_client=genai2)
    by_id2 = {r["item_id"]: r for r in bq2.enrichments}
    assert by_id2["hn:55002"]["off_topic"] is True
    assert by_id2["hn:55002"]["storyline_id"] == "unassigned"
    assert by_id2["hn:55003"]["off_topic"] is False
    assert by_id2["hn:55003"]["storyline_id"] == "unassigned"
    # Only mid_item gets 3 stance calls; floor_item gets 0
    assert len(genai2.generate_calls) == 3


# ---------------------------------------------------------------------------
# AC 9: Rule-based entities, is_official, conflict_person
# ---------------------------------------------------------------------------


def test_ac9_entities_is_official_and_conflict_person_string_rules_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.enrich.main import EnrichSettings, run_enrich

    fixture = _load_fixture("enrich_items.json")
    cfg = EventConfig.model_validate(fixture["event_config"])
    _base_env(monkeypatch)
    settings = EnrichSettings()

    items = [
        # Official via official.posts + query_id=official (author_ref 9001)
        fixture["items"][0],
        # Second post by same official X author_ref 9001 (not in official.posts)
        {
            "item_id": "x:100099",
            "event_id": cfg.event_id,
            "platform": "x",
            "kind": "reply",
            "url": "https://x.com/i/status/100099",
            "author_ref": "9001",
            "created_at": "2026-11-16T12:10:00Z",
            "collected_at": "2026-11-16T12:12:00Z",
            "text": "Follow-up clarification about NovaEngine latency.",
            "lang": "en",
        },
        # Official via official.accounts handle directly
        {
            "item_id": "hn:77001",
            "event_id": cfg.event_id,
            "platform": "hn",
            "kind": "comment",
            "url": "https://news.ycombinator.com/item?id=77001",
            "author_ref": "DemoCorp",
            "created_at": "2026-11-16T12:12:00Z",
            "collected_at": "2026-11-16T12:14:00Z",
            "text": "Answering hardware questions here.",
            "lang": "en",
        },
        # Official via spokesperson in text AND conflict_person in text
        fixture["items"][2],
        # Conflict_person via author matching conflict person
        {
            "item_id": "hn:77002",
            "event_id": cfg.event_id,
            "platform": "hn",
            "kind": "comment",
            "url": "https://news.ycombinator.com/item?id=77002",
            "author_ref": "Vikram Rao",
            "created_at": "2026-11-16T13:05:00Z",
            "collected_at": "2026-11-16T13:06:00Z",
            "text": "Excited about Nova API compared to AlphaModel.",
            "lang": "en",
        },
        # Regular third-party post mentioning aliases -> entities matched, not official, no conflict
        fixture["items"][1],
    ]

    # Model attempts to smuggle extra fields (must be completely ignored)
    def _smuggling_stance(_p: str, _i: int) -> dict[str, Any]:
        return {
            "lang": "en",
            "is_official": True,
            "conflict_person": "Model Hallucinated Person",
            "claims": [],
        }

    bq = FakeBigQueryClient(items=items, raw_items_official_authors=["9001"])
    fs = FakeFirestoreClient()
    genai = FakeGenAIClient(dim=8, stance_fn=_smuggling_stance)

    run_enrich(cfg, settings, bq_client=bq, fs_client=fs, genai_client=genai)
    by_id = {r["item_id"]: r for r in bq.enrichments}

    assert by_id["x:100001"]["is_official"] is True
    assert by_id["x:100099"]["is_official"] is True
    assert by_id["hn:77001"]["is_official"] is True
    assert by_id["news:abc123"]["is_official"] is True  # Priya Nair spokesperson
    assert by_id["news:abc123"]["conflict_person"] == "Vikram Rao"  # named in text
    assert by_id["news:abc123"]["lang"] == "en"  # GDELT "English" mapped to BCP-47
    assert by_id["hn:77002"]["is_official"] is False
    assert by_id["hn:77002"]["conflict_person"] == "Vikram Rao"  # author matches conflict
    assert set(by_id["hn:77002"]["entities"]) == {"NovaEngine", "AlphaModel"}
    assert by_id["x:100002"]["is_official"] is False
    assert by_id["x:100002"]["conflict_person"] is None
    assert set(by_id["x:100002"]["entities"]) == {"DemoCorp", "NovaEngine", "AlphaModel", "CloudBench"}


# ---------------------------------------------------------------------------
# AC 10: Metadata fields, embedding dimension, and empty-text handling
# ---------------------------------------------------------------------------


def test_ac10_required_metadata_embedding_dim_and_empty_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.enrich.main import EnrichSettings, resolve_language, run_enrich

    fixture = _load_fixture("enrich_items.json")
    cfg = EventConfig.model_validate(fixture["event_config"])
    _base_env(monkeypatch, EMBEDDING_DIM="16", GEMINI_MODEL="gemini-2.5-flash-lite-preview")
    settings = EnrichSettings()

    items = [fixture["items"][0], fixture["items"][4]]  # one normal, one empty text
    bq = FakeBigQueryClient(items=items)
    fs = FakeFirestoreClient()
    genai = FakeGenAIClient(dim=16)

    run_enrich(cfg, settings, bq_client=bq, fs_client=fs, genai_client=genai)
    by_id = {r["item_id"]: r for r in bq.enrichments}

    for row in bq.enrichments:
        assert row["model_id"] == "gemini-2.5-flash-lite-preview"
        assert row["embedding_model_id"] == "gemini-embedding-001"
        assert row["prompt_version"] == "v1"

    normal_row = by_id["x:100001"]
    assert len(normal_row["embedding"]) == 16

    empty_row = by_id["x:100003"]
    assert empty_row["lang"] == "und"
    assert empty_row["storyline_id"] == "unassigned"
    assert empty_row["claims"] == []
    assert empty_row["embedding"] == []

    # Language helper checks
    assert resolve_language("English", "hi") == "en"
    assert resolve_language("und", "hi-Latn") == "hi-en"
    assert resolve_language(None, "hi-en") == "hi-en"
    assert resolve_language(None, None) == "und"


# ---------------------------------------------------------------------------
# AC 11: Daily call cap stops cleanly with cap_reached per UTC day
# ---------------------------------------------------------------------------


def test_ac11_daily_call_cap_stops_with_cap_reached_for_utc_day(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from services.enrich.main import EnrichSettings, compute_seed_hash, run_enrich

    fixture = _load_fixture("enrich_items.json")
    cfg = EventConfig.model_validate(fixture["event_config"])
    # Cap of 4 allows 1 item (1 embed + 3 stance = 4 calls), blocks 2nd item
    _base_env(monkeypatch, ENRICH_DAILY_CALL_CAP="4")
    settings = EnrichSettings()

    centres = [
        {
            "event_id": cfg.event_id,
            "storyline_id": "pricing-and-speed",
            "embedding": _unit_vec(8, 0),
            "seed_hash": compute_seed_hash(cfg.seed_storylines[0].seeds, settings.embedding_model, 8),
            "embedding_model_id": settings.embedding_model,
            "computed_at": "2026-11-16T12:00:00Z",
        },
        {
            "event_id": cfg.event_id,
            "storyline_id": "hardware-funding",
            "embedding": _unit_vec(8, 1),
            "seed_hash": compute_seed_hash(cfg.seed_storylines[1].seeds, settings.embedding_model, 8),
            "embedding_model_id": settings.embedding_model,
            "computed_at": "2026-11-16T12:00:00Z",
        },
    ]

    bq = FakeBigQueryClient(items=fixture["items"][:2], storyline_centres=centres)
    fs = FakeFirestoreClient()
    genai = FakeGenAIClient(dim=8)
    day1 = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)

    with caplog.at_level(logging.INFO):
        run1 = run_enrich(
            cfg,
            settings,
            bq_client=bq,
            fs_client=fs,
            genai_client=genai,
            now_fn=lambda: day1,
        )

    assert run1.cap_reached is True
    assert run1.items_done == 1
    assert run1.calls_made == 4
    assert any("cap_reached" in rec.getMessage() for rec in caplog.records)
    assert any("enrich_run" in rec.getMessage() for rec in caplog.records)

    # Same UTC day: second run must make 0 calls
    run2 = run_enrich(
        cfg,
        settings,
        bq_client=bq,
        fs_client=fs,
        genai_client=genai,
        now_fn=lambda: day1,
    )
    assert run2.cap_reached is True
    assert run2.calls_made == 0
    assert len(genai.embed_calls) == 1
    assert len(genai.generate_calls) == 3

    # Next UTC day: cap resets and remaining item is enriched
    day2 = datetime(2026, 11, 21, 0, 1, 0, tzinfo=timezone.utc)
    run3 = run_enrich(
        cfg,
        settings,
        bq_client=bq,
        fs_client=fs,
        genai_client=genai,
        now_fn=lambda: day2,
    )
    assert run3.cap_reached is False
    assert run3.items_done == 1
    assert len(bq.enrichments) == 2


# ---------------------------------------------------------------------------
# AC 12: Stable account_map numbering across runs
# ---------------------------------------------------------------------------


def test_ac12_account_map_assigns_stable_account_n_across_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.enrich.main import EnrichSettings, run_enrich

    fixture = _load_fixture("enrich_items.json")
    cfg = EventConfig.model_validate(fixture["event_config"])
    _base_env(monkeypatch)
    settings = EnrichSettings()

    item1 = fixture["items"][0]  # x, author 9001
    item2 = fixture["items"][1]  # x, author 9002
    bq = FakeBigQueryClient(items=[item1, item2])
    fs = FakeFirestoreClient()
    genai = FakeGenAIClient(dim=8)

    run_enrich(cfg, settings, bq_client=bq, fs_client=fs, genai_client=genai)
    assert [(r["platform"], r["author_ref"], r["account_n"]) for r in bq.account_map] == [
        ("x", "9001", 1),
        ("x", "9002", 2),
    ]

    # Second run with repeat author 9001 and new author pressdaily-desk
    repeat_item = copy.deepcopy(item1)
    repeat_item["item_id"] = "x:100999"
    repeat_item["created_at"] = "2026-11-16T12:50:00Z"
    new_author_item = fixture["items"][2]  # news, pressdaily-desk
    bq.items.extend([repeat_item, new_author_item])

    run_enrich(cfg, settings, bq_client=bq, fs_client=fs, genai_client=genai)
    assert [(r["platform"], r["author_ref"], r["account_n"]) for r in bq.account_map] == [
        ("x", "9001", 1),
        ("x", "9002", 2),
        ("news", "pressdaily-desk", 3),
    ]


# ---------------------------------------------------------------------------
# AC 13 & AC 14: Event-agnostic & no live Vertex AI calls
# ---------------------------------------------------------------------------


def test_ac13_no_event_specific_literals_in_enrich_service() -> None:
    forbidden = ("sarvam", "pratyush", "vivek raghavan", "token factory", "hemant mohapatra")
    files = list(ENRICH_DIR.rglob("*"))
    assert len(files) > 0
    for path in files:
        if path.is_file() and "__pycache__" not in path.parts:
            content = path.read_text(encoding="utf-8").lower()
            for term in forbidden:
                assert term not in content, f"Event-specific literal {term!r} found in {path}"

    # Verify all 3 prompt paraphrases exist, differ from each other, and use placeholders
    prompts = [
        (ENRICH_DIR / "prompts" / f"stance_v1_{v}.txt").read_text(encoding="utf-8")
        for v in ("a", "b", "c")
    ]
    assert len(set(prompts)) == 3
    for p in prompts:
        assert "{claims_list}" in p
        assert "{item_text}" in p
        assert "<<<BEGIN_UNTRUSTED_ITEM_TEXT>>>" in p
        assert "<<<END_UNTRUSTED_ITEM_TEXT>>>" in p


def test_retry_on_429_and_dry_run_makes_zero_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.enrich.main import EnrichSettings, run_enrich

    sleeps: list[float] = []
    monkeypatch.setattr("services.enrich.main.time.sleep", lambda s: sleeps.append(s))

    fixture = _load_fixture("enrich_items.json")
    cfg = EventConfig.model_validate(fixture["event_config"])
    _base_env(monkeypatch)
    settings = EnrichSettings()

    attempts = {"embed": 0}

    def _flaky_embed(text: str) -> list[float]:
        attempts["embed"] += 1
        if attempts["embed"] == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: rate limit")
        return _unit_vec(8, 0)

    bq = FakeBigQueryClient(items=[fixture["items"][0]])
    fs = FakeFirestoreClient()
    genai = FakeGenAIClient(dim=8, embed_fn=_flaky_embed)

    summary = run_enrich(
        cfg, settings, dry_run=True, bq_client=bq, fs_client=fs, genai_client=genai
    )
    assert summary.dry_run is True
    assert summary.items_done == 1
    assert len(sleeps) == 1 and sleeps[0] > 0
    assert bq.insert_calls == []
    assert fs.store == {}

