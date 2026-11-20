# VeriFact API Service

Read-only FastAPI service providing event configuration, storylines, claim cards, trust metrics, frames, and live updates to the VeriFact web UI and browser extension.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/healthz` | Health check endpoint returning backend status |
| `GET` | `/events` | Lists all discovered events from `EVENTS_DIR` |
| `GET` | `/events/{event_id}` | Header data including name, T0, enabled sources, and turning points |
| `GET` | `/events/{event_id}/frames` | Graph frames for the timeline slider |
| `GET` | `/events/{event_id}/storylines` | Storylines for the storyline list and timeline |
| `GET` | `/events/{event_id}/storylines/{storyline_id}` | Detail for a specific storyline |
| `GET` | `/events/{event_id}/claims/{claim_id}` | Shared claim card component data |
| `GET` | `/events/{event_id}/before-after` | Descriptive comparison before and after a turning point |
| `GET` | `/events/{event_id}/trust` | Source coverage metrics and trust strip data |
| `GET` | `/events/{event_id}/live` | SSE stream for real-time item updates and heartbeats |
| `GET` | `/events/{event_id}/signals` | Neutral timing signals |

## Environment Variables

Configured via environment variables (or `.env`):

| Variable | Default | Description |
| --- | --- | --- |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed CORS origins |
| `GRAPH_BACKEND` | `frames` | Backend mode: `frames` (precomputed) or `spanner` |
| `EVENTS_DIR` | `events` | Directory containing event YAML configs |
| `LOG_LEVEL` | `INFO` | Structured JSON logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `SSE_HEARTBEAT_SECONDS` | `15.0` | Heartbeat frequency for the SSE stream |

## Running Locally

```bash
uvicorn services.api.main:app --host 0.0.0.0 --port 8000 --reload
```

## Running Tests

```bash
pytest tests/test_api_mvp.py
```
