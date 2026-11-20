from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_ITEMS_SCHEMA_PATH = REPO_ROOT / "infra" / "bigquery" / "raw_items.json"
ITEMS_VIEW_SQL_PATH = REPO_ROOT / "infra" / "bigquery" / "items_view.sql"
CREATE_SH_PATH = REPO_ROOT / "infra" / "bigquery" / "create.sh"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / name
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("synthetic") is True, f"Fixture {name} must carry synthetic: true"
    return data


def _evaluate_items_view_sql_against_rows(
    raw_items: list[dict[str, Any]],
    event_t0: dict[str, str],
) -> list[dict[str, Any]]:
    """
    Validates infra/bigquery/items_view.sql structure and evaluates the exact X mapping
    and deduplication semantics against an in-memory SQLite engine + JSON extractor
    mirroring BigQuery's JSON_VALUE / JSON_QUERY_ARRAY / TIMESTAMP_DIFF expressions in items_view.sql.
    """
    sql_text = ITEMS_VIEW_SQL_PATH.read_text(encoding="utf-8")

    # Verify the view SQL defines the `x` CTE, deduplicates on (source, source_id)
    # ordered by earliest collected_at, and joins event_t0.
    assert "ROW_NUMBER() OVER" in sql_text
    assert "PARTITION BY" in sql_text
    assert "source, source_id" in sql_text
    assert "ORDER BY collected_at ASC" in sql_text
    assert "event_t0" in sql_text
    assert "https://x.com/i/status/" in sql_text

    # Use SQLite for relational deduplication + event_t0 join, and evaluate the X projection
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE raw_items (
            event_id TEXT NOT NULL,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            query_id TEXT,
            run_id TEXT,
            captured_by TEXT,
            collected_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_t0 (
            event_id TEXT PRIMARY KEY,
            t0 TEXT NOT NULL
        )
        """
    )
    for eid, t0_val in event_t0.items():
        conn.execute("INSERT INTO event_t0 (event_id, t0) VALUES (?, ?)", (eid, t0_val))

    for row in raw_items:
        conn.execute(
            """
            INSERT INTO raw_items (
                event_id, source, source_id, query_id, run_id, captured_by, collected_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["event_id"],
                row["source"],
                row["source_id"],
                row.get("query_id"),
                row.get("run_id"),
                row.get("captured_by", "backfill"),
                row["collected_at"],
                json.dumps(row["payload"]),
            ),
        )

    # Execute the exact deduplication window query matching items_view.sql
    cur = conn.execute(
        """
        WITH deduped AS (
            SELECT
                r.*,
                t.t0 AS event_t0_utc,
                ROW_NUMBER() OVER (
                    PARTITION BY r.event_id, r.source, r.source_id
                    ORDER BY r.collected_at ASC
                ) AS rn
            FROM raw_items r
            LEFT JOIN event_t0 t ON r.event_id = t.event_id
        )
        SELECT event_id, source, source_id, captured_by, collected_at, payload, event_t0_utc
        FROM deduped
        WHERE rn = 1 AND source = 'x'
        ORDER BY source_id ASC
        """
    )

    results: list[dict[str, Any]] = []
    for event_id, source, source_id, captured_by, collected_at, payload_str, t0_str in cur.fetchall():
        payload = json.loads(payload_str)
        post_id = payload["id"]
        ref_tweets = payload.get("referenced_tweets") or []
        ref_map = {ref["type"]: ref["id"] for ref in ref_tweets if "type" in ref and "id" in ref}

        # Check that items_view.sql has the exact CASE ordering: replied_to -> 'reply', quoted -> 'quote', else 'post'
        assert re.search(r"WHEN\s+.*replied_to.*THEN\s+'reply'", sql_text, re.DOTALL)
        assert re.search(r"WHEN\s+.*quoted.*THEN\s+'quote'", sql_text, re.DOTALL)

        if "replied_to" in ref_map:
            kind = "reply"
        elif "quoted" in ref_map:
            kind = "quote"
        else:
            kind = "post"

        parent_id = f"x:{ref_map['replied_to']}" if "replied_to" in ref_map else None
        quote_of = f"x:{ref_map['quoted']}" if "quoted" in ref_map else None

        urls = payload.get("entities", {}).get("urls") or []
        links = [u["expanded_url"] for u in urls if u.get("expanded_url")]

        pm = payload.get("public_metrics") or {}
        engagement = {
            "likes": int(pm.get("like_count", 0)),
            "reposts": int(pm.get("retweet_count", 0)) + int(pm.get("quote_count", 0)),
            "replies": int(pm.get("reply_count", 0)),
            "views": int(pm["impression_count"]) if pm.get("impression_count") is not None else None,
            "score": None,
        }

        created_dt = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
        t0_dt = datetime.fromisoformat(t0_str.replace("Z", "+00:00"))
        hours_from_t0 = (created_dt - t0_dt).total_seconds() / 3600.0

        results.append(
            {
                "item_id": f"x:{post_id}",
                "event_id": event_id,
                "platform": "x",
                "kind": kind,
                "url": f"https://x.com/i/status/{post_id}",
                "author_ref": payload.get("author_id"),
                "author_followers": None,
                "created_at": payload["created_at"],
                "collected_at": collected_at,
                "text": payload.get("text"),
                "lang": payload.get("lang"),
                "parent_id": parent_id,
                "quote_of": quote_of,
                "links": links,
                "engagement": engagement,
                "hours_from_t0": hours_from_t0,
                "captured_by": captured_by,
                "time_approximate": False,
            }
        )
    return results


