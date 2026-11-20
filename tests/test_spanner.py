"""Tests for Spec 07: Spanner Graph DDL, setup.sh, mutations, upserts, and GQL queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
from typing import Any

import pytest

from verifact_core.config import EventConfig
from verifact_core.spanner_graph import (
    GraphBatch,
    UpsertResult,
    build_graph_mutations,
    query_graph_at_hour,
    upsert_graph_batch,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL_PATH = REPO_ROOT / "infra" / "spanner" / "schema.sql"
SETUP_SH_PATH = REPO_ROOT / "infra" / "spanner" / "setup.sh"

T0_ISO = "2026-11-16T12:00:00Z"
T0_DT = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)


def _make_event_config(
    *,
    min_followers_to_name: int | None = None,
) -> EventConfig:
    """Create a synthetic EventConfig fixture with zero event-specific literals."""
    return EventConfig.model_validate(
        {
            "event_id": "acme-launch-event",
            "name": "Acme Cloud Launch",
            "t0": {
                "utc": T0_ISO,
                "source_url": "https://x.com/AcmeOfficial/status/1001",
                "note": "Synthetic test event",
            },
            "window": {
                "pre_hours": 24,
                "post_hours": 72,
                "live": False,
            },
            "official": {
                "accounts": ["AcmeOfficial"],
                "posts": ["1001"],
                "urls": ["https://acme.example.com/blog/launch"],
                "spokespeople": ["Jane Doe"],
            },
            "entities": {
                "org": {"Acme": ["Acme Corp", "@AcmeOfficial"]},
                "product": {"CloudEngine": ["Acme CloudEngine"]},
                "related": ["OpenRouter"],
            },
            "keywords": {
                "core": ["acme cloud launch"],
                "context": ["gpu", "pricing"],
                "exclude": [],
            },
            "languages": ["en"],
            "sources": {
                "x": {"enabled": True, "max_posts": 500, "thread_ids": ["1001"]},
                "hn": {"enabled": True},
                "news": {"enabled": True, "gdelt": True, "google_news_rss": True},
            },
            "turning_points": {
                "detect_official_response": True,
                "manual": [
                    {
                        "id": "response-1",
                        "label": "Official pricing clarification",
                        "at_utc": "2026-11-16T18:00:00Z",
                        "confirmed": True,
                        "form": "x_post",
                    }
                ],
            },
            "seed_storylines": [
                {
                    "id": "pricing-claims",
                    "name": "Official pricing and throughput claims",
                    "seeds": ["Acme announced lower inference pricing."],
                },
                {
                    "id": "hardware-sourcing",
                    "name": "Questions about cluster hardware sourcing",
                    "seeds": ["Where did Acme source its cluster hardware?"],
                },
            ],
            "claims_to_check": [
                {
                    "id": "claim-half-cost",
                    "text": "Inference runs at half the standard cost",
                },
                {
                    "id": "claim-private-cluster",
                    "text": "Cluster hardware was privately financed",
                },
            ],
            "conflicts_of_interest": [
                {
                    "person": "Alex Investor",
                    "relationship": "Partner at SeedVC, an investor in Acme.",
                }
            ],
            "known_coverage": ["TechDaily"],
            "privacy": {
                "min_followers_to_name": min_followers_to_name,
            },
            "enrichment": {
                "assign_threshold": 0.65,
                "offtopic_floor": 0.35,
            },
        }
    )


class FakeMutationBatch:
    """In-memory mock of Spanner database.batch() context manager."""

    def __init__(self, db: FakeSpannerDatabase) -> None:
        self._db = db
        self.operations: list[tuple[str, str, tuple[str, ...], list[tuple[Any, ...]]]] = []

    def __enter__(self) -> FakeMutationBatch:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self._db.committed_batches.append(self.operations)

    def insert_or_update(
        self,
        table: str,
        columns: tuple[str, ...] | list[str],
        values: list[tuple[Any, ...] | list[Any]],
    ) -> None:
        col_tuple = tuple(columns)
        val_tuples = [tuple(v) for v in values]
        self.operations.append(("insert_or_update", table, col_tuple, val_tuples))
        for row_vals in val_tuples:
            row_dict = dict(zip(col_tuple, row_vals, strict=True))
            if table == "GraphNode":
                key = (row_dict["event_id"], row_dict["node_id"])
                self._db.nodes[key] = row_dict
            elif table == "GraphEdge":
                parent_key = (row_dict["event_id"], row_dict["node_id"])
                if parent_key not in self._db.nodes:
                    raise RuntimeError(
                        f"Interleaved parent GraphNode {parent_key} missing before GraphEdge write"
                    )
                edge_key = (
                    row_dict["event_id"],
                    row_dict["node_id"],
                    row_dict["edge_label"],
                    row_dict["dest_id"],
                )
                self._db.edges[edge_key] = row_dict
            else:
                raise ValueError(f"Unexpected table {table}")

    def delete(self, *args: Any, **kwargs: Any) -> None:
        self._db.delete_calls += 1
        raise AssertionError("delete mutations are forbidden in Spanner Graph upserts")


class FakeSnapshot:
    """In-memory mock of Spanner database.snapshot() executing GQL queries."""

    def __init__(self, db: FakeSpannerDatabase) -> None:
        self._db = db

    def __enter__(self) -> FakeSnapshot:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def execute_sql(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        param_types: dict[str, Any] | None = None,
    ) -> list[tuple[Any, ...]]:
        self._db.executed_queries.append((sql, params or {}, param_types or {}))
        event_id = (params or {}).get("event_id")
        cutoff: datetime | None = (params or {}).get("t")

        normalized_sql = " ".join(sql.split())
        if "MATCH (n)" in normalized_sql:
            out_nodes: list[tuple[Any, ...]] = []
            for row in self._db.nodes.values():
                if row["event_id"] != event_id:
                    continue
                if cutoff is not None and row["valid_from"] > cutoff:
                    continue
                out_nodes.append(
                    (
                        row["node_id"],
                        row["label"],
                        row["valid_from"],
                        row["platform"],
                        row["storyline_id"],
                        row["properties"],
                    )
                )
            return out_nodes

        if "-[e]->" in normalized_sql:
            out_edges: list[tuple[Any, ...]] = []
            for row in self._db.edges.values():
                if row["event_id"] != event_id:
                    continue
                if cutoff is not None and row["valid_from"] > cutoff:
                    continue
                src_node = self._db.nodes.get((row["event_id"], row["node_id"]))
                dst_node = self._db.nodes.get((row["event_id"], row["dest_id"]))
                if src_node is None or dst_node is None:
                    continue
                if cutoff is not None and (
                    src_node["valid_from"] > cutoff or dst_node["valid_from"] > cutoff
                ):
                    continue
                out_edges.append(
                    (
                        row["node_id"],
                        row["edge_label"],
                        row["dest_id"],
                        row["valid_from"],
                        row["properties"],
                    )
                )
            return out_edges

        raise ValueError(f"Unsupported GQL query in FakeSnapshot: {sql}")


class FakeSpannerDatabase:
    """In-memory mock Spanner database tracking mutations and GQL queries."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.edges: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.committed_batches: list[list[tuple[str, str, tuple[str, ...], list[tuple[Any, ...]]]]] = []
        self.executed_queries: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.delete_calls = 0

    def batch(self) -> FakeMutationBatch:
        return FakeMutationBatch(self)

    def snapshot(self, **kwargs: Any) -> FakeSnapshot:
        return FakeSnapshot(self)


