# `enrich` job

Turns unenriched BigQuery `items` rows into `enrichments` rows for an event: language, embedding, storyline assignment, verified claim stances across three prompt paraphrases, rule-based entities, official and conflict flags, and stable `account_map` pseudonyms.

## CLI usage

```bash
python -m services.enrich.main --event events/<event_id>.yaml [--limit N] [--dry-run] [--report-similarities]
```

### Flags
- `--event`: Path to an event YAML config file (required).
- `--limit N`: Cap items processed in this run (combined with `ENRICH_MAX_ITEMS_PER_RUN`).
- `--dry-run`: Run without writing to BigQuery or updating tables.
- `--report-similarities`: Print a per-storyline cosine similarity histogram and example texts near the top, middle, and bottom, with zero writes. Use this to choose `enrichment.assign_threshold` and `enrichment.offtopic_floor`.

## Environment variables

See `.env.example`. Required at startup:
- `ENRICH_DAILY_CALL_CAP`: Hard daily Vertex AI model call cap per UTC day, tracked in Firestore `collector_cursors` (`source = 'gemini'`).
- `GEMINI_MODEL`: Vertex AI Gemini Flash-Lite model ID (required for normal enrichment runs).
- `GCP_PROJECT`, `GCP_REGION`: GCP project and single region for Vertex AI and BigQuery.

## Verification

- All `claim_id` values must exist in the event config's `claims_to_check`.
- Every `evidence_span` is checked in code to be an exact substring of the item text.
- Claims require at least 2 of 3 prompt paraphrases; majority stance and `agreement` are stored with `votes`.
- `entities`, `is_official`, and `conflict_person` are computed purely from configured string rules, never from model output.
