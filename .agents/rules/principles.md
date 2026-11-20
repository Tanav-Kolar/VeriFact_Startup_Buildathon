---
trigger: always_on
description: VeriFact's non-negotiable product principles as checkable rules. Applies to every data field, API response, prompt and UI string.
---

# Principles (non-negotiable)

Each rule has an ID. Reviewers cite these IDs. If a spec conflicts with a rule, the rule wins: stop and ask.

## P1 Evidence, not verdicts
- P1.1 Every `Claim` node and every `SUPPORTS` / `CONTRADICTS` edge has a non-null `source_item_id` that resolves to a row in `items`.
- P1.2 Every `SUPPORTS` / `CONTRADICTS` edge has an `evidence_span`. The span must be an exact substring of that item's text. Check this in code after the model returns, and drop the edge if it fails.
- P1.3 Claim status is the enum `supported | contradicted | mixed | unverified` and comes with counts (`supporting_count`, `contradicting_count`). No other status values exist.
- P1.4 No field, variable, label or UI string named or meaning `verdict`, `is_true`, `is_false`, `fake`, `truth_score`, `credibility_score` or `risk_score`.
- P1.5 Claim status is never shown in red/green or any true/false colour pair. Colour is never the only way meaning is shown: always add a text label.
- P1.6 Every count shown in the UI or brief links to, or can expand into, the items it counts.

## P2 Self-reported company statements
- P2.1 Claims whose source is an account in the config's `official.accounts` or `official.spokespeople`, or an article quoting one of them, have `self_reported = true`.
- P2.2 `self_reported` becomes false only when `confirmed_by` lists at least one independent `source_item_id`. The model never sets it on its own.
- P2.3 The UI shows the text "Self-reported" on every claim card, quote and brief line where `self_reported = true`.
- P2.4 One claim per assertion. Never merge separate claims (for example "privately procured" and "privately financed") into one.

## P3 Conflicts of interest
- P3.1 Conflicts come only from the config's `conflicts_of_interest`. The model never infers them.
- P3.2 When a quoted person has a configured conflict, the disclosure is rendered in the same component, next to the quote, wherever that quote appears (graph inspector, claim card, brief).

## P4 No bot labels
- P4.1 No identifier or string such as `is_bot`, `bot_score`, `bot_probability`, `sockpuppet`, `troll`, `fake_account`, `inauthentic` or `coordinated_account`, in data, API or UI.
- P4.2 Timing and duplication patterns attach to an item group or storyline, never to an account node.
- P4.3 Signal wording is neutral and factual, for example "14 posts with near-identical text within 6 minutes". It never says or implies intent, automation or coordination.

## P5 Public data, privacy
- P5.1 Collect only through official APIs or public endpoints, within their terms. No scraping of social platforms. Page fetching is only for news articles and the subject's own official pages.
- P5.2 Accounts show as stable pseudonyms ("Account A17") unless they are in `official`, are named public figures in the config, or are in the top public amplifiers defined by the spec. Fetch profiles only for those.
- P5.3 Never build a profile of a private individual: no cross-platform linking of non-public accounts, no per-person history views.
- P5.4 The identity of a Check or extension user never enters the shared graph, BigQuery or logs. That means no user ID, email, IP address, install ID or browsing URL beyond the content being checked.
- P5.5 Full article text stays internal. The public API and UI show only short excerpts with a link to the source.

## P6 No suppression
- P6.1 No endpoint, button or workflow to report, flag, take down, mass-reply to or block content or accounts.
- P6.2 The brief and claim card suggest evidence to look at. They never draft counter-narratives, rebuttal posts or messaging campaigns.

## P7 No invention
- P7.1 Quotes shown to users are exact substrings of a stored item's text. Check this in code.
- P7.2 Timestamps come from the platform's `created_at` or our own `collected_at`, never from model output.
- P7.3 Post IDs, handles and URLs come from API responses or the config, never from model output.
- P7.4 Every number in the UI or brief is computed from stored data at render or build time. If it is missing, show a bracketed placeholder like `[n]`.
- P7.5 Fixtures and test data are marked `synthetic: true`, live under `tests/fixtures/`, and are never loaded by the deployed event.
- P7.6 Unconfirmed facts stay labelled as unconfirmed. If a turning point's time or form is unconfirmed in the config, the UI shows "time unconfirmed" next to its marker.

## P8 Before/after is descriptive
- P8.1 Every before/after view and brief section carries the label "Descriptive comparison, not a causal estimate."
- P8.2 UI and brief copy never says "caused", "led to", "resulted in", "drove", "because of the response" or "impact of the response" (as a measured effect).

## Review checklist (run before reporting done)
```bash
grep -rniE "verdict|is_true|is_false|truth_score|credibility_score|risk_score" --include=*.{py,js,ts,tsx,html,sql} . | grep -v _legacy
grep -rniE "\bbots?\b|is_bot|bot_score|sockpuppet|troll|fake_account|inauthentic" --include=*.{py,js,ts,tsx,html,sql} . | grep -v _legacy
grep -rniE "caused|led to|resulted in" web/ services/api/ 2>/dev/null
grep -rniE "report_to|report-to|takedown|take_down|flag_content|mass_reply" services/api/ web/ extension/ 2>/dev/null
```
Every hit must be justified in your report, or fixed.
