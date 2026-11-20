# check

The Check service (Spec 14). It turns pasted text, a URL or a screenshot into the shared **claim
card** (`steering_docs/api.md`) used by the Check page, the graph inspector and the browser
extension. It reports evidence, never a verdict: status is `supported | contradicted | mixed |
unverified`, always with counts, and is computed in code from verified evidence.

## Endpoints

| Method and path | Body | Returns |
| --- | --- | --- |
| `POST /check` | JSON `{"text": "..."}` or `{"url": "..."}`; or multipart with an `image` file | `{report_id, claims: [claim card], matched_event_id, report}` |
| `GET /check/{report_id}` | | The stored report |
| `GET /healthz` | | `{"ok": true, "check_enabled": true}` |

Person-targeted input ("is @someone a paid shill?") gets no assessment and nothing is stored:
`{"report_id": null, "claims": [], "matched_event_id": null, "notice": "..."}`.

Errors are `{"error": {"code", "message"}}`: `413 input_too_large`, `422 invalid_input |
unsupported_image | unsupported_url | unreadable_url`, `429 rate_limited | daily_cap_reached`,
`500 internal_error`, `502 upstream_error`, `503 check_disabled | store_unavailable`,
`504 timeout`. Every error, including unexpected ones, is JSON and carries the CORS headers.

## How a check runs (Google ADK)

The pipeline is an ADK agent tree, the same shape as the legacy one. ADK runs the agents and passes
data between them through session state; the service only starts the run and validates the result.

```
SequentialAgent  check_pipeline
  claims_extractor      text or image in, every checkable claim out (no cap on the number)
  ParallelAgent  research
    web_search          Google Search; only its grounding sources are used (URLs from the API)
    fact_checker        Google Fact Check Tools API, through a tool
  evidence_reader       opens the pages the search found with the read_page tool, quotes the passage
  report_generator      tag, summary, conclusions and credibility notes
```

- **Claims** are copied verbatim from the input and matched against the event's `claims_to_check`.
  An id the config does not have is dropped, and a claim that is not a verbatim part of the input
  is dropped (images have no text to compare, so they pass).
- **Evidence** counts only when the quote appears exactly in the page it names (P1.2, P7.1). The
  reader can open only the pages the search found, through the SSRF-guarded fetcher.
- **Fact-checks** are recorded by the tool exactly as the API returned them, so a rating or URL is
  never model output (P7.3). Without `FACT_CHECK_API_KEY` the agent skips itself and makes no model call.
- **Callbacks** count every model call against the daily cap, skip agents with nothing to do (no
  claims, no pages), and add the claims to the model's input as untrusted data. User text, pages and
  images are never placed in a system instruction.
- **The cap:** if it stops any agent before the report step, the check is refused with 429. If it
  only stops the report step, the claim cards are returned with a neutral report.

`self_reported`, `source_type` and `conflict` come from the event config, never from the model.

## The `report` field (legacy shape)

At the project owner's request the response also carries the legacy extension's report:
`{tag, overall_summary, analyzed_claims: [{claim_text, supporting_evidence, opposing_evidence,
fact_checking_results, conclusion}], source_credibility_summary: [{url, credibility_score, category,
flags, reasoning}]}`. It is additive: `claims` is unchanged, so `web/` keeps working.

- Evidence lists are the span-verified evidence; `source` URLs are pages the service fetched.
- One extra model step writes `tag` (one of True, False, Misleading, Partially True, Unverified),
  `overall_summary`, each `conclusion` and the credibility notes. Anything outside those sets, an
  out-of-range index or a score outside 0-100 is discarded or clamped.
- `fact_checking_results` is always empty (the fact-check database lookup is out of scope).
- **This deliberately breaks principles P1.4 and P1.5 and overrules `DECISIONS.md` #6** (no verdict
  tag, no credibility score). It was an explicit decision by the project owner and should be
  recorded in `project_context/DECISIONS.md`.
Matching a config claim fills `claim_id`, `storyline_id` (only when a seed storyline has the same
id), `deep_link` and `matched_event_id`; otherwise they are `null` and the id is `chk-<hash>`.
Evidence `created_at` is our own fetch time, because a page's publish time is not known.

## Configuration

All values come from the environment (see `.env.example`). The four limits have no defaults and
the service refuses to start without them.

