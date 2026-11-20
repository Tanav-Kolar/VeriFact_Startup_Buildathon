# collect-gdelt

Cloud Run Job that collects news articles via the public GDELT DOC 2.0 API (`ArtList`), enforcing a 5-second shared rate limit, recursive window splitting down to 15-minute buckets when 250 records are returned, and SHA-1 normalized URL deduplication keys.

## Run locally

```bash
python -m services.collect_gdelt.main --event events/<event_id>.yaml --dry-run
python -m services.collect_gdelt.main --event events/<event_id>.yaml [--since 2026-11-16T00:00:00Z]
```

## Environment variables

See `.env.example`. No API key is required for GDELT DOC 2.0.
