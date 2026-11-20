# DECISIONS

Newest first. A decision marked SUPERSEDED stays for history; follow the decision that replaced it.

## 2026-11-20 (late afternoon): budget, Spanner, judging format

9. **Budget.** $5 of GCP credits expire at midnight 2026-11-20 (IST); after that, costs go to the
   user's card. Spend the credits before midnight on Gemini enrichment of the collected corpus
   (Flash-Lite + embeddings, results saved to BigQuery) and the first Spanner hours. Develop prompts
   on the AI Studio free tier with public data only. Use premium models (e.g. Argon) for coding in
   Antigravity, not in the runtime pipeline. Set budget alerts.
10. **Spanner Graph is back in (replaces afternoon #5).** Enterprise edition (required for Spanner
    Graph and vector search), 100 processing units, one regional instance created once and kept
    running through judging (about $3/day), deleted afterwards. Replay frames precomputed from
    BigQuery stay as the fallback if Spanner setup takes more than about 2 hours.
    Memorystore and Pub/Sub are not used.
11. **Judging format.** Judges get a deployed web-app link and the GitHub repository.
    - The web app is the primary entry point and includes a "Check a claim" page (paste text/URL or
      upload a screenshot) showing the same claim card as the extension.
    - The extension ships as a downloadable zip plus a short demo video.
    - The deployed link opens on the Sarvam event with a working replay and a sample brief; live
      ingestion is an add-on, not a dependency.
    - Before the repo goes public: fill `.gitignore` (.env*, .venv, __pycache__, node_modules),
      rotate the ScraperAPI key, keep `_legacy/` labelled, add a judge-facing README.
    - Public Gemini-backed endpoints are rate-limited and cached to cap cost.
12. **Experience direction (to be finalised in design.md).** One claim card reused everywhere
    (extension, Check page, graph). Workspace = storyline rail, graph centre, time slider with
    response marker and live pulse, inspector panel, trust strip (coverage, gold-set accuracy,
    unassigned count). One-page brief. No red/green true/false colouring.

## 2026-11-20 (afternoon): scope, 20-hour plan, legacy reuse

1. **Product story (B2C2B).** Consumer first, enterprise later, on one shared evidence graph.
   Jury progression: browser extension (live) -> MCP server and X bot (demonstrated plan) ->
   enterprise narrative and decision intelligence (prototype on the Sarvam Token Factory case).
2. **Scope (replaces 2026-11-20 morning #2).** In scope: a thin browser extension (right-click,
   claim card, "see the whole story" deep link into the narrative graph) and the enterprise
   narrative view (graph, time slider, per-storyline before/after Sarvam's response, one-page brief).
   MCP server: thin wrapper on the same API, only if time allows. X bot: plan only, pending
   verification of X's automation rules. Trace and Telegram/WhatsApp bots: not built.
3. **Check (replaces 2026-11-20 morning #1, "Check only as a panel").** Check is the extension's
   backend and also supplies the claim card shown on graph nodes. Same card format in both places.
4. **20-hour build order.** (a) Collectors start first and write raw items to BigQuery
   (X window for T0 closes about 2026-11-23 13:37 UTC). (b) Revive the old Check pipeline on a
   fresh Cloud Run service, timeboxed to 3 hours; fallback = one FastAPI service with Gemini +
   Search grounding doing the same four steps. (c) Narrative view built as a replay from BigQuery
   with precomputed frames; live ingestion shown as an add-on. (d) Deep link, rehearsal, roadmap slide.
5. SUPERSEDED by late afternoon #10. **Graph store (amends 2026-11-20 morning #4, stack).** Spanner Graph is optional for the
   prototype and added only if time allows; reason: 20 hours to submission. Replay is served from
   BigQuery-precomputed frames via a Cloud Run API. Firestore keeps Check reports.
6. **Evidence, not verdicts, in Check output.** Replace the single verdict tag
   (True/False/Misleading/...) with per-claim evidence status (Supported / Contradicted / Mixed /
   Unverified) plus evidence counts, and a self-reported flag on company statements.
   Show source-type labels (company statement, press, social, investor with conflict) instead of a
   single credibility score.
7. **Legacy reuse.** Pre-existing code lives in `_legacy/` (read-only snapshot, no git history,
   secrets removed). New code is ported into new service folders. Required fixes when porting:
   make Redis and BigQuery logging optional, update model IDs, fix the Retina crop bug
   (`devicePixelRatio`), escape model output instead of `innerHTML`. Rotate the ScraperAPI key.
   The old `web-app/` is out of scope.
8. **Privacy of the shared graph.** Only the public content being checked enters the shared
   evidence graph, never the user's identity.

## 2026-11-20 (morning)

1. SUPERSEDED by afternoon #3. Lead demo = Narratives (live Token Factory graph). Check appears
   only as a panel on graph nodes. Radar and Trace are deferred.
2. SUPERSEDED by afternoon #2. Scope = the narrative-intelligence visualisation tool only.
   The browser extension, MCP server and bots are out of scope for now.
3. The engine is event-agnostic and config-driven. Sarvam Token Factory is the first configured case.
4. AMENDED by afternoon #5. Stack = Cloud Scheduler + Cloud Run collectors -> Pub/Sub ->
   Cloud Run enrichment (Gemini) -> Spanner Graph + BigQuery -> Cloud Run API -> web graph front end.
5. Tool split = Claude project for specs, Antigravity for code, Claude Code for review.
