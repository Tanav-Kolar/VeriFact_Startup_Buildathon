---
trigger: always_on
description: VeriFact services, folder layout, naming, Python/FastAPI and front-end conventions, and the config-driven event rule.
---

# Stack and conventions

## Services
| Name (Cloud Run) | Type | Folder | Does |
| --- | --- | --- | --- |
| `x-backfill` | Job, one-off | `services/x_backfill/` | Capped X backfill of the T0 window, written straight to BigQuery |
| `collect-x`, `collect-hn`, `collect-youtube`, `collect-gdelt` | Jobs, scheduled | `services/collect_<source>/` | Fetch new items since the cursor and append to `raw_items` |
| `fetch-pages` | Job | `services/fetch_pages/` | Article text and dated snapshots of official pages |
| `enrich` | Job, every 1–2 min | `services/enrich/` | Language, embedding, storyline, claims, stance; writes `enrichments`, upserts Spanner |
| `api` | Service | `services/api/` | Frames, storylines, item detail, brief data, SSE live updates |
| `check` | Service | `services/check/` | Claim card for the Check page and extension; reports in Firestore |
| `web` | Front end | `web/` | Workspace, before/after, brief, Check page |
| extension | Chrome MV3 | `extension/` | Right-click → claim card → "see the whole story" deep link |

Which sources are on comes from the event config (`sources.<name>.enabled`), not from code.

## Folder layout
```
events/<event_id>.yaml        # one per event
verifact_core/                # shared Python package: config loader, item schema, BigQuery/Spanner/Gemini clients
services/<service_name>/      # main.py, Dockerfile, requirements.txt, README.md
web/  extension/
infra/                        # BigQuery schemas, Spanner DDL, deploy and scheduler scripts
tests/  tests/fixtures/       # fixtures are synthetic and marked so
```
Services import shared code only from `verifact_core`. Never import from `_legacy/`. Copy what you need, then apply the port fixes below.

## Names
- Cloud Run resources: kebab-case (`collect-hn`). Python folders and modules: snake_case.
- BigQuery dataset `verifact`. Tables: `raw_items` (append-only), `items` (view, deduplicated), `enrichments`. Use the exact column names in `steering_docs/data-model.md`.
- Item ID: `<platform>:<source_id>`, for example `hn:41234567`.
- Graph node labels: `Event, Post, Account, Article, Outlet, Storyline, Claim`. Edge labels: `REPLIED_TO, QUOTED, LINKS_TO, AUTHORED_BY, BELONGS_TO, SUPPORTS, CONTRADICTS`.
- Every node and edge has `event_id` and a timestamp (`valid_from`).

## Event-agnostic (config-driven)
- Every job, the API and the web app take the event from the config: CLI `--event events/<id>.yaml` for jobs, env `EVENT_ID` for services.
- No event-specific literal (company or product names, handles, post IDs, keywords, T0, storyline names) outside `events/` and `tests/fixtures/`. Check: `grep -rni sarvam --exclude-dir={events,tests,_legacy,steering_docs,project_context,.git} .` returns nothing.
- Load and validate the config with one Pydantic model in `verifact_core`. Unknown or missing required keys fail at startup with a clear error.
- Adding an event must need only a new YAML file. If a spec seems to need code changes per event, stop and ask.

## Data rules
- All timestamps are UTC, ISO 8601 with `Z`. Store `created_at` (platform) and `collected_at` (ours). Replay uses `created_at`. `hours_from_t0` is computed from `t0.utc` in the config.
- `raw_items` stores payloads unmodified. Never run UPDATE or DELETE on it. Dedupe in the `items` view.
- Every job is idempotent: a re-run does not duplicate rows or re-bill API reads. Collectors keep a cursor per event, source and query in Firestore `collector_cursors`, using the helpers in `verifact_core/cursors.py`. X read counts live there too.
- Every `enrichments` row stores `model_id` and `prompt_version`.
- Collectors write to BigQuery directly, in batches. Pub/Sub is not used (DECISIONS 2026-11-20 late #10; this supersedes PRD IR-3 and IR-4).
- Graph backend is chosen by env `GRAPH_BACKEND=spanner|frames`. The `frames` path (precomputed from BigQuery) must keep working, because it is the fallback.

## Python
- Python 3.12. FastAPI + Pydantic v2 for services. Type hints on all public functions.
- Format and lint with `ruff`. Tests with `pytest`; external APIs are mocked or use recorded fixtures.
- Settings come from env vars through `pydantic-settings`. No values are hardcoded: region, project ID, model IDs, dataset names and limits all come from env or config.
- Log structured JSON to stdout (Cloud Logging). No `print`. Never log secrets or user identity (P5.4).
- Make optional integrations (logging sinks, caches) truly optional: the service starts and works without them.
- Every service has `/healthz` (services) or exits non-zero on failure (jobs).

## API
- JSON with snake_case fields. Endpoint paths and response shapes exactly as in `steering_docs/api.md` or the spec.
- Live updates use server-sent events, never WebSockets.
- Errors return `{"error": {"code", "message"}}` with the right HTTP status.

## Front end and extension
- Graph: Sigma.js (WebGL) with graphology. Do not swap the graph library without a spec.
- Never put model output or source text into `innerHTML`. Use `textContent` or an escaping template.
- One claim card component, shared by the graph inspector, the Check page and the extension.
- Colour is never the only carrier of meaning. The time slider is keyboard-operable.
- Placeholder data in UI stays bracketed (`[n]`) until real.

## Legacy port fixes (when copying from `_legacy/`)
Make Redis and BigQuery logging optional, update Gemini model IDs to the env-configured ones, fix the Retina screenshot crop bug (scale by `devicePixelRatio`), and escape model output instead of using `innerHTML`. The old `web-app/` is out of scope.
