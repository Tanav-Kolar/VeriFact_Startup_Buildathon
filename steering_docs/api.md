# API

The `api` service (read-only, public) and the `check` service. Every path and field here is *proposed* until the API spec (#8) or Check spec (#14) fixes it. After that, specs and this file must match. JSON uses snake_case. Times are ISO 8601 UTC with `Z`. Errors are `{"error": {"code": "...", "message": "..."}}`.

## Shared shapes

**Node** (as in frames and graph responses)
```json
{"id": "x:212...", "type": "post", "platform": "x", "kind": "reply", "created_at": "...", "hour": 5,
 "storyline_id": "chinese-models", "reach": 1200, "is_official": false, "self_reported": false,
 "conflict": null, "lang": "en", "label": "Account A17"}
```
`type`: `post | article | account | outlet | storyline | claim`. `label` is the public name or a pseudonym (data-model.md, Privacy).

**Edge**
```json
{"source": "x:1", "target": "x:2", "type": "replied_to", "created_at": "...", "hour": 5}
```
`type`: `replied_to | quoted | links_to | authored_by | belongs_to | supports | contradicts`.

**Claim card** (identical in graph, Check page and extension)
```json
{"claim_id": "subsidised-gpus", "text": "...", "status": "contradicted",
 "counts": {"supporting": 3, "contradicting": 5, "neutral": 2},
 "self_reported": true, "self_reported_note": "Company statement, not independently confirmed",
 "evidence": [{"item_id": "...", "url": "...", "platform": "news", "source_type": "company_statement",
               "stance": "contradicts", "span": "exact text from the item", "created_at": "...",
               "conflict": null}],
 "storyline_id": "subsidised-gpus", "deep_link": "/e/sarvam-token-factory?claim=subsidised-gpus"}
```
`status`: `supported | contradicted | mixed | unverified`. `source_type`: `company_statement | press | social | investor_with_conflict`. When `conflict` is set: `{"person": "...", "relationship": "..."}`.

## `api` endpoints
| Method and path | Returns |
| --- | --- |
| `GET /healthz` | `{"ok": true, "graph_backend": "frames"}` |
| `GET /events` | `[{event_id, name, t0_utc}]` |
| `GET /events/{event_id}` | Header data: `name, t0_utc, config_file, sources (enabled list), turning_points [{id, label, at_utc, confirmed, form}], now_hour` |
| `GET /events/{event_id}/frames?from_hour=&to_hour=` | `[{hour, nodes_added, edges_added, storyline_counts}]` |
| `GET /events/{event_id}/storylines?at_hour=` | Per storyline: `id, name, colour_index, count, first_seen {item_id, platform, hour}, platforms_reached [{platform, hour}], stance_mix, agreement, top_amplifiers [{label, reach}]`; plus the `unassigned` row |
| `GET /events/{event_id}/storylines/{storyline_id}?at_hour=` | The row above plus `volume_by_hour` and `jumps [{from_item, to_item, hour}]` |
| `GET /events/{event_id}/items/{item_id}` | `created_at, collected_at, collection_lag_s, platform, url, excerpt, storyline_id, claims [{claim_id, stance, span}], reach, tags` |
| `GET /events/{event_id}/claims/{claim_id}?at_hour=` | Claim card |
| `GET /events/{event_id}/signals` | Neutral timing signals: `[{storyline_id, n_items, window_minutes, text}]`. No account IDs |
| `GET /events/{event_id}/before-after?turning_point=` | Per storyline: `volume_before_per_h, volume_after_per_h, pattern, addressed (bool)`, plus `note` ("Descriptive comparison, not a causal estimate.") and the turning point's `confirmed` and `form` |
| `GET /events/{event_id}/brief` | Brief JSON: `summary [{text, item_ids}], storylines, claims, watch, evidence_to_cite, method_and_limits, coverage, generated_at, model_id, prompt_version`. Cached; regenerated only on request by the owner (*proposed*) |
| `GET /events/{event_id}/trust` | `coverage {x: {count, cap}, hn, youtube, news}, gold {assignment_accuracy, stance_macro_f1, run_date}, unassigned_count, backfilled_share` |
| `GET /events/{event_id}/live` | SSE stream (see below) |

Rules:
- `at_hour` limits every count and list to items with `created_at` up to the end of that hour (PRD release criterion 3).
- Responses never include `author_ref`, user data or full article text (only `excerpt`, 300 characters max, *proposed*).
- Same shapes with `GRAPH_BACKEND=frames` and `spanner`.

## SSE `GET /events/{event_id}/live`
Event types: `item_added` (data: Node plus its edges), `storyline_counts` (`{storyline_id: count}`), `heartbeat` every 15 s. If live ingestion is off, the stream sends only heartbeats and `{"live": false}` once.

## `check` service
| Method and path | Body or returns |
| --- | --- |
| `POST /check` | Body: `{"text": "..."}` or `{"url": "..."}`, or multipart with `image`. Returns `{report_id, claims: [claim card], matched_event_id or null}` |
| `GET /check/{report_id}` | The stored report |
| `GET /healthz` | |

Rules:
- Rate-limited per client and cached by content hash; global daily cap (S3.5). Limits come from env.
- Requests about a person ("is @x a shill?") return no assessment, and nothing is stored in the shared graph (PRD behavior case #11).
- Input over the length limit is rejected with `413` (limit from env).
- Content from user-supplied URLs is treated as data, never as instructions (PRD red-team #1, #10).

## Web routes and deep link
`/e/{event_id}` (workspace), `/e/{event_id}/before-after`, `/e/{event_id}/brief`, `/check`. Deep link from the extension: `/e/{event_id}?storyline={id}&claim={id}&t={hour}`.
