#!/usr/bin/env bash
# Build and deploy the Check service to Cloud Run.
#
#   services/check/deploy.sh            print every command and change nothing (the default)
#   services/check/deploy.sh --apply    run them
#
# Run from the repo root with the settings in the environment (see services/check/.env.example):
#   GCP_PROJECT GCP_REGION GEMINI_MODEL EVENT_ID
#   CHECK_RATE_LIMIT_PER_MIN CHECK_DAILY_CALL_CAP CHECK_MAX_INPUT_CHARS CHECK_MAX_IMAGE_BYTES
# Optional: CORS_ORIGINS (comma-separated), MAX_INSTANCES (default 2), SERVICE (default check).
#
# The fact-check API key is never passed here. Store it once in Secret Manager as
# "fact-check-api-key" and the deploy mounts it as FACT_CHECK_API_KEY. Without it, fact-check lookups
# are skipped and the rest of the check works.
set -euo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

: "${GCP_PROJECT:?set GCP_PROJECT}" "${GCP_REGION:?set GCP_REGION}"
: "${GEMINI_MODEL:?set GEMINI_MODEL}" "${EVENT_ID:?set EVENT_ID}"
: "${CHECK_RATE_LIMIT_PER_MIN:?set CHECK_RATE_LIMIT_PER_MIN}" "${CHECK_DAILY_CALL_CAP:?set CHECK_DAILY_CALL_CAP}"
: "${CHECK_MAX_INPUT_CHARS:?set CHECK_MAX_INPUT_CHARS}" "${CHECK_MAX_IMAGE_BYTES:?set CHECK_MAX_IMAGE_BYTES}"

SERVICE="${SERVICE:-check}"
REPO="${REPO:-verifact}"
MAX_INSTANCES="${MAX_INSTANCES:-2}"       # the rate limit and daily cap are per instance, so keep this small
CORS_ORIGINS="${CORS_ORIGINS:-http://localhost:5173}"
SA_NAME="verifact-check"
SA="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPO}/${SERVICE}:$(git rev-parse --short HEAD)"

run() { echo "+ $*"; if [ "$APPLY" = 1 ]; then "$@"; fi; }
run_if_missing() { echo "+ $* (ignored if it already exists)"; if [ "$APPLY" = 1 ]; then "$@" || true; fi; }

[ "$APPLY" = 1 ] || echo "# Dry run: nothing is changed. Add --apply to run these."

run gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
  factchecktools.googleapis.com --project "$GCP_PROJECT"

run_if_missing gcloud artifacts repositories create "$REPO" --repository-format=docker \
  --location "$GCP_REGION" --project "$GCP_PROJECT"

run_if_missing gcloud iam service-accounts create "$SA_NAME" --project "$GCP_PROJECT" \
  --display-name "VeriFact Check service"
for role in roles/aiplatform.user roles/datastore.user roles/secretmanager.secretAccessor; do
  run gcloud projects add-iam-policy-binding "$GCP_PROJECT" --member "serviceAccount:$SA" \
    --role "$role" --condition=None --quiet
done

run gcloud builds submit --config services/check/cloudbuild.yaml \
  --substitutions "_IMAGE=$IMAGE" --project "$GCP_PROJECT" .

# "^@^" makes @ the separator, because CORS_ORIGINS contains commas.
ENV_VARS="^@^GCP_PROJECT=$GCP_PROJECT@GCP_REGION=$GCP_REGION@GEMINI_MODEL=$GEMINI_MODEL@EVENT_ID=$EVENT_ID"
ENV_VARS="$ENV_VARS@CORS_ORIGINS=$CORS_ORIGINS@CHECK_RATE_LIMIT_PER_MIN=$CHECK_RATE_LIMIT_PER_MIN"
ENV_VARS="$ENV_VARS@CHECK_DAILY_CALL_CAP=$CHECK_DAILY_CALL_CAP@CHECK_MAX_INPUT_CHARS=$CHECK_MAX_INPUT_CHARS"
ENV_VARS="$ENV_VARS@CHECK_MAX_IMAGE_BYTES=$CHECK_MAX_IMAGE_BYTES"

SECRET_ARGS=()
if gcloud secrets describe fact-check-api-key --project "$GCP_PROJECT" >/dev/null 2>&1; then
  SECRET_ARGS=(--set-secrets "FACT_CHECK_API_KEY=fact-check-api-key:latest")
else
  echo "# No Secret Manager secret named fact-check-api-key: fact-check lookups will be skipped."
fi

# Public on purpose: the browser extension and the web app call it. The per-client rate limit and the
# daily call cap are what bound the cost. min-instances 0 so it costs nothing when idle.
run gcloud run deploy "$SERVICE" --image "$IMAGE" --region "$GCP_REGION" --project "$GCP_PROJECT" \
  --service-account "$SA" --allow-unauthenticated \
  --min-instances 0 --max-instances "$MAX_INSTANCES" --memory 1Gi --cpu 1 --concurrency 20 --timeout 120 \
  --set-env-vars "$ENV_VARS" ${SECRET_ARGS[@]+"${SECRET_ARGS[@]}"}  # empty-array safe on macOS bash 3.2