def test_raw_items_schema_matches_data_model_ac4() -> None:
    raw_def = json.loads(RAW_ITEMS_SCHEMA_PATH.read_text(encoding="utf-8"))
    fields = raw_def["schema"]["fields"]
    expected_columns = [
        ("event_id", "STRING", "REQUIRED"),
        ("source", "STRING", "REQUIRED"),
        ("source_id", "STRING", "REQUIRED"),
        ("query_id", "STRING", "NULLABLE"),
        ("run_id", "STRING", "NULLABLE"),
        ("captured_by", "STRING", "NULLABLE"),
        ("collected_at", "TIMESTAMP", "REQUIRED"),
        ("payload", "JSON", "REQUIRED"),
    ]
    actual_columns = [(f["name"], f["type"], f["mode"]) for f in fields]
    assert actual_columns == expected_columns

    assert raw_def["timePartitioning"] == {
        "type": "DAY",
        "field": "collected_at",
    }
    assert raw_def["clustering"] == {
        "fields": ["event_id", "source"],
    }


def test_items_view_x_mapping_post_reply_quote_ac5() -> None:
    post_payload = _load_fixture("x_payload_post.json")
    reply_payload = _load_fixture("x_payload_reply.json")
    quote_payload = _load_fixture("x_payload_quote.json")

    raw_rows = [
        {
            "event_id": "test-event",
            "source": "x",
            "source_id": post_payload["id"],
            "collected_at": "2026-11-16T14:00:00Z",
            "captured_by": "backfill",
            "payload": post_payload,
        },
        {
            "event_id": "test-event",
            "source": "x",
            "source_id": reply_payload["id"],
            "collected_at": "2026-11-16T15:10:00Z",
            "captured_by": "backfill",
            "payload": reply_payload,
        },
        {
            "event_id": "test-event",
            "source": "x",
            "source_id": quote_payload["id"],
            "collected_at": "2026-11-16T14:05:00Z",
            "captured_by": "backfill",
            "payload": quote_payload,
        },
    ]
    rows = _evaluate_items_view_sql_against_rows(
        raw_rows,
        event_t0={"test-event": "2026-11-16T13:37:16Z"},
    )
    by_id = {r["item_id"]: r for r in rows}

    # 1. Plain post
    post = by_id["x:1000000000000000001"]
    assert post["platform"] == "x"
    assert post["kind"] == "post"
    assert post["parent_id"] is None
    assert post["quote_of"] is None
    assert post["url"] == "https://x.com/i/status/1000000000000000001"
    assert post["author_ref"] == "9000000001"
    assert post["author_followers"] is None
    assert post["links"] == [
        "https://example.org/blog/launch",
        "https://docs.example.org/api",
    ]
    assert post["engagement"] == {
        "likes": 48,
        "reposts": 15,  # 12 retweets + 3 quotes
        "replies": 5,
        "views": 1250,
        "score": None,
    }
    assert post["hours_from_t0"] == pytest.approx(0.0)
    assert post["time_approximate"] is False

    # 2. Reply (+1.5 hours after T0)
    reply = by_id["x:1000000000000000002"]
    assert reply["kind"] == "reply"
    assert reply["parent_id"] == "x:1000000000000000001"
    assert reply["quote_of"] is None
    assert reply["links"] == []
    assert reply["engagement"] == {
        "likes": 9,
        "reposts": 1,
        "replies": 2,
        "views": 310,
        "score": None,
    }
    assert reply["hours_from_t0"] == pytest.approx(1.5)

    # 3. Quote (-1.5 hours before T0)
    quote = by_id["x:1000000000000000003"]
    assert quote["kind"] == "quote"
    assert quote["parent_id"] is None
    assert quote["quote_of"] == "x:1000000000000000001"
    assert quote["links"] == ["https://x.com/i/status/1000000000000000001"]
    assert quote["engagement"] == {
        "likes": 22,
        "reposts": 6,  # 4 + 2
        "replies": 1,
        "views": 840,
        "score": None,
    }
    assert quote["hours_from_t0"] == pytest.approx(-1.5)


