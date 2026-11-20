# _legacy: pre-existing VeriFact code (read-only)

This folder is a snapshot of code written **before** the Google Startup Buildathon India 2026.
It is kept for reference and porting. Do not edit files here; port what you need into the new
service folders so the new work stays clearly separate.

## Provenance
- Source: the earlier VeriFact repository (`~/Verifact`), commit `0596f61`.
- Copied on 2026-11-20 as files only (no git history).
- Excluded: `web-app/`, `.env*` files, `.DS_Store`, screenshots.
- A commented-out hard-coded ScraperAPI key was removed from
  `misinformation-app/sub_agents/claims_extractor_agent/agent.py` in this copy.
  The key still exists in the old repository's history and must be rotated.

## Contents
- `misinformation-app/`: FastAPI + Google ADK pipeline:
  claims extractor -> (web search agent || Fact Check API agent) -> report generator.
  Media is first described/OCR'd/transcribed by Gemini. Caches in Redis, stores reports in
  Firestore, logs to BigQuery.
- `browser-extension/chrome/`: Manifest V3 extension. Right-click -> drag a box -> sends the
  cropped screenshot and page URL to the backend -> shows the report card on the page.

## Known issues (as of 2026-11-20)
1. Endpoint returns 503 unless Redis (Memorystore), Firestore, BigQuery, the embedding model and
   Gemini all initialise. Memorystore also needs VPC networking.
2. Model IDs (`gemini-2.0-flash`, `text-embedding-005`) are a year old; check they are still served.
3. No source-credibility agent exists in code, although the report prompt expects its output.
4. Report schema forces a single verdict tag (True/False/Misleading/...), which conflicts with the
   project's evidence-not-verdicts principle.
5. Extension `config.js` points at a Cloud Run URL that no longer exists; `host_permissions` is a
   placeholder domain.
6. Screenshot crop ignores `devicePixelRatio`, so it crops the wrong area on Retina displays.
7. `display.js` renders model output with `innerHTML`; escape or sanitise before reuse.
