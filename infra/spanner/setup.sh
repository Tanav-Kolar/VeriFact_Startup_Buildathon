#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT:?GCP_PROJECT environment variable is required}"
: "${SPANNER_INSTANCE:?SPANNER_INSTANCE environment variable is required}"
: "${SPANNER_DATABASE:=verifact}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA_FILE="${SCRIPT_DIR}/schema.sql"

if [[ ! -f "${SCHEMA_FILE}" ]]; then
  echo "Schema file not found: ${SCHEMA_FILE}" >&2
  exit 1
fi

# S4.2: Verify the existing Spanner instance is present.
gcloud spanner instances describe "${SPANNER_INSTANCE}" \
  --project="${GCP_PROJECT}" >/dev/null

# 1. If the database does not exist yet, create it and apply schema.sql atomically.
if ! gcloud spanner databases describe "${SPANNER_DATABASE}" \
  --instance="${SPANNER_INSTANCE}" \
  --project="${GCP_PROJECT}" >/dev/null 2>&1; then
  gcloud spanner databases create "${SPANNER_DATABASE}" \
    --instance="${SPANNER_INSTANCE}" \
    --project="${GCP_PROJECT}" \
    --ddl-file="${SCHEMA_FILE}"
  exit 0
fi

# 2. Database already exists: inspect current DDL and apply any missing statements idempotently.
EXISTING_DDL="$(
  gcloud spanner databases ddl describe "${SPANNER_DATABASE}" \
    --instance="${SPANNER_INSTANCE}" \
    --project="${GCP_PROJECT}"
)"

TMP_DDL="$(mktemp)"
trap 'rm -f "${TMP_DDL}"' EXIT

python3 - "${SCHEMA_FILE}" "${TMP_DDL}" "${EXISTING_DDL}" <<'PY'
from pathlib import Path
import re
import sys

schema_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
existing_ddl = sys.argv[3]

raw_sql = schema_path.read_text(encoding="utf-8")
lines = [
    re.sub(r"--.*$", "", line)
    for line in raw_sql.splitlines()
]
cleaned_sql = "\n".join(lines)
statements = [stmt.strip() for stmt in cleaned_sql.split(";") if stmt.strip()]

object_re = re.compile(
    r"^CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|INDEX|PROPERTY\s+GRAPH)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_]+)",
    re.IGNORECASE,
)

missing: list[str] = []
for stmt in statements:
    match = object_re.match(stmt)
    if match:
        obj_name = match.group(1)
        pattern = re.compile(rf"\b{re.escape(obj_name)}\b", re.IGNORECASE)
        if pattern.search(existing_ddl):
            continue
    missing.append(stmt + ";")

if missing:
    out_path.write_text("\n\n".join(missing) + "\n", encoding="utf-8")
PY

if [[ -s "${TMP_DDL}" ]]; then
  gcloud spanner databases ddl update "${SPANNER_DATABASE}" \
    --instance="${SPANNER_INSTANCE}" \
    --project="${GCP_PROJECT}" \
    --ddl-file="${TMP_DDL}"
fi
