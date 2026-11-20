# `frame-builder` Cloud Run Job

Precomputes hourly replay frames (`frames` table in BigQuery) and derived snapshot JSON files (`<event_id>.frames.json`, `<event_id>.snapshot.json`) from collected `items`, `enrichments`, and `account_map`.

## CLI Usage

```bash
python3 -m services.frame_builder.main \
  --event events/<event_id>.yaml \
  [--input-json path/to/bundle.json] \
  [--output-dir path/to/frames_dir] \
  [--dry-run]
```

## Environment Variables

See `.env.example`:
- `GCP_PROJECT`: Google Cloud project ID
- `BQ_DATASET`: BigQuery dataset name (default `verifact`)
- `PROMPT_VERSION`: Enrichment prompt version (default `v1`)
