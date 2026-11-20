#!/usr/bin/env bash
set -euo pipefail

# Deploy VeriFact Cloud Run serving services (api, web) and frame-builder job.
# Enforces S4.3 (min-instances=0, small max-instances) and S4.4 (single GCP_REGION from env).

: "${GCP_PROJECT:?GCP_PROJECT environment variable is required}"
: "${GCP_REGION:?GCP_REGION environment variable is required}"
: "${EVENT_CONFIG:?EVENT_CONFIG environment variable is required (e.g. events/<event_id>.yaml)}"
: "${BQ_DATASET:=verifact}"
: "${AR_REPO:=verifact}"
: "${GRAPH_BACKEND:=frames}"
: "${SPANNER_INSTANCE:=}"
: "${SPANNER_DATABASE:=verifact}"
: "${SPANNER_LIVE:=false}"
: "${MAX_INSTANCES:=3}"
: "${CORS_ORIGINS:=*}"
: "${VITE_CHECK_BASE_URL:=}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

# 1. Build & deploy frame-builder Cloud Run Job
FB_IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/frame-builder:latest"
gcloud builds submit "${REPO_ROOT}" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --tag="${FB_IMAGE}" \
  --dockerfile="services/frame_builder/Dockerfile"

FB_ACTION="create"
if gcloud run jobs describe "frame-builder" --project="${GCP_PROJECT}" --region="${GCP_REGION}" >/dev/null 2>&1; then
  FB_ACTION="update"
fi

gcloud run jobs "${FB_ACTION}" "frame-builder" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --image="${FB_IMAGE}" \
  --args="--event,${EVENT_CONFIG}" \
  --set-env-vars="GCP_PROJECT=${GCP_PROJECT},GCP_REGION=${GCP_REGION},BQ_DATASET=${BQ_DATASET}" \
  --task-timeout="15m" \
  --max-retries=1

# 2. Build & deploy read-only API Cloud Run Service
API_IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/api:latest"
gcloud builds submit "${REPO_ROOT}" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --tag="${API_IMAGE}" \
  --dockerfile="services/api/Dockerfile"

gcloud run deploy "api" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --image="${API_IMAGE}" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances="${MAX_INSTANCES}" \
  --set-env-vars="GCP_PROJECT=${GCP_PROJECT},GCP_REGION=${GCP_REGION},BQ_DATASET=${BQ_DATASET},GRAPH_BACKEND=${GRAPH_BACKEND},SPANNER_INSTANCE=${SPANNER_INSTANCE},SPANNER_DATABASE=${SPANNER_DATABASE},SPANNER_LIVE=${SPANNER_LIVE},CORS_ORIGINS=${CORS_ORIGINS}"

API_URL="$(gcloud run services describe "api" --project="${GCP_PROJECT}" --region="${GCP_REGION}" --format='value(status.url)')"
echo "Deployed API: ${API_URL}"

# 3. Build & deploy Nebula Web UI Cloud Run Service
WEB_IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/web:latest"
WEB_CLOUDBUILD="${TMP_DIR}/web-cloudbuild.yaml"
cat > "${WEB_CLOUDBUILD}" <<EOF
steps:
  - name: gcr.io/cloud-builders/docker
    args:
      - build
      - --build-arg
      - VITE_API_BASE_URL=${API_URL}
      - --build-arg
      - VITE_CHECK_BASE_URL=${VITE_CHECK_BASE_URL}
      - -t
      - ${WEB_IMAGE}
      - .
images:
  - ${WEB_IMAGE}
EOF

gcloud builds submit "${REPO_ROOT}/web" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --config="${WEB_CLOUDBUILD}"

gcloud run deploy "web" \
  --project="${GCP_PROJECT}" \
  --region="${GCP_REGION}" \
  --image="${WEB_IMAGE}" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances="${MAX_INSTANCES}"

WEB_URL="$(gcloud run services describe "web" --project="${GCP_PROJECT}" --region="${GCP_REGION}" --format='value(status.url)')"
echo "Deployed Web UI: ${WEB_URL}"
