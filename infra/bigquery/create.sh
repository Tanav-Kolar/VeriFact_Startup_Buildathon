#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT:?GCP_PROJECT environment variable is required}"
: "${GCP_REGION:?GCP_REGION environment variable is required}"
: "${BQ_DATASET:=verifact}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
EVENTS_DIR="${EVENTS_DIR:-${REPO_ROOT}/events}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

create_table_from_schema() {
  local table_name="$1"
  local schema_json_path="$2"
  shift 2
  local extra_flags=("$@")

  if ! bq --project_id="${GCP_PROJECT}" show "${GCP_PROJECT}:${BQ_DATASET}.${table_name}" >/dev/null 2>&1; then
    local tmp_schema="${TMP_DIR}/${table_name}.schema.json"
    python3 -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1]))["schema"]["fields"]))' "${schema_json_path}" > "${tmp_schema}"
    bq --project_id="${GCP_PROJECT}" --location="${GCP_REGION}" mk \
      --table \
      "${extra_flags[@]}" \
      "${GCP_PROJECT}:${BQ_DATASET}.${table_name}" \
      "${tmp_schema}"
  fi
}

# 1. Create dataset if missing (idempotent)
if ! bq --project_id="${GCP_PROJECT}" show --dataset "${GCP_PROJECT}:${BQ_DATASET}" >/dev/null 2>&1; then
  bq --project_id="${GCP_PROJECT}" --location="${GCP_REGION}" mk --dataset "${GCP_PROJECT}:${BQ_DATASET}"
fi

# 2. Create raw_items table if missing (idempotent, never drops or modifies raw_items)
create_table_from_schema \
  "raw_items" \
  "${SCRIPT_DIR}/raw_items.json" \
  --time_partitioning_field=collected_at \
  --time_partitioning_type=DAY \
  --clustering_fields=event_id,source

# 3. Create enrichment tables if missing (idempotent)
create_table_from_schema \
  "enrichments" \
  "${SCRIPT_DIR}/enrichments.json" \
  --time_partitioning_field=enriched_at \
  --time_partitioning_type=DAY \
  --clustering_fields=event_id,storyline_id

create_table_from_schema \
  "storyline_centres" \
  "${SCRIPT_DIR}/storyline_centres.json" \
  --clustering_fields=event_id

create_table_from_schema \
  "account_map" \
  "${SCRIPT_DIR}/account_map.json" \
  --clustering_fields=event_id,platform

create_table_from_schema \
  "frames" \
  "${SCRIPT_DIR}/frames.json" \
  --clustering_fields=event_id,hour

# 4. Create event_t0 table if missing and upsert configured events' T0 timestamps
if ! bq --project_id="${GCP_PROJECT}" show "${GCP_PROJECT}:${BQ_DATASET}.event_t0" >/dev/null 2>&1; then
  bq --project_id="${GCP_PROJECT}" --location="${GCP_REGION}" mk \
    --table \
    "${GCP_PROJECT}:${BQ_DATASET}.event_t0" \
    "event_id:STRING,t0:TIMESTAMP"
fi

MERGE_SQL="$(PYTHONPATH="${REPO_ROOT}" python3 - "${EVENTS_DIR}" "${GCP_PROJECT}" "${BQ_DATASET}" <<'PY'
from pathlib import Path
import sys
from verifact_core.config import load_event

events_dir = Path(sys.argv[1])
project = sys.argv[2]
dataset = sys.argv[3]

rows = []
for yaml_path in sorted(events_dir.glob("*.yaml")):
    cfg = load_event(yaml_path)
    t0_iso = cfg.t0.utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows.append(f"SELECT '{cfg.event_id}' AS event_id, TIMESTAMP('{t0_iso}') AS t0")

if rows:
    union_sql = " UNION ALL ".join(rows)
    print(
        f"MERGE `{project}.{dataset}.event_t0` T "
        f"USING ({union_sql}) S ON T.event_id = S.event_id "
        f"WHEN MATCHED THEN UPDATE SET t0 = S.t0 "
        f"WHEN NOT MATCHED THEN INSERT (event_id, t0) VALUES (S.event_id, S.t0);"
    )
PY
)"

if [[ -n "${MERGE_SQL}" ]]; then
  bq --project_id="${GCP_PROJECT}" --location="${GCP_REGION}" query --use_legacy_sql=false "${MERGE_SQL}"
fi

# 5. Create or replace the items view
RENDERED_VIEW_SQL="$(
  GCP_PROJECT="${GCP_PROJECT}" BQ_DATASET="${BQ_DATASET}" python3 - "${SCRIPT_DIR}/items_view.sql" <<'PY'
import os
from pathlib import Path
import sys

sql = Path(sys.argv[1]).read_text(encoding="utf-8")
sql = sql.replace("${GCP_PROJECT}", os.environ["GCP_PROJECT"])
sql = sql.replace("${BQ_DATASET}", os.environ["BQ_DATASET"])
print(sql)
PY
)"

VIEW_DDL="CREATE OR REPLACE VIEW \`${GCP_PROJECT}.${BQ_DATASET}.items\` AS ${RENDERED_VIEW_SQL}"
bq --project_id="${GCP_PROJECT}" --location="${GCP_REGION}" query --use_legacy_sql=false "${VIEW_DDL}"
