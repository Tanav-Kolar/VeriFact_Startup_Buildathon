# Build plan

The order to build in, deadlines and fallbacks. Data that can be lost comes first. Spec numbers match the table in `PRD.md` (Spec breakdown for Antigravity). Each spec goes in `specs/NN-name.md` and is built with `/implement-spec`.

## Hard deadlines
| When (UTC / IST) | What | Consequence if missed |
| --- | --- | --- |
| 2026-11-20 18:30 / 2026-11-21 00:00 | $5 GCP credits expire | Gemini enrichment and Spanner hours go on the card |
| 2026-11-23 13:37 / 19:07 | X recent search loses the T0 window | Only paid full-archive search remains |
| `[submission time]` | Submission | — |

## Phases
| Phase | Specs | Goal | Timebox | Fallback |
| --- | --- | --- | --- | --- |
| A. Capture | 01 config + tables, 02 x-backfill, 03 BigQuery writer, 04 collectors (HN, YouTube, GDELT), 05 fetch-pages | Every irreplaceable item stored | 4 h | Run x-backfill before anything else, even if the other collectors slip |
| B. Enrich | 06 enrich, gold-set labelling (in parallel, by hand) | Storylines, claims, stance in `enrichments` | 4 h | Start enrichment before the credits expire, even with draft prompts (re-run later with a new `prompt_version`) |
| C. Serve | 07 Spanner schema + upsert, 08 frame-builder + api | Replay frames and API | 4 h | If Spanner setup passes about 2 h, ship `GRAPH_BACKEND=frames` only |
| D. Show | 09 workspace, 10 timeline, 11 inspector + claim card + trust strip, 12 before/after, 13 brief | The demo path end to end | 6 h | Cut FR-3.5 settings, FR-3.7 ripple and the live pulse first |
| E. Check | 14 check service + page, 15 extension + deep link | Thin consumer path | 3 h (14 is timeboxed to 3 h on its own) | One FastAPI service with Gemini + Search grounding |
| F. Ship | 16 repo hygiene + README | Public repo and deployed link | 1 h | — |

Phases B and D can overlap once 08's response shapes are fixed (`api.md`). Gold-set labelling is human work and runs alongside A–C.

## Spec status
| # | Spec | Depends on | Status |
| --- | --- | --- | --- |
| 01 | `raw_items` table, `items` view, event config loader | — | Draft |
| 02 | `x-backfill` | 01, 03 | Draft |
| 03 | Shared BigQuery writer and cursor store in `verifact_core` | 01 | Draft |
| 04 | Collectors: HN, YouTube, GDELT | 01, 03 | Draft |
| 05 | `fetch-pages` (Firecrawl) | 04 | Draft |
| 06 | `enrich` | 01, 03 | Draft |
| 07 | Spanner Graph schema and upsert | 06 | Not written |
| 08 | Frame builder and `api` | 06, 07 | Not written |
| 09 | Workspace: rail, graph, settings | 08 | Not written |
| 10 | Timeline and slider | 08 | Not written |
| 11 | Inspector, claim card, trust strip | 08 | Not written |
| 12 | Before/after | 08 | Not written |
| 13 | Brief | 08 | Not written |
| 14 | Check service and page | — | Not written |
| 15 | Extension and deep link | 14, 09 | Not written |
| 16 | Repo hygiene and judge README | — | Not written |

Update the Status column as specs move: Draft → Ready → Built → Reviewed.

## Human tasks (not for agents)
- Set the X spend limit to $10 in the X developer console. Rotate the ScraperAPI key. Turn on budget alerts.
- Create the Spanner instance once (Enterprise, 100 PU) and delete it after judging.
- Confirm the time and form of Sarvam's response, and the status of the reported government equity stake.
- Label the gold set (two people).
- Pick and rehearse the second event.