| Variable | Target value | Meaning |
| --- | --- | --- |
| `CHECK_RATE_LIMIT_PER_MIN` | `10` | Requests per client per minute |
| `CHECK_DAILY_CALL_CAP` | `500` | Model calls per UTC day, all clients |
| `CHECK_MAX_INPUT_CHARS` | `4000` | Longer text or URL gets `413`; fetched pages are cut to this |
| `CHECK_MAX_IMAGE_BYTES` | `5000000` | Larger screenshots get `413` |
| `CORS_ORIGINS` | `http://127.0.0.1:5173,http://localhost:5173` + the deployed web origin | Browser origins allowed to call `POST /check` |

Also required: `GCP_PROJECT`, `GCP_REGION`, `GEMINI_MODEL` (Flash-Lite model ID), `EVENT_ID`.
Optional: `EVENTS_DIR` (default `events`), `CHECK_ENABLED` (default `true`; `false` serves only
cached checks), `LOG_LEVEL`, `CHECK_REQUEST_TIMEOUT_S` (default `80`; whole-request deadline, then
`504`), `FACT_CHECK_API_KEY` (see below), and `TRUSTED_PROXY_HOPS` (default `1`).

`TRUSTED_PROXY_HOPS` is how many proxies append to `X-Forwarded-For` before a request reaches the
service. `1` is right for Cloud Run reached directly. Behind a load balancer or custom domain set
`2`, otherwise every user shares the load balancer's rate-limit bucket. Entries further left are
written by the client and are ignored. IPv6 clients are limited per /64.

A check makes roughly 6 to 8 model calls (extractor, search, fact-check when configured, the reader's
two turns, report), each counted against the daily cap. They run in five stages, so a check takes
about 45 to 50 seconds with `gemini-3.5-flash-lite` in `asia-south1`. Identical input is served from a content-hash cache and spends
nothing.

## Privacy

Reports in Firestore `check_reports/{report_id}` hold the claim cards, `input_type`, `created_at`
and `content_hash`, and no user ID, IP, install ID or input text. Rate limiting keys clients by a
salted hash held in memory only, and logs carry neither IPs nor input.

## Fact Check Tools API key

Create an API key restricted to the Fact Check Tools API, then store it in Secret Manager as
`fact-check-api-key` (`deploy.sh` mounts it as `FACT_CHECK_API_KEY`). Locally, set
`FACT_CHECK_API_KEY` in the environment. The key is sent in an `X-Goog-Api-Key` header, never in a URL.

## Deploy to Cloud Run

```bash
# from the repo root, with the settings in the environment
services/check/deploy.sh            # prints every command and changes nothing
services/check/deploy.sh --apply    # runs them
```

It enables the APIs, creates the `verifact-check` service account (Vertex AI user, Firestore user,
Secret Manager accessor), builds the image with Cloud Build, and deploys `check` with
`min-instances=0`, `max-instances=2`, 1 GiB, and public access (the extension and web app call it;
the per-client rate limit and the daily call cap bound the cost). Set `CORS_ORIGINS` to the deployed
web origin. The rate limit and daily cap are per instance, so the worst case is the cap times
`MAX_INSTANCES`.

Builds run as a dedicated `verifact-build` service account with only the rights a build needs (read
the source archive, write to the repository, write logs), because new projects no longer give the
default Compute account those rights.

`GET /healthz` works locally and inside the container, but on a `*.run.app` URL Google's front end
answers that path itself with an HTML 404. To test a deployed service use another path, for example
send `{}` to `POST /check` and expect a `422`. If you run the script through a pipe, add
`set -o pipefail`, or a failure will be hidden behind the last command's exit code.

## Run and test

```bash
uvicorn services.check.main:create_app --factory --port 8081   # env from .env.example
pytest tests/test_check.py
```

Docker builds from the repo root: `docker build -f services/check/Dockerfile .`. Deploy with
`min-instances=0` and a small `max-instances`.

## Known limits

- The rate limiter, the daily cap and the cache live in one instance, so the real ceilings are
  multiplied by `max-instances`. Keep it small, or move the counters to Firestore.
- Retries (up to 3 per step) are not counted against the daily cap.
- Page fetching resolves a host once and connects to that validated address, refuses compressed
  responses, caps size and redirects, and has a 15 s deadline per page.
- The request body is capped while it streams, so chunked uploads are bounded too.
- No BigQuery access: counts come from web sources found at check time, not from the collected
  graph. Matching a screenshot against collected official posts is not done in v1.
