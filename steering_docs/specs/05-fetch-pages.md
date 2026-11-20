# Spec 05: `fetch-pages`: article text and dated official snapshots

## 1. Goal
Fetch the main text of each news article GDELT found, and take dated snapshots of the subject's official pages, within a hard Firecrawl credit cap.

## 2. Inputs and outputs
**Inputs**
- `items` rows with `platform = 'news'` and no Firecrawl text yet.
- Config `official.urls`.
- Secret `FIRECRAWL_API_KEY`. Env `FIRECRAWL_MAX_PER_RUN` (default 50) and `FIRECRAWL_MAX_TOTAL` (required; the monthly credit budget we allow this project to use).
- Spec 03 (`write_raw_items`, cursors, `add_reads` reused as a credit counter with `source = 'firecrawl'`).

**Outputs**
- `services/fetch_pages/` (`main.py`, `Dockerfile`, `requirements.txt`, `README.md`, `.env.example`).
- `raw_items` rows with `source = 'firecrawl'`:
  - articles: `query_id = 'article'`, `source_id` = the same URL hash as the GDELT row.
  - official snapshots: `query_id = 'official'`, `source_id` = `<url hash>:<YYYY-MM-DD>`.
- `items_view.sql`: article rows get `text` from the latest Firecrawl article row with the same `source_id`. A new `web` CTE covers official snapshots.
- A scheduler entry in `infra/deploy/collectors.sh` (`FETCH_PAGES_EVERY`, default 15m; official snapshots once per UTC day).

**CLI:** `python -m services.fetch_pages.main --event events/<id>.yaml [--mode articles|official|both] [--dry-run]`

## 3. Steps
1. Refuse to start without `FIRECRAWL_MAX_TOTAL`. Budget = `min(FIRECRAWL_MAX_PER_RUN, FIRECRAWL_MAX_TOTAL − get_reads(event_id, 'firecrawl'))`. At 0, exit 0 with `cap_reached`.
2. **Never scrape social platforms (P5.1, PRD IR-8).** Skip any URL whose host is, or is under, `x.com`, `twitter.com`, `reddit.com`, `linkedin.com`, `youtube.com`, `youtu.be`, `facebook.com`, `instagram.com`, `tiktok.com`, `t.me`, `news.ycombinator.com`, and log `skipped_social`. This blocklist is a constant in `verifact_core`, not event config.
3. **Articles:** select up to the budget of `news` items with no Firecrawl row, oldest `created_at` first. For each: `POST https://api.firecrawl.dev/v2/scrape` with `{"url": ..., "formats": ["markdown"], "onlyMainContent": true}`.
4. **Official snapshots (`--mode official`, once per UTC day):** scrape each `official.urls` entry with the same request. Skip a URL already snapshotted today.
5. After each call (success or failure that Firecrawl bills), `add_reads(event_id, 'firecrawl', 1)`. Write the full response as the payload.
6. A failed fetch stores nothing for that URL, logs `fetch_failed` with the status, and is retried on later runs at most 3 times in total (count kept in the cursor). The article keeps its GDELT metadata, and the claim card shows headline and link only (PRD edge case).
7. **`items` view:**
   - Article `text` = title + Firecrawl markdown.
   - If the Firecrawl metadata contains a publish time (check the actual response, for example an `article:published_time` value), use it as `created_at` and set `time_approximate = false`. Otherwise keep GDELT's `seendate`.
   - `web` CTE: `item_id = 'web:' || source_id`, `platform = 'web'`, `kind = 'official_page'`, `created_at = collected_at` (time of the snapshot), `url`, `text` = markdown.
8. Full article text is stored in `raw_items` only. Nothing in this spec exposes it publicly (P5.5).

## 4. Acceptance criteria
1. With `get_reads` at `FIRECRAWL_MAX_TOTAL`, the job makes no Firecrawl calls and exits 0 with `cap_reached`.
2. A run never makes more than `FIRECRAWL_MAX_PER_RUN` calls.
3. A URL on any blocklisted host is never sent to Firecrawl (test covers `x.com`, a subdomain such as `mobile.twitter.com`, and `youtu.be`).
4. An article with a Firecrawl row shows its markdown in `items.text`; one without keeps the GDELT title.
5. Running official mode twice on the same UTC day makes one snapshot per URL; on the next day it makes a new one with a new `source_id`.
6. A URL that has failed 3 times is not retried.
7. Every request body is exactly `url`, `formats: ["markdown"]`, `onlyMainContent: true`.
8. No test calls the live Firecrawl API.
9. `grep -rni sarvam services/fetch_pages` returns nothing.

## 5. Out of scope
Crawling (`/crawl`, `/map`), LLM extraction formats, screenshots, PDF parsing. Enrichment of the text.

## 6. Open questions
- What value should `FIRECRAWL_MAX_TOTAL` have? The free tier is 1,000 credits a month (PRD). Recommend 800, leaving headroom. Set it in env before deploy; the job refuses to start without it.
