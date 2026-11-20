# collect-hn

Cloud Run Job that collects Hacker News stories and nested comment trees via the public HN Algolia API (`search_by_date` and `/api/v1/items/<id>`), appending raw payloads to BigQuery `raw_items` and checkpointing cursors in Firestore `collector_cursors`.

## Run locally

```bash
python -m services.collect_hn.main --event events/<event_id>.yaml --dry-run
python -m services.collect_hn.main --event events/<event_id>.yaml [--since 2026-11-16T00:00:00Z]
```

## Environment variables

See `.env.example`. No API key is required for HN Algolia.