# ---------------------------------------------------------------------------
# 1. DDL & setup.sh Tests
# ---------------------------------------------------------------------------


def test_schema_sql_matches_data_model_ddl() -> None:
    assert SCHEMA_SQL_PATH.exists()
    ddl = SCHEMA_SQL_PATH.read_text(encoding="utf-8")

    assert "CREATE TABLE GraphNode" in ddl
    assert "PRIMARY KEY (event_id, node_id);" in ddl
    assert "allow_commit_timestamp = true" in ddl
    assert "CREATE TABLE GraphEdge" in ddl
    assert "PRIMARY KEY (event_id, node_id, edge_label, dest_id)," in ddl
    assert "INTERLEAVE IN PARENT GraphNode ON DELETE CASCADE;" in ddl
    assert "CREATE INDEX GraphNodeByTime ON GraphNode (event_id, valid_from);" in ddl
    assert (
        "CREATE INDEX GraphNodeByStoryline ON GraphNode (event_id, storyline_id, valid_from);"
        in ddl
    )
    assert "CREATE INDEX GraphEdgeByDest ON GraphEdge (event_id, dest_id);" in ddl
    assert "CREATE PROPERTY GRAPH verifact_graph" in ddl
    assert "DYNAMIC LABEL (label)" in ddl
    assert "DYNAMIC LABEL (edge_label)" in ddl
    assert "DYNAMIC PROPERTIES (properties)" in ddl
    assert "SOURCE KEY (event_id, node_id) REFERENCES GraphNode (event_id, node_id)" in ddl
    assert "DESTINATION KEY (event_id, dest_id) REFERENCES GraphNode (event_id, node_id)" in ddl


