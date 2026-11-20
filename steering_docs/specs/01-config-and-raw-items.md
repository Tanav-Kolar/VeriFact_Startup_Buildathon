# Spec 01: Event config, `raw_items` table and `items` view

## 1. Goal
Create the event config loader, the first event's config file, the BigQuery `raw_items` table and an `items` view that turns X payloads into the common item shape.

## 2. Inputs and outputs
**Inputs**
- `project_context/SEEDS.md`: config schema (Part 1) and Sarvam values (Part 2).
- `steering_docs/data-model.md`: `raw_items`, `items`, config-defined objects, privacy.

**Outputs**
- `events/sarvam-token-factory.yaml`
- `verifact_core/__init__.py`, `verifact_core/config.py` (Pydantic v2 model `EventConfig`, function `load_event(path: str) -> EventConfig`)
- `infra/bigquery/raw_items.json` (table schema), `infra/bigquery/items_view.sql`, `infra/bigquery/create.sh`
- `tests/test_config.py`, `tests/test_items_view.py`, `tests/fixtures/x_payload_*.json` (synthetic, marked `"synthetic": true`)

## 3. Steps
1. Write `events/sarvam-token-factory.yaml` using SEEDS Part 1 keys and Part 2 values:
   - `sources`: X `max_posts: 1500`, Reddit and `manual` disabled, HN, YouTube and news enabled.
   - `seed_storylines`: the 9 seed storylines from SEEDS Part 2, each as `{id, name, seeds: []}` with a kebab-case `id`. Leave `seeds` empty; humans fill them in.
   - `claims_to_check`: the 8 rows of SEEDS' claims table, as `{id, text}`.
   - `conflicts_of_interest`: the one entry in SEEDS, as `{person, relationship}`. Copy the relationship text with its source exactly as written ("per MediaNama").
   - `turning_points.manual`: one entry `{id: response-1, label: "Sarvam: GPUs privately procured / financed", at_utc: null, confirmed: false, form: null}`.
   - Add `privacy: {min_followers_to_name: null}`. Null means only official accounts and config-named people are named.
   - Do not add any value that is not in SEEDS.md.
2. Write `EventConfig` covering every key in SEEDS Part 1, plus the shapes in data-model.md (Config-defined objects) and `privacy`. Required: `event_id`, `name`, `t0.utc`, `window`, `official`, `keywords.core`, `sources`. Unknown keys raise an error.
3. `load_event` validates: `t0.utc` parses as UTC; `event_id` is a slug matching the filename; IDs are unique within `seed_storylines` and `claims_to_check`; every enabled source block is present.
4. Write the `raw_items` schema exactly as in data-model.md, partitioned by `DATE(collected_at)` and clustered by `event_id, source`.
5. Write `items_view.sql` as a `UNION ALL` of one CTE per source. Implement the `x` CTE now; later specs add the others. For `x`, from `payload` (an X API v2 post object):
   - `item_id = 'x:' || id`, `platform = 'x'`.
   - `kind = 'reply'` if `referenced_tweets` has `replied_to`, `'quote'` if it has `quoted`, else `'post'`.
   - `parent_id` and `quote_of` from `referenced_tweets`, each with an `x:` prefix.
   - `url = 'https://x.com/i/status/' || id`. `author_ref = author_id`. `author_followers = NULL`.
   - `text`, `lang`, `created_at` from the payload. `links` from `entities.urls[].expanded_url`.
   - `engagement` from `public_metrics`: likes = like_count, reposts = retweet_count + quote_count, replies = reply_count, views = impression_count, score = NULL.
   - `hours_from_t0`: computed against the event's T0. The view joins a small `event_t0` table (`event_id, t0`) loaded by `create.sh` from the config.
   - Dedupe on (`source`, `source_id`), keeping the row with the earliest `collected_at`.
6. `create.sh` creates the dataset (if missing), `raw_items`, `event_t0` and the view. It reads `GCP_PROJECT`, `GCP_REGION` and `BQ_DATASET` from env and is safe to re-run.
7. Tests: config load, a validation failure for each rule in step 3, and the view's X mapping against the fixtures, using the BigQuery emulator or a SQL check documented in the test.

## 4. Acceptance criteria
1. `load_event("events/sarvam-token-factory.yaml")` returns an `EventConfig` with `t0.utc == 2026-11-16T13:37:16Z` and `sources.x.max_posts == 1500`.
2. The YAML contains no value absent from SEEDS.md, except `privacy.min_followers_to_name: null`, the turning-point entry in step 1, and generated `id` slugs.
3. A config with an unknown key, a duplicate claim ID, a missing `t0.utc`, or an `event_id` that does not match the filename fails with a clear error naming the problem.
4. `raw_items` columns, types, partitioning and clustering match data-model.md exactly.
5. Given fixtures for a plain post, a reply and a quote, the `items` view returns the correct `kind`, `parent_id`, `quote_of`, `links`, `engagement` and `hours_from_t0`.
6. Two `raw_items` rows with the same `source_id` produce one `items` row, carrying the earlier `collected_at`.
7. `create.sh` runs twice without error and without data loss.
8. `grep -rni sarvam verifact_core infra` returns nothing.
9. No code, script or test issues an UPDATE or DELETE on `raw_items`.

## 5. Out of scope
Item mappings for HN, YouTube, GDELT and Firecrawl (specs 04–05). Writing rows (spec 03). Enrichment. Filling `seeds` sentences.

## 6. Open questions
- None blocking. `privacy.min_followers_to_name` stays null until we agree a value.
