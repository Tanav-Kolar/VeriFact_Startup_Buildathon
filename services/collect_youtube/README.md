# collect-youtube

Cloud Run Job that collects YouTube videos, video channel statistics (never commenter channels), and comment threads via YouTube Data API v3, appending raw payloads to BigQuery `raw_items` and checkpointing cursors in Firestore `collector_cursors`.

## Run locally

```bash
python -m services.collect_youtube.main --event events/<event_id>.yaml --dry-run
YOUTUBE_API_KEY=... python -m services.collect_youtube.main --event events/<event_id>.yaml [--since 2026-11-16T00:00:00Z]
```

## Environment variables

See `.env.example`. `YOUTUBE_API_KEY` is mounted from Secret Manager in Cloud Run. `YOUTUBE_SEARCHES_PER_RUN` defaults to `4`.