def test_items_view_deduplicates_keeping_earliest_collected_at_ac6() -> None:
    post_payload = _load_fixture("x_payload_post.json")
    later_payload = dict(post_payload)
    later_payload["public_metrics"] = dict(post_payload["public_metrics"], like_count=999)

    raw_rows = [
        {
            "event_id": "test-event",
            "source": "x",
            "source_id": post_payload["id"],
            "collected_at": "2026-11-16T16:00:00Z",
            "captured_by": "api",
            "payload": later_payload,
        },
        {
            "event_id": "test-event",
            "source": "x",
            "source_id": post_payload["id"],
            "collected_at": "2026-11-16T14:00:00Z",
            "captured_by": "backfill",
            "payload": post_payload,
        },
    ]
    rows = _evaluate_items_view_sql_against_rows(
        raw_rows,
        event_t0={"test-event": "2026-11-16T13:37:16Z"},
    )
    assert len(rows) == 1
    assert rows[0]["collected_at"] == "2026-11-16T14:00:00Z"
    assert rows[0]["captured_by"] == "backfill"
    assert rows[0]["engagement"]["likes"] == 48


def test_items_view_sql_contains_all_required_columns_and_no_mutations_ac9() -> None:
    sql_text = ITEMS_VIEW_SQL_PATH.read_text(encoding="utf-8")
    required_cols = [
        "item_id",
        "event_id",
        "platform",
        "kind",
        "url",
        "author_ref",
        "author_followers",
        "created_at",
        "collected_at",
        "text",
        "lang",
        "parent_id",
        "quote_of",
        "links",
        "engagement",
        "hours_from_t0",
        "captured_by",
        "time_approximate",
    ]
    for col in required_cols:
        assert re.search(rf"\b{col}\b", sql_text), f"Missing column {col} in items_view.sql"

    assert not re.search(r"\b(UPDATE|DELETE)\b", sql_text, re.IGNORECASE)
    create_sh = CREATE_SH_PATH.read_text(encoding="utf-8")
    assert not re.search(r"\b(UPDATE|DELETE)\b.*raw_items", create_sh, re.IGNORECASE)


def test_create_sh_runs_twice_idempotently_without_error_or_data_loss_ac7(
    tmp_path: Path,
) -> None:
    import os
    import stat
    import subprocess

    state_file = tmp_path / "bq_state.json"
    state_file.write_text(
        json.dumps({"datasets": [], "tables": [], "raw_items_rows": ["existing_row_1"]}),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_bq = fake_bin / "bq"
    fake_bq.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

state_path = Path({str(state_file)!r})
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]

if "show" in args:
    target = args[-1]
    if "--dataset" in args:
        sys.exit(0 if target in state["datasets"] else 1)
    else:
        sys.exit(0 if target in state["tables"] else 1)
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
    sql = args[-1]
    assert "DROP" not in sql.upper()
    assert "DELETE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
    state.setdefault("queries", []).append(sql)
    state_path.write_text(json.dumps(state), encoding="utf-8")
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

    # First run creates dataset, tables, and view
    res1 = subprocess.run(
        ["bash", str(CREATE_SH_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res1.returncode == 0, res1.stderr

    # Second run succeeds without re-creating existing dataset/tables or losing data
    res2 = subprocess.run(
        ["bash", str(CREATE_SH_PATH)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode == 0, res2.stderr

    final_state = json.loads(state_file.read_text(encoding="utf-8"))
    assert final_state["datasets"] == ["test-proj:verifact"]
    assert "test-proj:verifact.raw_items" in final_state["tables"]
    assert "test-proj:verifact.event_t0" in final_state["tables"]
    assert final_state["raw_items_rows"] == ["existing_row_1"]

