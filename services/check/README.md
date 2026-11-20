# check

The Check service (Spec 14). It turns pasted text, a URL or a screenshot into the shared **claim
card** (`steering_docs/api.md`) used by the Check page, the graph inspector and the browser
extension. It reports evidence, never a verdict: status is `supported | contradicted | mixed |
unverified`, always with counts, and is computed in code from verified evidence.

## Endpoints

| Method and path | Body | Returns |
| --- | --- | --- |
| `POST /check` | JSON `{"text": "..."}` or `{"url": "..."}`; or multipart with an `image` file | `{report_id, claims: [claim card], matched_event_id}` |
| `GET /check/{report_id}` | | The stored report |
| `GET /healthz` | | `{"ok": true, "check_enabled": true}` |

Person-targeted input ("is @someone a paid shill?") gets no assessment and nothing is stored:
`{"report_id": null, "claims": [], "matched_event_id": null, "notice": "..."}`.

Errors are `{"error": {"code", "message"}}`: `413 input_too_large`, `422 invalid_input |
unsupported_image | unsupported_url | unreadable_url`, `429 rate_limited | daily_cap_reached`,
`500 internal_error`, `502 upstream_error`, `503 check_disabled | store_unavailable`,
`504 timeout`. Every error, including unexpected ones, is JSON and carries the CORS headers.

## How a check runs

1. **Extract** (Gemini, JSON): up to 2 factual claims copied verbatim from the input (or read from
   the image), matched against the event's `claims_to_check`. An id the config does not have is
   dropped.
2. **Ground** (Gemini + Google Search): only the source URLs are used, taken from grounding
   metadata.
3. **Fetch** each source (public http(s) only, social platforms refused, redirects re-validated,
   size capped).
4. **Stance** (Gemini, JSON): per page, supports / contradicts / neutral plus a quoted span. A span
   that is not an exact substring of the fetched page is dropped (P1.2, P7.1).

`self_reported`, `source_type` and `conflict` come from the event config, never from the model.
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
cached checks), `LOG_LEVEL`, `CHECK_REQUEST_TIMEOUT_S` (default `45`; whole-request deadline, then
`504`), and `TRUSTED_PROXY_HOPS` (default `1`).

`TRUSTED_PROXY_HOPS` is how many proxies append to `X-Forwarded-For` before a request reaches the
service. `1` is right for Cloud Run reached directly. Behind a load balancer or custom domain set
`2`, otherwise every user shares the load balancer's rate-limit bucket. Entries further left are
written by the client and are ignored. IPv6 clients are limited per /64.

A check costs at most `1 + 2 x 2` model calls (extract, then ground and stance per claim), all
counted against the daily cap. Identical input is served from a content-hash cache and spends
nothing.

## Privacy

Reports in Firestore `check_reports/{report_id}` hold the claim cards, `input_type`, `created_at`
and `content_hash`, and no user ID, IP, install ID or input text. Rate limiting keys clients by a
salted hash held in memory only, and logs carry neither IPs nor input.

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
