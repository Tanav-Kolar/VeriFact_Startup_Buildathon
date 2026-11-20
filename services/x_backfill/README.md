# `x-backfill` Cloud Run Job

One-off, hard-capped X API v2 backfill job (Spec 02). Collects X posts around an event's `t0` within the recent search window, writes unmodified payloads directly to BigQuery `raw_items`, and persists per-step cursors and cumulative read totals in Firestore `collector_cursors`.

## Usage

```bash
python -m services.x_backfill.main \
  --event events/<event_id>.yaml \
  [--limit N] \
  [--dry-run] \
  [--step thread|quotes|official|keywords]
```

### Recommended run order

1. **Dry run (zero HTTP calls, zero writes, no token required):**
   ```bash
   python -m services.x_backfill.main --event events/<event_id>.yaml --dry-run
   ```
2. **Billing check (`--limit 10`):**
   ```bash
   python -m services.x_backfill.main --event events/<event_id>.yaml --limit 10
   ```
3. **Full capped backfill:**
   ```bash
   python -m services.x_backfill.main --event events/<event_id>.yaml
   ```

## Fetch Order

Steps execute strictly in order, skipping any step already marked `step_done = true` in Firestore:

1. `thread`: post lookup for unique `official.posts` + `sources.x.thread_ids`, followed by recent search `conversation_id:<id>`.
2. `quotes`: `GET /2/tweets/:id/quote_tweets` for each `official.posts` ID.
3. `official`: recent search `from:<handle>` for each `official.accounts` handle.
4. `keywords`: recent search (`sort_order=relevancy`) built from `keywords.core`, `keywords.context` combined with `entities.org` aliases, minus `keywords.exclude`, with `-is:retweet`.

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
docker build -f services/x_backfill/Dockerfile -t x-backfill .
```
