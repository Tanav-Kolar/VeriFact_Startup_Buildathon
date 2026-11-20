# Data model

Exact names for tables, fields and graph labels. Specs use these names; if a spec needs a new field, the spec adds it here too. *Proposed* means it's a recommendation, not yet agreed.

## Identifiers and time
- `event_id`: slug from the config, for example `sarvam-token-factory`. On every row, node and edge.
- `item_id`: `<platform>:<source_id>`, for example `x:2122323115535720921`, `hn:41234567`, `yt:dQw4w9WgXcQ`, `gdelt:<sha1 of url>`.
- `platform` enum: `x | hn | youtube | news | web | manual`. (`news` covers GDELT articles with their Firecrawl text; `web` covers dated snapshots of the subject's own pages.)
- All timestamps are UTC `TIMESTAMP`. `created_at` = posted on the platform; `collected_at` = fetched by us. `hours_from_t0 = (created_at − t0.utc) / 3600`, which can be negative for pre-T0 items.

## BigQuery (dataset `verifact`)

### `raw_items` (append-only; never UPDATE or DELETE)
| Column | Type | Notes |
| --- | --- | --- |
| `event_id` | STRING | Required |
| `source` | STRING | Collector source: `x`, `hn`, `youtube`, `gdelt`, `firecrawl`, `manual` |
| `source_id` | STRING | Platform ID, or the URL hash for articles |
| `query_id` | STRING | Which configured query or fetch step produced it (*proposed*) |
| `run_id` | STRING | Collector run that wrote it (*proposed*) |
| `captured_by` | STRING | `api` (live collector) or `backfill` |
| `collected_at` | TIMESTAMP | Required |
| `payload` | JSON | Response object exactly as received |

Partition by `DATE(collected_at)`, cluster by `event_id, source`.

### `items` (view; one row per item, deduplicated on `source`+`source_id`, earliest `collected_at` kept)
`item_id, event_id, platform, kind, url, author_ref, author_followers, created_at, collected_at, text, lang, parent_id, quote_of, links (ARRAY<STRING>), engagement (STRUCT<likes, reposts, replies, views, score INT64>), hours_from_t0, captured_by, time_approximate (BOOL)`

- `kind`: `post | reply | quote | comment | video | story | article | official_page`.
- `author_ref`: the platform's author ID. Internal only; the API maps it to a public name or pseudonym (see Privacy).
- `time_approximate`: true when an article has no publish time and GDELT's first-seen time is used.
- `parent_id`, `quote_of`: `item_id`s, or NULL.

### `enrichments` (one row per item and `prompt_version`)
| Column | Type | Notes |
| --- | --- | --- |
| `item_id`, `event_id` | STRING | |
| `enriched_at` | TIMESTAMP | |
| `model_id`, `embedding_model_id`, `prompt_version` | STRING | Required (reproducibility) |
| `lang` | STRING | BCP-47, `hi-Latn`/`hi-en` for Hinglish (*proposed*: `hi-en`) |
| `embedding` | ARRAY<FLOAT64> | |
| `storyline_id` | STRING | Nearest centre above the threshold, else `unassigned` |
| `storyline_similarity` | FLOAT64 | |
| `second_storyline_id`, `second_similarity` | STRING, FLOAT64 | Stored, not shown |
| `off_topic` | BOOL | Matched `keywords.exclude` or below the similarity floor |
| `claims` | ARRAY<STRUCT<claim_id STRING, stance STRING, evidence_span STRING, votes ARRAY<STRING>, agreement FLOAT64>> | `stance`: `supports | contradicts | neutral`. `votes`: one per prompt paraphrase (3). `agreement` = share of votes matching the majority. `evidence_span` must be an exact substring of `items.text` |
| `entities` | ARRAY<STRING> | Config entity keys found |
| `is_official` | BOOL | Author in `official.accounts`, or text quotes `official.spokespeople` |
| `conflict_person` | STRING | Name from `conflicts_of_interest`, or NULL |

Partition by `DATE(enriched_at)`, cluster by `event_id, storyline_id`. Embeddings are `gemini-embedding-001` with `output_dimensionality = 768` and `task_type = CLUSTERING` (model and size from env, so they can change with a new `prompt_version`).

Items with no text are not enriched beyond `lang = und`, and they count as Unassigned.

### `storyline_centres`
| Column | Type | Notes |
| --- | --- | --- |
| `event_id`, `storyline_id` | STRING | |
| `embedding` | ARRAY<FLOAT64> | Normalised mean of the seed-sentence embeddings |
| `seed_hash` | STRING | Hash of the seed sentences and embedding model; recompute when it changes |
| `embedding_model_id` | STRING | |
| `computed_at` | TIMESTAMP | |

Cluster by `event_id`. The latest row per `(event_id, storyline_id, seed_hash)` wins. With about 10 storylines, assignment is an exact cosine comparison in memory inside `enrich`. No vector index is needed anywhere.

### `frames` (*proposed*; written by `frame-builder`)
| Column | Type | Notes |
| --- | --- | --- |
| `event_id` | STRING | |
| `hour` | INT64 | Hours from T0; one row per hour from `−window.pre_hours` to now |
| `built_at` | TIMESTAMP | |
| `nodes_added` | JSON | Nodes whose `created_at` falls in this hour, in the API's node shape |
| `edges_added` | JSON | Edges whose later endpoint falls in this hour |
| `storyline_counts` | JSON | `{storyline_id: cumulative_count}` at the end of the hour |

Partition by `event_id` is not possible (STRING), so cluster by `event_id, hour`. `frame-builder` rewrites an event's frames by writing a new `built_at` batch; the API reads the latest `built_at` per event (this table is derived, so replacing rows here does not break the append-only rule, which applies to `raw_items` only).

The client builds the graph at hour *h* by applying frames up to *h*. Nothing in a frame may have `created_at` later than the end of its hour.

### `account_map` (internal only)
`event_id, platform, author_ref, account_n INT64, first_seen TIMESTAMP`. One row per author per event. `account_n` is assigned in order of first appearance and gives the pseudonym `Account A<account_n>` and the graph node ID `<platform>:acct:<account_n>`. Only `enrich` and `frame-builder` read it; the API never returns `author_ref`.

### `events` (tracking, *proposed*)
`event_name, event_id, ts, session_id, props (JSON)`. No user identity.

## Firestore
- `collector_cursors/{event_id}__{source}__{query_id}`: `cursor` (last ID or timestamp), `updated_at`, and for X `reads_total` (running count of billed reads for this event, never reset). *Recommended* over a BigQuery table: single-document atomic increments make the X read cap safe across reruns.
- `check_reports/{report_id}`: the claim card result plus `input_type`, `created_at`, `content_hash`. No user ID, IP or install ID (P5.4).

## Spanner Graph (live graph)
Schemaless design: one node table, one edge table, dynamic labels and JSON properties. Reasons: one upsert path, new node or edge types need no DDL change, and it fits the 2-hour Spanner timebox. Columns used for filtering are typed; everything else lives in `properties`.

### Tables (database `verifact`, *proposed* name)
```sql
CREATE TABLE GraphNode (
  event_id     STRING(64)  NOT NULL,
  node_id      STRING(256) NOT NULL,   -- item_id, account_id, outlet_id, storyline_id, claim_id, or event_id
  label        STRING(32)  NOT NULL,   -- Event | Post | Account | Article | Outlet | Storyline | Claim
  valid_from   TIMESTAMP   NOT NULL,   -- created_at of the item that made it appear
  platform     STRING(16),
  storyline_id STRING(64),
  properties   JSON,
  updated_at   TIMESTAMP   NOT NULL OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY (event_id, node_id);

CREATE TABLE GraphEdge (
  event_id   STRING(64)  NOT NULL,
  node_id    STRING(256) NOT NULL,     -- source node
  edge_label STRING(32)  NOT NULL,     -- REPLIED_TO | QUOTED | LINKS_TO | AUTHORED_BY | PUBLISHED_BY | BELONGS_TO | SUPPORTS | CONTRADICTS
  dest_id    STRING(256) NOT NULL,     -- destination node
  valid_from TIMESTAMP   NOT NULL,
  properties JSON,
  updated_at TIMESTAMP   NOT NULL OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY (event_id, node_id, edge_label, dest_id),
  INTERLEAVE IN PARENT GraphNode ON DELETE CASCADE;

CREATE INDEX GraphNodeByTime ON GraphNode (event_id, valid_from);
CREATE INDEX GraphNodeByStoryline ON GraphNode (event_id, storyline_id, valid_from);
CREATE INDEX GraphEdgeByDest ON GraphEdge (event_id, dest_id);

CREATE PROPERTY GRAPH verifact_graph
  NODE TABLES (
    GraphNode KEY (event_id, node_id)
      DYNAMIC LABEL (label)
      DYNAMIC PROPERTIES (properties)
  )
  EDGE TABLES (
    GraphEdge KEY (event_id, node_id, edge_label, dest_id)
      SOURCE KEY (event_id, node_id) REFERENCES GraphNode (event_id, node_id)
      DESTINATION KEY (event_id, dest_id) REFERENCES GraphNode (event_id, node_id)
      DYNAMIC LABEL (edge_label)
      DYNAMIC PROPERTIES (properties)
  );
```
Spec 07 must check this DDL against the current Spanner Graph docs (dynamic labels, JSON properties, key clauses) before applying it, and record any change here.

### Rules
- `event_id` leads every key, so one database holds many events and every query filters by event.
- `valid_from` is always set, so a GQL query `WHERE n.valid_from <= @t` replays the graph at time *t*.
- Edges are written only when both endpoints exist. Edges whose destination is missing (for example a reply to a post we never collected) stay in BigQuery and are retried on the next `enrich` run.
- Upserts are idempotent (`INSERT OR UPDATE` on the primary key). Nothing is deleted except by an event teardown script.
- Nothing private goes into `properties`: no `author_ref`, no full article text. `Account` nodes carry only `display` (public name or pseudonym), `is_public_figure` and `followers`.

### Labels and properties (JSON `properties` keys)
| Label | `node_id` | `properties` |
| --- | --- | --- |
| `Event` | `event_id` | `name`, `t0` |
| `Post` | `item_id` | `kind`, `text_excerpt`, `reach`, `lang`, `is_official`, `self_reported`, `conflict_person`, `url` |
| `Account` | `<platform>:acct:<n>` | `display`, `is_public_figure`, `followers` |
| `Article` | `item_id` | `title`, `url`, `outlet_id`, `time_approximate`, `lang`, `self_reported`, `conflict_person` |
| `Outlet` | domain | `name` |
| `Storyline` | `storyline_id` | `name`, `colour_index` |
| `Claim` | `claim_id` | `text`, `source_item_id`, `self_reported`, `confirmed_by` |

| Edge label | Source → destination | `properties` |
| --- | --- | --- |
| `REPLIED_TO`, `QUOTED` | Post → Post | |
| `LINKS_TO` | Post/Article → Post/Article | `explicit: true` |
| `AUTHORED_BY` | Post → Account | |
| `PUBLISHED_BY` | Article → Outlet | |
| `BELONGS_TO` | Post/Article → Storyline | `similarity` |
| `SUPPORTS`, `CONTRADICTS` | Post/Article → Claim | `evidence_span`, `agreement`, `source_item_id` |

`Account` node IDs use the pseudonym number, never the platform's author ID, so the shared graph never holds raw author identifiers.

## Config-defined objects
Storylines and claims come from the event config, so they need stable IDs. *Proposed* shapes for the optional config lists:

```yaml
seed_storylines:
  - {id: chinese-models, name: "Mostly serving Chinese models", seeds: ["<seed sentence>", "..."]}
claims_to_check:
  - {id: subsidised-gpus, text: "Token Factory runs on subsidised government GPUs"}
conflicts_of_interest:
  - {person: "<name>", relationship: "<relationship, with source>"}
turning_points:
  manual:
    - {id: response-1, label: "<label>", at_utc: "<time or null>", confirmed: false, form: "<form or null>"}
```

## Derived values (computed, never stored by the model)
- **Claim evidence status:** `supported` if supporting > 0 and contradicting = 0; `contradicted` if contradicting > 0 and supporting = 0; `mixed` if both > 0; `unverified` if neither (*proposed*). Counts come with it.
- **Storyline facts:** first-seen item, volume by hour, platform of origin, platform-jump times (explicit edges only), top public amplifiers (reach × engagement), stance mix, and volume before and after the response marker.

## Privacy
An account is shown by name only if it is in `official.accounts`, is a named public figure in the config, or has at least `privacy.min_followers_to_name` followers (a new config key, *proposed*; the value is set per event). Engagement alone never makes an account nameable: a small private account at the top of a storyline stays a pseudonym (PRD behavior case #16). Every other account gets a stable pseudonym, `Account A<n>`, numbered in order of first appearance per event. Profiles are fetched only for nameable accounts. The public API never returns `author_ref`.

> This replaces the PRD data-model line "or among the top 20–30 by engagement", which conflicts with the PRD's own edge cases and behavior case #16.
