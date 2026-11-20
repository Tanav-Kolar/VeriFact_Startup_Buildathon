# VeriFact: agent guide

VeriFact is a narrative-intelligence prototype for the Google Startup Buildathon India 2026. It shows, as a live, time-stamped graph, how the story around an event formed and spread across platforms, with evidence for every claim and never a verdict. The first configured event is Sarvam's Token Factory launch (`events/sarvam-token-factory.yaml`). It is a config file, not a special case in code.

Also in scope: a "Check a claim" page and a thin browser extension that share one claim card with the graph. An MCP server only if a spec asks for it.

## Who does what
- Antigravity (you): writes the code, one spec at a time.
- Claude Code: reviews your work against the spec and `.agents/rules/`.
- Specs and decisions are written outside the repo and land in `steering_docs/` and `project_context/`.

## Stack
Cloud Scheduler → Cloud Run Jobs (collectors) → BigQuery `raw_items` (direct writes) → Cloud Run Job `enrich` (Gemini Flash-Lite + Gemini embeddings) → Spanner Graph (Enterprise, 100 PU, GQL) + BigQuery `enrichments` → Cloud Run `api` (JSON + server-sent events) → `web` (Sigma.js). Firestore holds Check reports. Fallback: replay frames precomputed from BigQuery. Pub/Sub, Memorystore and Dataflow are not used.

## Where things live
| Path | What |
| --- | --- |
| `.agents/rules/` | Auto-loaded rules: `principles.md`, `stack-and-conventions.md`, `secrets-and-cost.md` |
| `.agents/workflows/implement-spec.md` | `/implement-spec`: how to build a spec |
| `steering_docs/specs/NN-name.md` | The task you are building |
| `steering_docs/` | `PRD.md`, `architecture.md`, `data-model.md`, `api.md`, `design.md`, `evaluation.md`, `demo-script.md`, `BUILD_PLAN.md` |
| `project_context/DECISIONS.md` | Decision log, newest first |
| `project_context/SEEDS.md` | Event config schema and the Sarvam values |
| `events/<event_id>.yaml` | One file per event |
| `_legacy/` | Read-only snapshot of old code. Copy from it; never edit or import it |

**ECC pack** (`common-*`, `python-*`, `typescript-*` rules and the ECC workflows in `.agents/`) governs how code is written: test-first development and coverage, commit and PR format, research before building, code review. Follow it. `principles.md` and `secrets-and-cost.md` still win on product behaviour, data, privacy, cost and secrets. Tests never call the live X API or other paid APIs.

**When documents disagree:** newest entry in `DECISIONS.md` > the spec > other `steering_docs/` files > `PRD.md` > `_legacy/`. If the conflict affects what you build, stop and ask.

## How to treat a spec
1. Build only what the spec says. Nothing from "Out of scope", no extra features, no refactors outside its files.
2. Use the exact table, file, endpoint and field names it gives.
3. If a step needs a decision the spec does not make, or it hits an item under "Open questions", stop and ask. Do not guess.
4. Commit in the ECC format (`common-git-workflow.md`), with a `Spec: NN` line in the commit body.
5. You are done when every numbered acceptance criterion is met and you have reported each one as met or not met, with the evidence (test, command output, file and line).

## Never
- Write a true/false verdict, a bot label, or a risk score anywhere: data, API or UI.
- Invent quotes, timestamps, numbers, post IDs, handles or URLs, including in seed data, fixtures or UI placeholders. Unknown values stay as bracketed placeholders like `[n]`.
- Hardcode anything event-specific (names, handles, post IDs, keywords, T0) outside `events/` and `tests/fixtures/`.
- Commit secrets, `.env` files or keys. Put keys in front-end or extension code.
- Call the paid X API outside the capped collector, or in tests.
- Create cloud resources or paid services that the spec does not name.
- Scrape social platforms, or store the identity of a person using Check or the extension.
- Add takedown, report-to-platform or counter-messaging features.
- Modify `_legacy/`, `project_context/` or `steering_docs/` unless the spec says to.