def test_setup_sh_requires_env_and_never_touches_instance_lifecycle(tmp_path: Path) -> None:
    assert SETUP_SH_PATH.exists()
    script_text = SETUP_SH_PATH.read_text(encoding="utf-8")
    assert "instances create" not in script_text
    assert "instances delete" not in script_text
    assert "instances update" not in script_text

    # Running without GCP_PROJECT or SPANNER_INSTANCE must fail
    env_no_vars = {k: v for k, v in os.environ.items() if k not in {"GCP_PROJECT", "SPANNER_INSTANCE"}}
    res = subprocess.run(
        ["bash", str(SETUP_SH_PATH)],
        env=env_no_vars,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0


def test_setup_sh_creates_db_when_missing_and_is_idempotent_when_present(
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "gcloud_calls.log"
    state_file = tmp_path / "db_exists.flag"
    ddl_state_file = tmp_path / "ddl_state.sql"

    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "{log_file}"
if [[ "$1 $2 $3" == "spanner instances describe" ]]; then
  exit 0
fi
if [[ "$1 $2 $3" == "spanner databases describe" ]]; then
  if [[ -f "{state_file}" ]]; then
    exit 0
  else
    exit 1
  fi
fi
if [[ "$1 $2 $3" == "spanner databases create" ]]; then
  touch "{state_file}"
  cp "{SCHEMA_SQL_PATH}" "{ddl_state_file}"
  exit 0
fi
if [[ "$1 $2 $3 $4" == "spanner databases ddl describe" ]]; then
  if [[ -f "{ddl_state_file}" ]]; then
    cat "{ddl_state_file}"
  fi
  exit 0
fi
if [[ "$1 $2 $3 $4" == "spanner databases ddl update" ]]; then
  cp "{SCHEMA_SQL_PATH}" "{ddl_state_file}"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
        "GCP_PROJECT": "test-proj",
        "SPANNER_INSTANCE": "test-inst",
    }
    env.pop("SPANNER_DATABASE", None)

    # First run: database does not exist -> creates default 'verifact' database with schema.sql
    first = subprocess.run(
        ["bash", str(SETUP_SH_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    calls_after_first = log_file.read_text(encoding="utf-8").splitlines()
    assert any("spanner databases create verifact" in c for c in calls_after_first)

    # Second run: database and all DDL objects exist -> no create, no ddl update
    log_file.write_text("", encoding="utf-8")
    second = subprocess.run(
        ["bash", str(SETUP_SH_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    calls_after_second = log_file.read_text(encoding="utf-8").splitlines()
    assert not any("databases create" in c for c in calls_after_second)
    assert not any("ddl update" in c for c in calls_after_second)

    # Third run: partial DDL (e.g. verifact_graph missing) -> runs ddl update for missing statement
    ddl_state_file.write_text("CREATE TABLE GraphNode (event_id STRING(64));", encoding="utf-8")
    log_file.write_text("", encoding="utf-8")
    third = subprocess.run(
        ["bash", str(SETUP_SH_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert third.returncode == 0, third.stderr
    calls_after_third = log_file.read_text(encoding="utf-8").splitlines()
    assert any("spanner databases ddl update verifact" in c for c in calls_after_third)


# ---------------------------------------------------------------------------
# 2. build_graph_mutations Tests (Nodes, Edges, Privacy, P1.2, P2.1/P2.2, Deferred)
# ---------------------------------------------------------------------------


def test_build_graph_mutations_all_node_and_edge_types() -> None:
    cfg = _make_event_config(min_followers_to_name=10000)

    long_article_body = "Headline about Acme CloudEngine launch.\n\n" + (
        " Jane Doe stated that inference runs at half the standard cost. " * 20
    )
    assert len(long_article_body) > 600

    items = [
        {
            "item_id": "x:1001",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "post",
            "url": "https://x.com/AcmeOfficial/status/1001",
            "author_ref": "AcmeOfficial",
            "author_followers": 50000,
            "created_at": T0_DT,
            "collected_at": T0_DT + timedelta(minutes=2),
            "text": "We launched CloudEngine! Inference runs at half the standard cost.",
            "lang": "en",
            "parent_id": None,
            "quote_of": None,
            "links": ["https://acme.example.com/news/story-1"],
            "engagement": {"likes": 120, "reposts": 40, "replies": 15, "views": 5000, "score": None},
            "time_approximate": False,
        },
        {
            "item_id": "x:1002",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "reply",
            "url": "https://x.com/i/status/1002",
            "author_ref": "private_user_secret_id_42",
            "author_followers": 250,
            "created_at": T0_DT + timedelta(hours=1),
            "collected_at": T0_DT + timedelta(hours=1, minutes=1),
            "text": "Replying: Cluster hardware was privately financed? No, it used public credits.",
            "lang": "en",
            "parent_id": "x:1001",
            "quote_of": None,
            "links": [],
            "engagement": {"likes": 99999, "reposts": 5000, "replies": 300, "views": 900000, "score": None},
            "time_approximate": False,
        },
        {
            "item_id": "x:1003",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "quote",
            "url": "https://x.com/i/status/1003",
            "author_ref": "Alex Investor",
            "author_followers": 800,
            "created_at": T0_DT + timedelta(hours=2),
            "collected_at": T0_DT + timedelta(hours=2, minutes=1),
            "text": "Quoting the launch: Cluster hardware was privately financed according to docs.",
            "lang": "en",
            "parent_id": None,
            "quote_of": "x:1001",
            "links": [],
            "engagement": {"likes": 30, "reposts": 5, "replies": 2, "views": 1200, "score": None},
            "time_approximate": False,
        },
        {
            "item_id": "gdelt:art1",
            "event_id": "acme-launch-event",
            "platform": "news",
            "kind": "article",
            "url": "https://acme.example.com/news/story-1",
            "author_ref": "acme.example.com",
            "author_followers": None,
            "created_at": T0_DT + timedelta(hours=3),
            "collected_at": T0_DT + timedelta(hours=3, minutes=5),
            "text": long_article_body,
            "lang": "en",
            "parent_id": None,
            "quote_of": None,
            "links": [],
            "engagement": None,
            "time_approximate": True,
        },
    ]

    enrichments = [
        {
            "item_id": "x:1001",
            "event_id": "acme-launch-event",
            "lang": "en",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.88,
            "off_topic": False,
            "is_official": True,
            "conflict_person": None,
            "claims": [
                {
                    "claim_id": "claim-half-cost",
                    "stance": "supports",
                    "evidence_span": "Inference runs at half the standard cost",
                    "votes": ["supports", "supports", "supports"],
                    "agreement": 1.0,
                }
            ],
        },
        {
            "item_id": "x:1002",
            "event_id": "acme-launch-event",
            "lang": "en",
            "storyline_id": "hardware-sourcing",
            "storyline_similarity": 0.79,
            "off_topic": False,
            "is_official": False,
            "conflict_person": None,
            "claims": [
                {
                    "claim_id": "claim-private-cluster",
                    "stance": "contradicts",
                    "evidence_span": "No, it used public credits",
                    "votes": ["contradicts", "contradicts", "contradicts"],
                    "agreement": 1.0,
                }
            ],
        },
        {
            "item_id": "x:1003",
            "event_id": "acme-launch-event",
            "lang": "en",
            "storyline_id": "hardware-sourcing",
            "storyline_similarity": 0.81,
            "off_topic": False,
            "is_official": False,
            "conflict_person": "Alex Investor",
            "claims": [
                {
                    "claim_id": "claim-private-cluster",
                    "stance": "supports",
                    "evidence_span": "Cluster hardware was privately financed",
                    "votes": ["supports", "supports", "absent"],
                    "agreement": 0.67,
                }
            ],
        },
        {
            "item_id": "gdelt:art1",
            "event_id": "acme-launch-event",
            "lang": "en",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.84,
            "off_topic": False,
            "is_official": True,
            "conflict_person": None,
            "claims": [
                {
                    "claim_id": "claim-half-cost",
                    "stance": "supports",
                    "evidence_span": "inference runs at half the standard cost",
                    "votes": ["supports", "supports", "supports"],
                    "agreement": 1.0,
                }
            ],
        },
    ]

    account_map = {
        ("x", "AcmeOfficial"): 1,
        ("x", "private_user_secret_id_42"): 17,
        ("x", "Alex Investor"): 23,
    }

    batch = build_graph_mutations(cfg, items, enrichments, account_map)
    assert isinstance(batch, GraphBatch)
    assert len(batch.deferred_edges) == 0

    nodes_by_id = {n.node_id: n for n in batch.nodes}
    labels_present = {n.label for n in batch.nodes}
    assert labels_present == {
        "Event",
        "Post",
        "Account",
        "Article",
        "Outlet",
        "Storyline",
        "Claim",
    }

    # Verify Event node
    ev_node = nodes_by_id["acme-launch-event"]
    assert ev_node.label == "Event"
    assert ev_node.properties["name"] == "Acme Cloud Launch"
    assert ev_node.properties["t0"] == T0_ISO

    # Verify Account privacy rules (P5.2, data-model.md)
    acct_official = nodes_by_id["x:acct:1"]
    assert acct_official.label == "Account"
    assert acct_official.properties["display"] == "AcmeOfficial"
    assert acct_official.properties["is_public_figure"] is True

    # Small private account with huge engagement MUST stay a pseudonym (PRD case #16)
    acct_private = nodes_by_id["x:acct:17"]
    assert acct_private.label == "Account"
    assert acct_private.properties["display"] == "Account A17"
    assert acct_private.properties["is_public_figure"] is False
    assert "private_user_secret_id_42" not in str(acct_private.properties)

    # Configured public figure (conflicts_of_interest) is named even with < 10000 followers
    acct_conflict = nodes_by_id["x:acct:23"]
    assert acct_conflict.label == "Account"
    assert acct_conflict.properties["display"] == "Alex Investor"
    assert acct_conflict.properties["is_public_figure"] is True

    # Verify Article and Outlet nodes & 300-char limit (P5.5)
    art_node = nodes_by_id["gdelt:art1"]
    assert art_node.label == "Article"
    assert art_node.properties["outlet_id"] == "acme.example.com"
    assert art_node.properties["time_approximate"] is True
    assert len(art_node.properties["title"]) <= 300
    for node in batch.nodes:
        assert "author_ref" not in node.properties
        assert "private_user_secret_id_42" not in str(node.properties)
        for val in node.properties.values():
            if isinstance(val, str):
                assert len(val) <= 300

    outlet_node = nodes_by_id["acme.example.com"]
    assert outlet_node.label == "Outlet"
    assert outlet_node.properties["name"] == "acme.example.com"

    # Verify Storyline nodes
    sl_node = nodes_by_id["pricing-claims"]
    assert sl_node.label == "Storyline"
    assert sl_node.properties == {
        "name": "Official pricing and throughput claims",
        "colour_index": 0,
    }

    # Verify Claim nodes & P2.1 / P2.2 self_reported logic:
    # claim-half-cost is supported ONLY by official post x:1001 and article quoting spokesperson Jane Doe -> self_reported = True
    claim_half = nodes_by_id["claim-half-cost"]
    assert claim_half.label == "Claim"
    assert claim_half.properties["source_item_id"] == "x:1001"
    assert claim_half.properties["self_reported"] is True
    assert claim_half.properties["confirmed_by"] == []

    # Verify all 8 edge labels exist
    edge_labels = {e.edge_label for e in batch.edges}
    assert edge_labels == {
        "REPLIED_TO",
        "QUOTED",
        "LINKS_TO",
        "AUTHORED_BY",
        "PUBLISHED_BY",
        "BELONGS_TO",
        "SUPPORTS",
        "CONTRADICTS",
    }

    edges_by_key = {(e.node_id, e.edge_label, e.dest_id): e for e in batch.edges}
    assert ("x:1002", "REPLIED_TO", "x:1001") in edges_by_key
    assert ("x:1003", "QUOTED", "x:1001") in edges_by_key
    assert ("x:1001", "LINKS_TO", "gdelt:art1") in edges_by_key
    assert edges_by_key[("x:1001", "LINKS_TO", "gdelt:art1")].properties == {"explicit": True}
    assert ("x:1002", "AUTHORED_BY", "x:acct:17") in edges_by_key
    assert ("gdelt:art1", "PUBLISHED_BY", "acme.example.com") in edges_by_key
    assert ("x:1001", "BELONGS_TO", "pricing-claims") in edges_by_key
    assert edges_by_key[("x:1001", "BELONGS_TO", "pricing-claims")].properties == {
        "similarity": 0.88
    }
    assert ("x:1001", "SUPPORTS", "claim-half-cost") in edges_by_key
    assert ("x:1002", "CONTRADICTS", "claim-private-cluster") in edges_by_key


def test_p2_self_reported_becomes_false_only_with_independent_confirmation() -> None:
    cfg = _make_event_config()

    items = [
        {
            "item_id": "x:1001",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "post",
            "url": "https://x.com/AcmeOfficial/status/1001",
            "author_ref": "AcmeOfficial",
            "author_followers": 50000,
            "created_at": T0_DT,
            "collected_at": T0_DT,
            "text": "Inference runs at half the standard cost.",
            "lang": "en",
        },
        {
            "item_id": "hn:2001",
            "event_id": "acme-launch-event",
            "platform": "hn",
            "kind": "comment",
            "url": "https://news.ycombinator.com/item?id=2001",
            "author_ref": "indie_benchmarker",
            "author_followers": None,
            "created_at": T0_DT + timedelta(hours=2),
            "collected_at": T0_DT + timedelta(hours=2),
            "text": "Our independent benchmark confirms inference runs at half the standard cost.",
            "lang": "en",
        },
    ]

    enrichments = [
        {
            "item_id": "x:1001",
            "event_id": "acme-launch-event",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.9,
            "is_official": True,
            "claims": [
                {
                    "claim_id": "claim-half-cost",
                    "stance": "supports",
                    "evidence_span": "Inference runs at half the standard cost",
                    "agreement": 1.0,
                }
            ],
        },
        {
            "item_id": "hn:2001",
            "event_id": "acme-launch-event",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.85,
            "is_official": False,
            "claims": [
                {
                    "claim_id": "claim-half-cost",
                    "stance": "supports",
                    "evidence_span": "inference runs at half the standard cost",
                    "agreement": 1.0,
                }
            ],
        },
    ]

    batch = build_graph_mutations(
        cfg,
        items,
        enrichments,
        {("x", "AcmeOfficial"): 1, ("hn", "indie_benchmarker"): 2},
    )
    claim_node = next(n for n in batch.nodes if n.label == "Claim" and n.node_id == "claim-half-cost")
    assert claim_node.properties["source_item_id"] == "x:1001"
    assert claim_node.properties["confirmed_by"] == ["hn:2001"]
    assert claim_node.properties["self_reported"] is False


def test_p1_2_drops_supports_or_contradicts_edge_when_span_not_exact_substring() -> None:
    cfg = _make_event_config()

    items = [
        {
            "item_id": "x:1001",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "post",
            "url": "https://x.com/AcmeOfficial/status/1001",
            "author_ref": "AcmeOfficial",
            "created_at": T0_DT,
            "collected_at": T0_DT,
            "text": "Actual post text without the hallucinated quote.",
            "lang": "en",
        }
    ]

    enrichments = [
        {
            "item_id": "x:1001",
            "event_id": "acme-launch-event",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.9,
            "is_official": True,
            "claims": [
                {
                    "claim_id": "claim-half-cost",
                    "stance": "supports",
                    "evidence_span": "This span does NOT exist in the item text",
                    "agreement": 1.0,
                }
            ],
        }
    ]

    batch = build_graph_mutations(cfg, items, enrichments, {("x", "AcmeOfficial"): 1})
    stance_edges = [e for e in batch.edges if e.edge_label in {"SUPPORTS", "CONTRADICTS"}]
    assert stance_edges == []


def test_missing_endpoint_edges_placed_in_deferred_edges_until_endpoint_arrives() -> None:
    cfg = _make_event_config()

    reply_item = {
        "item_id": "x:2002",
        "event_id": "acme-launch-event",
        "platform": "x",
        "kind": "reply",
        "url": "https://x.com/i/status/2002",
        "author_ref": "user_a",
        "created_at": T0_DT + timedelta(hours=1),
        "collected_at": T0_DT + timedelta(hours=1),
        "text": "Replying to an uncollected parent and quoting an uncollected post.",
        "lang": "en",
        "parent_id": "x:9999",
        "quote_of": "x:8888",
        "links": ["https://x.com/other/status/7777"],
    }
    enrichments = [
        {
            "item_id": "x:2002",
            "event_id": "acme-launch-event",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.75,
            "is_official": False,
            "claims": [],
        }
    ]

    batch1 = build_graph_mutations(cfg, [reply_item], enrichments, {("x", "user_a"): 1})
    active_keys_1 = {(e.node_id, e.edge_label, e.dest_id) for e in batch1.edges}
    deferred_keys_1 = {(e.node_id, e.edge_label, e.dest_id) for e in batch1.deferred_edges}

    assert ("x:2002", "REPLIED_TO", "x:9999") not in active_keys_1
    assert ("x:2002", "QUOTED", "x:8888") not in active_keys_1
    assert ("x:2002", "LINKS_TO", "x:7777") not in active_keys_1

    assert ("x:2002", "REPLIED_TO", "x:9999") in deferred_keys_1
    assert ("x:2002", "QUOTED", "x:8888") in deferred_keys_1
    assert ("x:2002", "LINKS_TO", "x:7777") in deferred_keys_1

    # Now parent x:9999 arrives in the next batch
    parent_item = {
        "item_id": "x:9999",
        "event_id": "acme-launch-event",
        "platform": "x",
        "kind": "post",
        "url": "https://x.com/i/status/9999",
        "author_ref": "user_b",
        "created_at": T0_DT,
        "collected_at": T0_DT,
        "text": "Original parent post.",
        "lang": "en",
    }
    batch2 = build_graph_mutations(
        cfg,
        [parent_item, reply_item],
        enrichments,
        {("x", "user_a"): 1, ("x", "user_b"): 2},
    )
    active_keys_2 = {(e.node_id, e.edge_label, e.dest_id) for e in batch2.edges}
    deferred_keys_2 = {(e.node_id, e.edge_label, e.dest_id) for e in batch2.deferred_edges}

    assert ("x:2002", "REPLIED_TO", "x:9999") in active_keys_2
    assert ("x:2002", "REPLIED_TO", "x:9999") not in deferred_keys_2


def test_min_followers_to_name_makes_high_follower_account_nameable() -> None:
    cfg = _make_event_config(min_followers_to_name=5000)

    items = [
        {
            "item_id": "x:3001",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "post",
            "url": "https://x.com/i/status/3001",
            "author_ref": "tech_commentator_id",
            "author_display": "Tech Commentator",
            "author_followers": 12000,
            "created_at": T0_DT,
            "collected_at": T0_DT,
            "text": "Commentary on launch.",
            "lang": "en",
        },
        {
            "item_id": "x:3002",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "post",
            "url": "https://x.com/i/status/3002",
            "author_ref": "small_acct_id",
            "author_display": "Small Account",
            "author_followers": 4999,
            "created_at": T0_DT,
            "collected_at": T0_DT,
            "text": "Another post.",
            "lang": "en",
        },
    ]

    batch = build_graph_mutations(
        cfg,
        items,
        [],
        {("x", "tech_commentator_id"): 1, ("x", "small_acct_id"): 2},
    )
    nodes_by_id = {n.node_id: n for n in batch.nodes}
    assert nodes_by_id["x:acct:1"].properties["display"] == "Tech Commentator"
    assert nodes_by_id["x:acct:1"].properties["is_public_figure"] is True
    assert nodes_by_id["x:acct:2"].properties["display"] == "Account A2"
    assert nodes_by_id["x:acct:2"].properties["is_public_figure"] is False


# ---------------------------------------------------------------------------
# 3. upsert_graph_batch & query_graph_at_hour Tests
# ---------------------------------------------------------------------------


def test_upsert_graph_batch_is_idempotent_and_never_deletes() -> None:
    cfg = _make_event_config()
    items = [
        {
            "item_id": "x:1001",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "post",
            "url": "https://x.com/AcmeOfficial/status/1001",
            "author_ref": "AcmeOfficial",
            "author_followers": 50000,
            "created_at": T0_DT,
            "collected_at": T0_DT,
            "text": "Inference runs at half the standard cost.",
            "lang": "en",
        },
        {
            "item_id": "x:1002",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "reply",
            "url": "https://x.com/i/status/1002",
            "author_ref": "user_2",
            "author_followers": 10,
            "created_at": T0_DT + timedelta(hours=4),
            "collected_at": T0_DT + timedelta(hours=4),
            "text": "Replying at hour 4.",
            "lang": "en",
            "parent_id": "x:1001",
        },
    ]
    enrichments = [
        {
            "item_id": "x:1001",
            "event_id": "acme-launch-event",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.91,
            "is_official": True,
            "claims": [
                {
                    "claim_id": "claim-half-cost",
                    "stance": "supports",
                    "evidence_span": "Inference runs at half the standard cost",
                    "agreement": 1.0,
                }
            ],
        },
        {
            "item_id": "x:1002",
            "event_id": "acme-launch-event",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.72,
            "is_official": False,
            "claims": [],
        },
    ]
    batch = build_graph_mutations(
        cfg, items, enrichments, {("x", "AcmeOfficial"): 1, ("x", "user_2"): 2}
    )

    fake_db = FakeSpannerDatabase()
    res1 = upsert_graph_batch(batch, database=fake_db)
    assert isinstance(res1, UpsertResult)
    assert res1.nodes_upserted == len(batch.nodes)
    assert res1.edges_upserted == len(batch.edges)
    assert res1.batches_committed >= 1
    assert fake_db.delete_calls == 0

    node_count_after_first = len(fake_db.nodes)
    edge_count_after_first = len(fake_db.edges)

    # Second upsert of identical batch is idempotent (same primary keys overwritten in-place)
    res2 = upsert_graph_batch(batch, database=fake_db)
    assert res2.nodes_upserted == len(batch.nodes)
    assert res2.edges_upserted == len(batch.edges)
    assert len(fake_db.nodes) == node_count_after_first
    assert len(fake_db.edges) == edge_count_after_first
    assert fake_db.delete_calls == 0

    # Empty batch makes zero commits
    empty_res = upsert_graph_batch(
        GraphBatch(nodes=[], edges=[], deferred_edges=[]),
        database=fake_db,
    )
    assert empty_res.nodes_upserted == 0
    assert empty_res.edges_upserted == 0
    assert empty_res.batches_committed == 0


def test_query_graph_at_hour_filters_by_valid_from_and_matches_api_shapes() -> None:
    cfg = _make_event_config()
    items = [
        {
            "item_id": "x:1001",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "post",
            "url": "https://x.com/AcmeOfficial/status/1001",
            "author_ref": "AcmeOfficial",
            "author_followers": 50000,
            "created_at": T0_DT,
            "collected_at": T0_DT,
            "text": "Inference runs at half the standard cost.",
            "lang": "en",
            "engagement": {"likes": 100, "reposts": 20, "replies": 5, "views": 1200, "score": None},
        },
        {
            "item_id": "x:1002",
            "event_id": "acme-launch-event",
            "platform": "x",
            "kind": "reply",
            "url": "https://x.com/i/status/1002",
            "author_ref": "private_author_id",
            "author_followers": 10,
            "created_at": T0_DT + timedelta(hours=5),
            "collected_at": T0_DT + timedelta(hours=5),
            "text": "Replying at hour 5.",
            "lang": "en",
            "parent_id": "x:1001",
            "engagement": {"likes": 3, "reposts": 0, "replies": 0, "views": 80, "score": None},
        },
    ]
    enrichments = [
        {
            "item_id": "x:1001",
            "event_id": "acme-launch-event",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.91,
            "is_official": True,
            "claims": [
                {
                    "claim_id": "claim-half-cost",
                    "stance": "supports",
                    "evidence_span": "Inference runs at half the standard cost",
                    "agreement": 1.0,
                }
            ],
        },
        {
            "item_id": "x:1002",
            "event_id": "acme-launch-event",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.72,
            "is_official": False,
            "claims": [],
        },
    ]
    batch = build_graph_mutations(
        cfg, items, enrichments, {("x", "AcmeOfficial"): 1, ("x", "private_author_id"): 17}
    )
    fake_db = FakeSpannerDatabase()
    upsert_graph_batch(batch, database=fake_db)

    # Query at hour 2 -> x:1001 (hour 0) is present, x:1002 (hour 5) is excluded
    nodes_h2, edges_h2 = query_graph_at_hour(
        "acme-launch-event",
        at_hour=2,
        t0_utc=T0_ISO,
        database=fake_db,
    )
    node_ids_h2 = {n["id"] for n in nodes_h2}
    assert "x:1001" in node_ids_h2
    assert "x:1002" not in node_ids_h2
    assert all(e["source"] != "x:1002" and e["target"] != "x:1002" for e in edges_h2)

    # Verify GQL queries used GRAPH verifact_graph and valid_from <= @t
    assert len(fake_db.executed_queries) >= 2
    for sql, params, _ in fake_db.executed_queries:
        assert "GRAPH verifact_graph" in sql
        assert "valid_from <= @t" in sql
        assert params["event_id"] == "acme-launch-event"

    # Query at hour 5 -> both x:1001 and x:1002 are present
    nodes_h5, edges_h5 = query_graph_at_hour(
        "acme-launch-event",
        at_hour=5,
        t0_utc=T0_ISO,
        database=fake_db,
    )
    nodes_by_id_h5 = {n["id"]: n for n in nodes_h5}
    assert "x:1001" in nodes_by_id_h5
    assert "x:1002" in nodes_by_id_h5

    post_1002 = nodes_by_id_h5["x:1002"]
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
    assert required_node_keys.issubset(post_1002.keys())
    assert post_1002["type"] == "post"
    assert post_1002["platform"] == "x"
    assert post_1002["kind"] == "reply"
    assert post_1002["hour"] == 5
    assert post_1002["label"] == "Account A17"
    assert "author_ref" not in post_1002
    assert "private_author_id" not in str(post_1002)

    reply_edge = next(
        e for e in edges_h5 if e["source"] == "x:1002" and e["target"] == "x:1001"
    )
    required_edge_keys = {"source", "target", "type", "created_at", "hour"}
    assert required_edge_keys.issubset(reply_edge.keys())
    assert reply_edge["type"] == "replied_to"
    assert reply_edge["hour"] == 5


def test_multi_batch_chunking_and_flexible_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dict = _make_event_config().model_dump()
    items = [
        {
            "item_id": "hn:501",
            "event_id": "acme-launch-event",
            "platform": "hn",
            "kind": "story",
            "url": "https://news.ycombinator.com/item?id=501",
            "author_ref": "hn_submitter",
            "created_at": "2026-11-16T12:30:00Z",
            "text": "Story linking to YT and HN.",
            "links": [
                "https://www.youtube.com/watch?v=vid123",
                "https://news.ycombinator.com/item?id=502",
                "yt:vid123",
            ],
            "engagement": {"likes": 0, "reposts": 0, "replies": 4, "views": None, "score": 42},
        },
        {
            "item_id": "yt:vid123",
            "event_id": "acme-launch-event",
            "platform": "youtube",
            "kind": "video",
            "url": "https://www.youtube.com/watch?v=vid123",
            "author_ref": "yt_channel_unmapped",
            "created_at": "2026-11-16T13:00:00Z",
            "text": "Video about Acme launch.",
        },
    ]
    enrichments_map = {
        "hn:501": {
            "item_id": "hn:501",
            "storyline_id": "pricing-claims",
            "storyline_similarity": 0.77,
        },
    }
    account_map_rows = [
        {"platform": "hn", "author_ref": "hn_submitter", "account_n": 5},
    ]

    batch = build_graph_mutations(cfg_dict, items, enrichments_map, account_map_rows)
    node_0 = batch.nodes[0]
    assert node_0["event_id"] == "acme-launch-event"
    assert node_0.get("label") == node_0.label
    assert node_0.to_dict()["node_id"] == node_0.node_id

    edge_0 = batch.edges[0]
    assert edge_0["event_id"] == "acme-launch-event"
    assert edge_0.get("edge_label") == edge_0.edge_label
    assert edge_0.to_dict()["dest_id"] == edge_0.dest_id

    # Auto-assigned account_n=6 for unmapped yt_channel_unmapped
    nodes_by_id = {n.node_id: n for n in batch.nodes}
    assert "youtube:acct:6" in nodes_by_id
    assert nodes_by_id["hn:501"].properties["reach"] == 46

    # hn:501 -> yt:vid123 is active; hn:501 -> hn:502 is deferred
    active_links = [e for e in batch.edges if e.edge_label == "LINKS_TO"]
    deferred_links = [e for e in batch.deferred_edges if e.edge_label == "LINKS_TO"]
    assert len(active_links) == 1
    assert active_links[0].dest_id == "yt:vid123"
    assert len(deferred_links) == 1
    assert deferred_links[0].dest_id == "hn:502"

    # Multi-batch upsert with batch_size=2 writes all GraphNode chunks before GraphEdge chunks
    fake_db = FakeSpannerDatabase()
    res = upsert_graph_batch(batch, database=fake_db, batch_size=2)
    assert res.nodes_upserted == len(batch.nodes)
    assert res.edges_upserted == len(batch.edges)
    assert res.batches_committed > 2

    # Missing SPANNER_INSTANCE raises ValueError when database=None
    monkeypatch.delenv("SPANNER_INSTANCE", raising=False)
    with pytest.raises(ValueError, match="SPANNER_INSTANCE"):
        upsert_graph_batch(batch, database=None)

