# Spec 04: Collectors for Hacker News, YouTube and GDELT

## 1. Goal
Collect new public items about the event from Hacker News, YouTube and GDELT on a schedule, append them to `raw_items`, and map them into the `items` view.

## 2. Inputs and outputs
**Inputs**
- Event config: `t0`, `window`, `keywords` (`core`, `context`, `exclude`), `entities.org`, `sources.hn`, `sources.youtube` (`max_videos`), `sources.news` (`gdelt`).
- Secret `YOUTUBE_API_KEY`. HN Algolia and GDELT need no key.
- Spec 01 (`EventConfig`, `items` view) and spec 03 (`write_raw_items`, cursors).

**Outputs**
- `services/collect_hn/`, `services/collect_youtube/`, `services/collect_gdelt/`, each with `main.py`, `Dockerfile`, `requirements.txt`, `README.md`, `.env.example`.
- `verifact_core/queries.py`: `build_queries(cfg, platform) -> list[Query]`, where `Query = {query_id, text}`, shared by all collectors (and reusable by spec 02).
- New CTEs `hn`, `youtube` and `gdelt` in `infra/bigquery/items_view.sql`.
- `infra/deploy/collectors.sh`: deploys the three Cloud Run Jobs and their Cloud Scheduler triggers.
- Tests and synthetic fixtures for each source.

**CLI (each job):** `python -m services.collect_<source>.main --event events/<id>.yaml [--dry-run] [--since ISO8601]`

## 3. Steps
1. **Queries.** `build_queries` returns one query per `keywords.core` term, plus one per `keywords.context` term combined with each org alias from `entities.org` (as an AND, in the platform's syntax). It drops queries matching `keywords.exclude`. `query_id` is a stable slug of the query text.
2. **Time bounds**, for every source: from the step cursor, or `t0 − window.pre_hours` on the first run, up to `min(now, t0 + window.post_hours)`. Stop collecting entirely after `t0 + window.post_hours` unless `window.live` is true.
3. **`collect-hn`** (HN Algolia):
   - `GET https://hn.algolia.com/api/v1/search_by_date?query=<q>&tags=(story,comment)&numericFilters=created_at_i>=<from>,created_at_i<<to>&hitsPerPage=100`, paging until no hits.
   - For each new story, fetch `GET /api/v1/items/<id>` once and store each comment in the tree as its own `raw_items` row (`query_id = 'thread:<story_id>'`).
   - `source = 'hn'`, `source_id` = the Algolia `objectID` or item `id`.
4. **`collect-youtube`** (Data API v3):
   - `search.list` (`type=video`, `order=date`, `publishedAfter`, `publishedBefore`, `maxResults=50`). Search costs 100 quota units, so the job runs at most `YOUTUBE_SEARCHES_PER_RUN` searches (env, default 4).
   - Stop adding new videos once the event has `sources.youtube.max_videos` videos stored.
   - `videos.list` (`part=snippet,statistics`) for new video IDs, in batches of 50.
   - `channels.list` (`part=statistics`) for those videos' channels only, to get `subscriberCount` (public, needed for the naming rule).
   - `commentThreads.list` (`part=snippet,replies`, `maxResults=100`) per stored video, paging from the cursor. A 403 `commentsDisabled` marks the video's comments unavailable and is not an error.
   - Store videos, channel statistics and comments as separate rows (`query_id`: `video`, `channel`, `comments:<video_id>`). Never fetch comment authors' channels.
   - Log quota units used per run.
5. **`collect-gdelt`** (DOC 2.0):
   - `GET https://api.gdeltproject.org/api/v2/doc/doc?query=<q>&mode=ArtList&format=json&maxrecords=250&sort=DateAsc&startdatetime=<YYYYMMDDHHMMSS>&enddatetime=<YYYYMMDDHHMMSS>`.
   - At most one request every 5 seconds (shared limiter).
   - If a response has 250 articles, split the time window in half and re-query each half, down to 15-minute windows; log `gdelt_window_saturated` if it is still full.
   - `source = 'gdelt'`, `source_id` = sha1 of the normalised URL (lower-case host, no tracking parameters, no fragment).
6. **Idempotency.** Each job saves its cursor per `(event_id, source, query_id)` after each successful page. A re-run with no new data writes zero rows.
7. **`items` view CTEs:**
   - **`hn`:** `item_id = 'hn:' || id`. `kind` is `story` or `comment`. `parent_id = 'hn:' || parent_id`. `text` = title + text (stories) or comment text with HTML stripped. `url = 'https://news.ycombinator.com/item?id=' || id`. `author_ref = author`. `engagement.score = points`, `replies = num_comments`. `links` = the story URL.
   - **`youtube`:** videos `item_id = 'yt:' || id`, `kind = 'video'`, `text` = title + description. Comments `item_id = 'yt:c:' || id`, `kind = 'comment'`, `parent_id` = the video, or the parent comment for replies. `author_ref` = channel ID. `author_followers` = subscriberCount for video channels, NULL for commenters. `engagement` from statistics (views, likes, comment count). Channel-statistics rows produce no item.
   - **`gdelt`:** `item_id = 'gdelt:' || source_id`, `platform = 'news'`, `kind = 'article'`. `url`, title as `text` until spec 05 adds the body. `created_at` from `seendate`, with `time_approximate = true`. Outlet = `domain`. `lang` from `language`.
8. **Deploy.** `collectors.sh` creates or updates the three jobs (`min-instances` does not apply to jobs; set task timeout and max retries 1) and Scheduler triggers. Cadences come from env, with defaults `HN_EVERY=10m`, `YOUTUBE_EVERY=60m`, `GDELT_EVERY=15m`. Triggers are created paused when `LIVE_INGESTION=false`.

## 4. Acceptance criteria
1. `build_queries` on the Sarvam config returns only queries made from config values, with stable `query_id`s, and none containing an `exclude` term.
2. Each job with `--dry-run` makes no HTTP calls or writes, and prints its queries and time bounds.
3. Against mocked APIs, a first run writes the expected rows, and an immediate second run writes zero new rows.
4. HN: a story with 3 nested comments yields 4 `items` rows with correct `parent_id` chains.
5. YouTube: a run never exceeds `YOUTUBE_SEARCHES_PER_RUN` `search.list` calls, never stores more than `max_videos` videos, and never calls `channels.list` for a commenter.
6. YouTube: a `commentsDisabled` 403 leaves the video stored and the job successful.
7. GDELT: a mocked response of 250 articles triggers a window split, and requests are at least 5 s apart (test with a fake clock).
8. GDELT `items` rows have `platform = 'news'`, `time_approximate = true` and a URL-hash `source_id`; the same URL with different tracking parameters dedupes to one item.
9. No item has `created_at` outside `[t0 − pre_hours, t0 + post_hours]`.
10. `grep -rni sarvam services/collect_*` returns nothing.
11. No test calls a live API.

## 5. Out of scope
Article body text (spec 05). X (specs 02 and later `collect-x`). Reddit and LinkedIn (disabled). Enrichment.

## 6. Open questions
- None blocking. Cadences are the PRD's proposed values and can be changed in env.
