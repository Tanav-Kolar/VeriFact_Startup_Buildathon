---
trigger: always_on
description: Secrets handling, the X read cap, Gemini rate limits and caching, and cloud budget rules for VeriFact.
---

# Secrets and cost

The repo will be public, and judges get the link. GCP costs after the credits run out, and every X read, are paid from a personal card. Treat every rule here as a hard stop.

## S1 Secrets
- S1.1 No keys, tokens, passwords or service-account JSON in the repo, in any branch or commit.
- S1.2 `.gitignore` covers at least: `.env*` (keep `.env.example`), `.venv/`, `__pycache__/`, `node_modules/`, `*.pem`, `*-key.json`, `.DS_Store`.
- S1.3 Each service has a `.env.example` with variable names and empty values only.
- S1.4 On Cloud Run, secrets come from Secret Manager mounted as env vars. Code reads them from env only.
- S1.5 No secret ever reaches the browser. The web app and extension call our `api` / `check` services, never Gemini, X or another paid API directly.
- S1.6 Treat the old ScraperAPI key as compromised. Do not port it or any key from `_legacy/`.
- S1.7 Before each commit, run `git diff --cached | grep -nE "AIza|sk-[A-Za-z0-9]|Bearer [A-Za-z0-9]|BEGIN (RSA|PRIVATE)|api_key\s*=\s*['\"][^'\"]+"`. It must return nothing.

## S2 X API (paid, capped)
- S2.1 Only `x-backfill` and `collect-x` call the X API. No other code, test or script does.
- S2.2 The cap is the config's `sources.x.max_posts`. The job refuses to start if it is missing.
- S2.3 The job counts reads and stores the running total for each event (in the cursor store), so re-runs and restarts never reset it. It stops cleanly at the cap and logs the total.
- S2.4 It supports `--limit N` and `--dry-run`. The first live run is `--limit 10` (the billing test).
- S2.5 Follow the fetch order in the spec, so stopping at any point leaves a coherent set. Request only the fields the spec lists. No author profile expansions on posts.
- S2.6 Never re-fetch a post already in `raw_items` unless the spec says to.
- S2.7 Tests use recorded fixtures. CI and local tests never hit the live X API.

## S3 Gemini and other runtime AI
- S3.1 Runtime uses only Gemini Flash-Lite and Gemini embeddings on Vertex AI. Model IDs come from env.
- S3.2 Enrich only items that have no `enrichments` row for the current `prompt_version`. Never re-enrich the whole corpus by default.
- S3.3 Cache model calls by a hash of (model_id, prompt_version, input text). A cache hit makes no API call.
- S3.4 Batch requests where the API allows it. Retry with exponential backoff and a max retry count; never retry forever.
- S3.5 Public endpoints that call Gemini (`check`, brief generation) are rate-limited per client and cached by input. They also have a global daily call cap. Values come from env vars. If the spec gives none, stop and ask.
- S3.6 When a limit or cap is hit, return a clear "limit reached" response. Never fall back to a more expensive model.

## S4 Cloud resources and budget
- S4.1 Create only the resources the spec names. No Pub/Sub, Memorystore, Dataflow, GPUs or extra databases.
- S4.2 One Spanner instance (Enterprise, 100 PU) already exists or is created once by a script in `infra/`. Code never creates, resizes or deletes Spanner instances.
- S4.3 Cloud Run: `min-instances=0` and a small `max-instances` (set in the deploy script) unless the spec says otherwise.
- S4.4 One region for everything, from env `GCP_REGION`. Never hardcode a region.
- S4.5 BigQuery queries filter by `event_id` and select only the columns they need. No `SELECT *` in scheduled or per-request code.
- S4.6 Scheduled jobs use the cadence in the spec or config. Never schedule more often than every minute.
- S4.7 Long or costly runs against real cloud resources (backfills, full re-enrichment, load tests) run only when the spec or the user says so.
