# fetch-pages

Service for fetching article body markdown and dated snapshots of official pages via Firecrawl (Spec 05).

## Overview
- Scrapes the full markdown text of news articles discovered by GDELT.
- Takes dated snapshots of configured official pages (`official.urls`) once per UTC day.
- Operates under strict credit limits with `FIRECRAWL_MAX_TOTAL` and `FIRECRAWL_MAX_PER_RUN`.
- Automatically enforces P5.1 by blocking all social platform domains (`x.com`, `twitter.com`, `reddit.com`, etc.).
- Never retries a URL that has failed 3 times (tracked in the Firestore cursor).
- Saves full payloads to BigQuery `raw_items` with `source = 'firecrawl'`.

## CLI Usage
```bash
# Run both articles and official page snapshots
python -m services.fetch_pages.main --event events/<event_id>.yaml

# Run articles only
python -m services.fetch_pages.main --event events/<event_id>.yaml --mode articles

# Run official snapshots only
python -m services.fetch_pages.main --event events/<event_id>.yaml --mode official

# Dry-run without making Firecrawl calls or writing to BigQuery
python -m services.fetch_pages.main --event events/<event_id>.yaml --dry-run
```

## Environment Variables
- `FIRECRAWL_API_KEY`: Firecrawl API key (required for live runs).
- `FIRECRAWL_MAX_TOTAL`: Hard project monthly credit budget (required; refuses to start without it).
- `FIRECRAWL_MAX_PER_RUN`: Maximum credits to use in a single job run (default: `50`).
- `GCP_PROJECT`: GCP Project ID.
- `BQ_DATASET`: BigQuery dataset name (default: `verifact`).
- `FIRESTORE_DATABASE`: Firestore database name (default: `(default)`).
