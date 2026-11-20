# VeriFact Gold-Set Evaluation Harness

Evaluates storyline assignment, claim stance classification, 3-paraphrase agreement, inter-annotator agreement, Hinglish subset metrics, and guardrail checks against a hand-labelled gold set (`steering_docs/evaluation.md` §1).

## Usage

```bash
python3 -m eval.run \
  --event events/<event_id>.yaml \
  --gold eval/gold/<event_id>.jsonl \
  --enrichments path/to/enrichments.jsonl \
  --output-dir eval/results
```

Use `--dry-run` to compute and log metrics without writing the result file.

## Gold JSONL Format

One JSON object per line (`eval/gold/<event_id>.jsonl`), storing labels only (no copies of private account data beyond `item_id`):

```json
{
  "item_id": "x:9001",
  "lang": "hi-en",
  "storyline_id": "pricing-claims",
  "claims": [{"claim_id": "claim-throughput", "stance": "contradicts", "span": "exact text"}],
  "annotator_a": "pricing-claims",
  "annotator_b": "pricing-claims",
  "resolved": true
}
```

## Output & `/trust` Integration

Each run writes `<output-dir>/<event_id>_<YYYY-MM-DD>.json` containing:
- `assignment_accuracy` (with `below_target: true` and a plain-word `note` when `< 0.70`)
- `stance_macro_f1` across `supports | contradicts | neutral`
- `mean_agreement` across the 3 prompt paraphrases
- `cohens_kappa` between `annotator_a` and `annotator_b`
- `hinglish` subset metrics (`lang in {"hi-en", "hi", "hi-Latn"}`)
- `guardrails` summary (exact-substring span rate, unknown claim rate, banned-label count)

The API `/events/{event_id}/trust` endpoint reads the latest matching `<event_id>_*.json` from `EVAL_RESULTS_DIR` (default `eval/results`) when no snapshot `gold` override is present, and returns `gold: null` when no evaluation run exists.
