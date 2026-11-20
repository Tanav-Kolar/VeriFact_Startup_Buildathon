# `collect-x` Cloud Run Job

Scheduled, hard-capped live X API v2 collector job. Incrementally polls recent X posts for a configured event using per-query `since_id` cursors stored in Firestore `collector_cursors`, shares the cumulative `reads_total` cap with `x-backfill` under `(event_id, "x")`, and appends unmodified payloads directly to BigQuery `raw_items` with `captured_by = "api"`.

## Usage

```bash
python3 -m services.collect_x.main \
  --event events/<event_id>.yaml \
  [--limit N] \
  [--dry-run]
```

### Modes

1. **Dry run (zero HTTP calls, zero writes, no bearer token required):**
   ```bash
   python3 -m services.collect_x.main --event events/<event_id>.yaml --dry-run
   ```
2. **Capped billing test (`--limit 10`):**
   ```bash
   python3 -m services.collect_x.main --event events/<event_id>.yaml --limit 10
   ```
3. **Scheduled live run:**
   ```bash
   python3 -m services.collect_x.main --event events/<event_id>.yaml
   ```

## Behavior

- Refuses to start if `sources.x.enabled` is false, `sources.x.max_posts` is missing, or `X_BEARER_TOKEN` is unset (except `--dry-run`).
- Shares the exact `(event_id, "x")` read counter in Firestore `collector_cursors` with `x-backfill` so combined reads never exceed `sources.x.max_posts`.
- Exits `0` with zero requests when `now > t0 + window.post_hours` and `window.live` is false.
- Persists an incremental `since_id` cursor per `(event_id, "x", query_id)` (`live:thread:<id>`, `live:official:<handle>`, `live:keywords:<hash>`) without colliding with `x-backfill` step cursors.
- Queries `/2/tweets/search/recent` with only `tweet.fields=created_at,lang,author_id,conversation_id,referenced_tweets,entities,public_metrics`.
- Handles HTTP 429 with rate-limit waits, retries 5xx up to 3 times with exponential backoff, skips 401/403 per query with structured JSON logs, and emits a final `collect_x_done` JSON log line.

## Environment Variables

See `.env.example`. On Cloud Run, mount `X_BEARER_TOKEN` from Secret Manager as an environment variable. Never commit secrets.

- `X_BEARER_TOKEN`: X API v2 bearer token (required unless `--dry-run`).
- `X_API_BASE_URL`: Optional override (defaults to `https://api.x.com`).
- `X_HTTP_TIMEOUT_SECONDS`: Optional HTTP timeout in seconds (defaults to `30`).
- `GCP_PROJECT`: GCP project ID for BigQuery and Firestore.
- `BQ_DATASET`: BigQuery dataset name (defaults to `verifact`).
- `FIRESTORE_DATABASE`: Firestore database name (defaults to `(default)`).
- `LOG_LEVEL`: Logging level (defaults to `INFO`).

## Build Container

Run from the repository root:

```bash
docker build -f services/collect_x/Dockerfile -t collect-x .
```
