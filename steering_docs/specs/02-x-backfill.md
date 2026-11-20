# Spec 02: `x-backfill` job

## 1. Goal
Collect, once and under a hard read cap, the X posts around the event's T0 before X recent search loses that window (about 2026-11-23 13:37 UTC for the first event).

## 2. Inputs and outputs
**Inputs**
- The event config (`--event events/<id>.yaml`): `t0`, `window`, `official.accounts`, `official.posts`, `keywords`, `entities.org`, `sources.x` (`enabled`, `max_posts`, `thread_ids`).
- Secret `X_BEARER_TOKEN` (env, from Secret Manager on Cloud Run).
- Spec 01 (`EventConfig`) and spec 03 (`write_raw_items`, cursors, `add_reads`).

**Outputs**
- `services/x_backfill/main.py`, `Dockerfile`, `requirements.txt`, `README.md`, `.env.example`
- Rows in `raw_items` with `source = 'x'`, `captured_by = 'backfill'`, `query_id` = the step name below, `payload` = the X post object as returned.
- Cursor documents per step, and `reads_total` for `(event_id, 'x')`.
- A final structured log line `x_backfill_done` with reads per step, `reads_total`, rows written, rows rejected, and the stop reason (`completed | cap_reached | error`).

**CLI:** `python -m services.x_backfill.main --event events/<id>.yaml [--limit N] [--dry-run] [--step STEP]`

## 3. Steps
1. Load the config. Refuse to start if `sources.x.enabled` is false, `max_posts` is missing, or `X_BEARER_TOKEN` is unset (except with `--dry-run`).
2. Set `budget = min(max_posts, --limit or max_posts) − get_reads(event_id, 'x')`. If `budget ≤ 0`, log `cap_reached` and exit 0.
3. Every request uses `tweet.fields=created_at,lang,author_id,conversation_id,referenced_tweets,entities,public_metrics`, with **no `expansions` and no `user.fields`**. `max_results = min(100, remaining budget)`, and never below the endpoint minimum. If the remaining budget is below that minimum, stop with `cap_reached`.
4. After each response, call `add_reads(event_id, 'x', number of posts returned)`, write the posts with `write_raw_items`, and save the step cursor (`next_token` or newest ID). Stop as soon as the budget is used.
5. Run the steps in this order (PRD, X fetch order), skipping any step with `step_done = true`:
   - **`thread`:** recent search `conversation_id:<id>` for each `official.posts` ID and `sources.x.thread_ids`, plus a post lookup of those IDs themselves.
   - **`quotes`:** quote posts of each `official.posts` ID (`GET /2/tweets/:id/quote_tweets`).
   - **`official`:** recent search `from:<handle>` for each `official.accounts` handle. Use search, not user timelines, so no user lookup (and no $0.010 user read) is needed.
   - **`keywords`:** recent search built from the config: each `keywords.core` term, and each `keywords.context` term combined with an org alias from `entities.org`, minus `keywords.exclude`, with `-is:retweet`. Use `sort_order=relevancy`.
   - Time bounds for every search: `start_time = max(t0 − window.pre_hours, now − 7 days + 10 minutes)`, `end_time = min(t0 + window.post_hours, now − 30 s)`.
6. Mark each step `step_done = true` when its pages are exhausted.
7. Handle HTTP 429 by waiting until `x-rate-limit-reset`. Retry 5xx up to 3 times with backoff. On 401 or 403, stop that step, log it, and continue with the next step.
8. `--dry-run` prints the planned requests (query strings, time bounds, budget) and makes **no** HTTP calls and no writes.
9. Tests use recorded or synthetic fixtures (`tests/fixtures/x_*.json`, marked synthetic) and a mocked HTTP client. No test calls the live X API.

## 4. Acceptance criteria
1. With `--dry-run`, the job makes zero HTTP requests (proven by a test with a client that fails on any call) and prints all four steps with their queries and time bounds.
2. With `--limit 10` against the mock, no more than 10 posts are requested, and `reads_total` rises by exactly the number returned.
3. With `reads_total` already at `max_posts`, the job exits 0 with stop reason `cap_reached` and makes no X requests.
4. Re-running after a completed step skips it (`step_done`) and makes no requests for it.
5. No request contains `expansions` or `user.fields`, and no code calls a user lookup endpoint (checked by test and by grep).
6. Every `raw_items` row written has `source = 'x'`, `captured_by = 'backfill'`, a step `query_id`, and an unmodified payload.
7. Steps run in the order thread → quotes → official → keywords, so stopping at the cap leaves earlier steps complete.
8. Search `start_time` is never earlier than now minus 7 days (test with a frozen clock).
9. A 429 response waits and retries. A 403 on `quotes` logs the error and moves on to `official`.
10. No event-specific literal (handles, post IDs, keywords) appears in `services/x_backfill/`.
11. The final `x_backfill_done` log line is emitted with every field listed in section 2.

## 5. Out of scope
The scheduled `collect-x` job. Author profiles or follower counts. Writing anything except `raw_items` and cursors. Engagement-ranked ordering beyond `sort_order=relevancy`. Full-archive search.

## 6. Open questions
- Is `author_id` billed as a user read? Settle it with the `--limit 10` live run (human task) before the full run. The job's behaviour does not change either way.
- If `quote_tweets` returns 403 on our plan, should `quotes` fall back to a recent search for the post URL? Not blocking: step 7 already moves on.
