# VeriFact: narrative intelligence

**See how a story forms, spreads and changes, with evidence for every claim and no verdicts.**

VeriFact builds a live, time-stamped graph of the conversation around an event across X, Hacker News, YouTube and news. It groups posts into storylines and links each claim to the posts that support or contradict it. It shows where each storyline started, when it jumped platforms and who amplified it. A time slider replays the graph from the first official post onward. A before/after view shows what each storyline did after the subject responded.

Prototype for the **Google Startup Buildathon India 2026**.

> Status: prototype under active build. Items in `[brackets]` are filled in when they exist.

## Try it
- **Web app:** `[deployed link]`. Opens on the first configured event with a working replay and a sample brief.
- **Check a claim:** paste text or a URL, or upload a screenshot, on the web app's Check page.
- **Browser extension:** `[zip download]` · `[demo video]`. Right-click → claim card → "See the whole story".

## First case: Sarvam Token Factory
The engine is event-agnostic: a new event is one config file, `events/<event_id>.yaml`, with no code changes. The first configured case is Sarvam's Token Factory launch (T0 = @SarvamAI's launch post, 2026-11-16 13:37:16 UTC). It covers the storylines that followed and Sarvam's response about 24 hours later. Sarvam is not hardcoded anywhere.

## What you'll see
- **Storylines:** posts assigned to seeded storylines by embedding similarity, plus an honest "Unassigned" bucket.
- **Claims with evidence:** each claim is Supported, Contradicted, Mixed or Unverified, with counts and a quoted evidence span. Stance is checked across three prompt wordings, and the agreement is shown.
- **Spread:** platform of origin, platform jumps (only explicit links), top public amplifiers.
- **Before/after:** each storyline's volume and stance around the response. Descriptive, not causal.
- **Trust strip:** coverage, gold-set accuracy and the unassigned count, always on screen.
- **One-page brief** a comms lead can act on.

## Principles
1. Evidence, not verdicts. Every claim links to a source item. No true/false colouring.
2. Company statements are labelled self-reported unless independently confirmed.
3. Conflicts of interest are disclosed next to the quote.
4. No account is ever labelled a bot. Timing and duplication show only as neutral signals.
5. Public data only, within platform terms. Private individuals are pseudonymised. The identity of a person using Check never enters the graph.
6. No takedown or reporting tools. We suggest evidence, not counter-narratives.
7. No invented quotes, timestamps, numbers or post IDs.

The full, checkable version is in [`.agents/rules/principles.md`](.agents/rules/principles.md).

## Architecture
Cloud Scheduler → Cloud Run collectors → BigQuery (append-only raw record) → `enrich` (Gemini Flash-Lite + Gemini embeddings) → Spanner Graph (live graph, GQL) + BigQuery → Cloud Run API with server-sent events → web app (Sigma.js, WebGL). Check reports are kept in Firestore. Details: [`steering_docs/PRD.md`](steering_docs/PRD.md) `[and architecture.md]`.

## Repository map
| Path | What |
| --- | --- |
| `events/` | One config file per event |
| `services/` | Collectors, `enrich`, `api`, `check` (Cloud Run) |
| `verifact_core/` | Shared Python package |
| `web/`, `extension/` | Front end and Chrome extension |
| `infra/` | BigQuery schemas, Spanner DDL, deploy scripts |
| `steering_docs/` | PRD, specs (`specs/NN-name.md`) and design docs |
| `project_context/` | Decision log and event seeds |
| `_legacy/` | Read-only snapshot of earlier VeriFact code, kept for porting |
| `AGENTS.md`, `.agents/` | Instructions for the coding agents |

## Run locally
`[Added as services land. Each service's README.md has its own setup.]`

## Add a new event
Copy `events/sarvam-token-factory.yaml`, then set `event_id`, `t0`, the official accounts, entities, keywords and sources. The checklist is in [`project_context/SEEDS.md`](project_context/SEEDS.md) (Part 3).

## Limits we know about
- X recent search reaches back about 7 days, so the T0 window must be collected early. X reads are capped.
- Storyline and stance quality is lower for Hindi and Hinglish. The trust strip shows the measured accuracy.
- The time and form of Sarvam's response are still being confirmed, and the UI marks them as unconfirmed until they are.

## How this was built
Specs and decisions are written with Claude. Code is written by Google Antigravity, one spec at a time, and reviewed against the spec by Claude Code. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Team
`[names and links]`

## License
MIT. See [`LICENSE`](LICENSE).
