# VeriFact API Service

Read-only FastAPI service providing event configuration, precomputed or Spanner Graph frames, storylines, item details, claim cards, neutral timing signals, before/after comparisons, grounded narrative briefs, trust metrics, and SSE live updates to the VeriFact web UI and browser extension.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/healthz` | Health check endpoint returning active `graph_backend` (`frames` or `spanner`) |
| `GET` | `/events` | Lists all discovered events from `EVENTS_DIR` |
| `GET` | `/events/{event_id}` | Header data including name, T0, enabled sources, turning points, and `now_hour` |
| `GET` | `/events/{event_id}/frames?from_hour=&to_hour=` | Hourly graph frames (`hour`, `nodes_added`, `edges_added`, `storyline_counts`) |
| `GET` | `/events/{event_id}/storylines?at_hour=` | Storyline summary rows up to `at_hour`, including `unassigned` |
| `GET` | `/events/{event_id}/storylines/{storyline_id}?at_hour=` | Storyline detail with `volume_by_hour` and cross-platform `jumps` |
| `GET` | `/events/{event_id}/items/{item_id}` | Single-item inspector payload (`<=300` char excerpt, verified claims, never `author_ref`) |
| `GET` | `/events/{event_id}/claims/{claim_id}?at_hour=` | Shared claim card component data (`supported \| contradicted \| mixed \| unverified`) |
| `GET` | `/events/{event_id}/signals` | Neutral timing signals with zero account identifiers |
| `GET` | `/events/{event_id}/before-after?turning_point=` | Descriptive before/after comparison around a turning point |
| `GET` | `/events/{event_id}/brief` | Cached structured brief JSON computed strictly from stored data |
| `GET` | `/events/{event_id}/trust` | Source coverage counts, `gold` metrics, `unassigned_count`, and `backfilled_share` |
| `GET` | `/events/{event_id}/live` | SSE stream (`status`, `storyline_counts`, `item_added`, and `heartbeat`) |

## Graph Backends

- `GRAPH_BACKEND=frames` (default and fallback): `FramesStore` loads `<event_id>.frames.json` and `<event_id>.snapshot.json` from `FRAMES_DIR` when present, or queries BigQuery (`frames`, `items`, `enrichments`, `account_map` with explicit column lists and `@event_id` filters), falling back cleanly to empty config-driven structures when no data has been collected yet.
- `GRAPH_BACKEND=spanner` with `SPANNER_LIVE=true`: `SpannerStore` delegates live graph reads to `verifact_core.spanner_graph.query_graph_at_hour`, returning identical API response shapes. Flipping `SPANNER_LIVE=false` reverts immediately to `FramesStore`.

## Environment Variables

Configured via environment variables (see `.env.example`):

| Variable | Default | Description |
| --- | --- | --- |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed CORS origins |
| `GRAPH_BACKEND` | `frames` | Backend mode: `frames` or `spanner` |
| `SPANNER_LIVE` | `false` | Kill-switch for live Spanner Graph reads |
| `LIVE_INGESTION` | `false` | Enables live SSE item/count broadcasts |
| `EVENTS_DIR` | `events` | Directory containing event YAML configs |
| `FRAMES_DIR` | *(unset)* | Optional local directory with `<event_id>.frames.json` / `.snapshot.json` |
| `GCP_PROJECT` | *(unset)* | GCP project ID for BigQuery / Spanner reads |
| `BQ_DATASET` | `verifact` | BigQuery dataset name |
| `LOG_LEVEL` | `INFO` | Structured JSON logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `SSE_HEARTBEAT_SECONDS` | `15.0` | Heartbeat frequency for the SSE stream |
| `PORT` | `8080` | HTTP listen port inside the container |

## Running Locally

```bash
uvicorn services.api.main:app --host 0.0.0.0 --port 8000 --reload
```

## Building the Container

```bash
docker build -f services/api/Dockerfile -t verifact-api .
```

## Running Tests

```bash
pytest tests/test_api_mvp.py
```
