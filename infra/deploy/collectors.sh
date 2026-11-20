#!/usr/bin/env bash
set -euo pipefail

: "${GCP_PROJECT:?GCP_PROJECT environment variable is required}"
: "${GCP_REGION:?GCP_REGION environment variable is required}"
: "${EVENT_CONFIG:?EVENT_CONFIG environment variable is required (e.g. events/<event_id>.yaml)}"
: "${BQ_DATASET:=verifact}"
: "${FIRESTORE_DATABASE:=(default)}"
: "${AR_REPO:=verifact}"
: "${HN_EVERY:=10m}"
: "${YOUTUBE_EVERY:=60m}"
: "${GDELT_EVERY:=15m}"
: "${FETCH_PAGES_EVERY:=15m}"
: "${FETCH_PAGES_OFFICIAL_CRON:=0 0 * * *}"
: "${ENRICH_EVERY:=2m}"
: "${LIVE_INGESTION:=false}"
: "${YOUTUBE_SEARCHES_PER_RUN:=4}"
: "${FIRECRAWL_MAX_PER_RUN:=50}"
: "${FIRECRAWL_MAX_TOTAL:=800}"
: "${ENRICH_MAX_ITEMS_PER_RUN:=500}"
: "${ENRICH_DAILY_CALL_CAP:=2000}"
: "${JOB_TASK_TIMEOUT:=15m}"
: "${SCHEDULER_SA:=}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cadence_to_cron() {
  local cadence="$1"
  if [[ "${cadence}" =~ ^([0-9]+)m$ ]]; then
    local mins="${BASH_REMATCH[1]}"
    if (( mins >= 60 )); then
      local hrs=$(( mins / 60 ))
      if (( hrs <= 1 )); then
        echo "0 * * * *"
      else
        echo "0 */${hrs} * * *"
      fi
    elif (( mins <= 1 )); then
      echo "* * * * *"
    else
      echo "*/${mins} * * * *"
    fi
  elif [[ "${cadence}" =~ ^([0-9]+)h$ ]]; then
    local hrs="${BASH_REMATCH[1]}"
    if (( hrs <= 1 )); then
      echo "0 * * * *"
    else
      echo "0 */${hrs} * * *"
    fi
  else
    # Allow explicit 5-field cron expression
    echo "${cadence}"
  fi
}

deploy_job() {
  local job_name="$1"
  local service_dir="$2"
  local extra_env="$3"
  local secret_flag="$4"

  local image="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/${job_name}:latest"
  local env_vars="GCP_PROJECT=${GCP_PROJECT},GCP_REGION=${GCP_REGION},BQ_DATASET=${BQ_DATASET},FIRESTORE_DATABASE=${FIRESTORE_DATABASE}"
  if [[ -n "${extra_env}" ]]; then
    env_vars="${env_vars},${extra_env}"
  fi

  gcloud builds submit "${REPO_ROOT}" \
    --project="${GCP_PROJECT}" \
    --region="${GCP_REGION}" \
    --tag="${image}" \
    --dockerfile="${service_dir}/Dockerfile"

  local action="create"
  if gcloud run jobs describe "${job_name}" --project="${GCP_PROJECT}" --region="${GCP_REGION}" >/dev/null 2>&1; then
    action="update"
  fi

  local args=(
    run jobs "${action}" "${job_name}"
    --project="${GCP_PROJECT}"
    --region="${GCP_REGION}"
    --image="${image}"
    --args="--event,${EVENT_CONFIG}"
    --set-env-vars="${env_vars}"
    --task-timeout="${JOB_TASK_TIMEOUT}"
    --max-retries=1
  )
  if [[ -n "${secret_flag}" ]]; then
    args+=("--set-secrets=${secret_flag}")
  fi

  gcloud "${args[@]}"
}

deploy_schedule() {
  local job_name="$1"
  local cadence="$2"
  local schedule_name="${job_name}-schedule"
  local cron_expr
  cron_expr="$(cadence_to_cron "${cadence}")"
  local uri="https://${GCP_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${GCP_PROJECT}/jobs/${job_name}:run"

  local action="create"
  if gcloud scheduler jobs describe "${schedule_name}" --project="${GCP_PROJECT}" --location="${GCP_REGION}" >/dev/null 2>&1; then
    action="update"
  fi

  local args=(
    scheduler jobs "${action}" http "${schedule_name}"
    --project="${GCP_PROJECT}"
    --location="${GCP_REGION}"
    --schedule="${cron_expr}"
    --time-zone="UTC"
    --uri="${uri}"
    --http-method="POST"
  )
  if [[ -n "${SCHEDULER_SA}" ]]; then
    args+=("--oauth-service-account-email=${SCHEDULER_SA}")
  fi

  gcloud "${args[@]}"

  if [[ "${LIVE_INGESTION}" != "true" ]]; then
    gcloud scheduler jobs pause "${schedule_name}" \
      --project="${GCP_PROJECT}" \
      --location="${GCP_REGION}"
  else
    gcloud scheduler jobs resume "${schedule_name}" \
      --project="${GCP_PROJECT}" \
      --location="${GCP_REGION}" || true
  fi
}

deploy_job "collect-hn" "services/collect_hn" "" ""
deploy_schedule "collect-hn" "${HN_EVERY}"

deploy_job "collect-youtube" "services/collect_youtube" "YOUTUBE_SEARCHES_PER_RUN=${YOUTUBE_SEARCHES_PER_RUN}" "YOUTUBE_API_KEY=YOUTUBE_API_KEY:latest"
deploy_schedule "collect-youtube" "${YOUTUBE_EVERY}"

deploy_job "collect-gdelt" "services/collect_gdelt" "" ""
deploy_schedule "collect-gdelt" "${GDELT_EVERY}"

deploy_job "fetch-pages" "services/fetch_pages" "FIRECRAWL_MAX_PER_RUN=${FIRECRAWL_MAX_PER_RUN},FIRECRAWL_MAX_TOTAL=${FIRECRAWL_MAX_TOTAL}" "FIRECRAWL_API_KEY=FIRECRAWL_API_KEY:latest"
deploy_schedule "fetch-pages" "${FETCH_PAGES_EVERY}"
deploy_job "fetch-pages-official" "services/fetch_pages" "FIRECRAWL_MAX_PER_RUN=${FIRECRAWL_MAX_PER_RUN},FIRECRAWL_MAX_TOTAL=${FIRECRAWL_MAX_TOTAL}" "FIRECRAWL_API_KEY=FIRECRAWL_API_KEY:latest"
deploy_schedule "fetch-pages-official" "${FETCH_PAGES_OFFICIAL_CRON}"

: "${GEMINI_MODEL:=}"
: "${EMBEDDING_MODEL:=gemini-embedding-001}"
: "${EMBEDDING_DIM:=768}"
: "${PROMPT_VERSION:=v1}"
deploy_job "enrich" "services/enrich" "GEMINI_MODEL=${GEMINI_MODEL},EMBEDDING_MODEL=${EMBEDDING_MODEL},EMBEDDING_DIM=${EMBEDDING_DIM},PROMPT_VERSION=${PROMPT_VERSION},ENRICH_MAX_ITEMS_PER_RUN=${ENRICH_MAX_ITEMS_PER_RUN},ENRICH_DAILY_CALL_CAP=${ENRICH_DAILY_CALL_CAP}" ""
deploy_schedule "enrich" "${ENRICH_EVERY}"
