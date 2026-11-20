# Specs

One file per Antigravity task: `NN-name.md`. Build with `/implement-spec steering_docs/specs/NN-name.md`. Status lives in `../BUILD_PLAN.md`.

Every spec has six sections:
1. **Goal:** one sentence.
2. **Inputs and outputs:** exact table, file, endpoint and field names.
3. **Steps:** in order.
4. **Acceptance criteria:** numbered, each one checkable by a reviewer.
5. **Out of scope.**
6. **Open questions:** the agent stops at any that affect the build.

Names must match `../data-model.md` and `../api.md`. If a spec changes a name, it updates those files in the same commit.

| # | File | Status |
| --- | --- | --- |
| 01 | `01-config-and-raw-items.md` | Draft |
| 02 | `02-x-backfill.md` | Draft |
| 03 | `03-bq-writer-and-cursors.md` | Draft |
| 04 | `04-collectors-hn-youtube-gdelt.md` | Draft |
| 05 | `05-fetch-pages.md` | Draft |
| 06 | `06-enrich.md` | Draft |
