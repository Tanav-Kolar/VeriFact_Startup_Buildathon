# Evaluation

How we know VeriFact works and is honest. Source: `PRD.md` sections 5 and 8. Thresholds marked *proposed* are not yet agreed.

## 1. Gold set (feeds the trust strip)
- 100–200 collected items from the event, about 20 of them Hinglish. Stratify across storylines, platforms and Unassigned.
- Two people label each item independently: storyline (or Unassigned or off-topic), stance toward each matched claim (`supports | contradicts | neutral`), and the evidence span. Record inter-annotator agreement (Cohen's κ) and resolve disagreements by discussion.
- Stored in `eval/gold/<event_id>.jsonl` (*proposed*), one object per item: `{item_id, storyline_id, claims: [{claim_id, stance, span}], annotator_a, annotator_b, resolved}`. Labels only, no copies of private accounts' data beyond the item ID.
- Metrics:
  - Storyline assignment accuracy. Target 70% or more (*proposed*). Below that, the trust strip says so in plain words.
  - Stance macro-F1 over the three classes, reported whatever it is.
  - Stance agreement across the 3 prompt paraphrases, shown next to F1.
  - Same metrics on the Hinglish subset, shown separately.
- Run before every deploy with `python -m eval.run --event events/<id>.yaml` (*proposed*). Results are written to `eval/results/<date>.json`, and `/trust` reads the latest run.

## 2. Behavior contract (regression tests)
The 18 cases in PRD section 4 become tests in `tests/behavior/` (*proposed*). Each case is a synthetic input marked `synthetic: true`. A case passes when the output matches "Good response" and contains nothing from "Reject". All 18 must pass before release (release criterion 16).

## 3. Guardrail checks (any failure blocks release)
| Check | How |
| --- | --- |
| No bot labels, no verdict tags | Review greps in `.agents/rules/principles.md`, plus a scan of rendered UI text and brief output |
| Every brief sentence links to at least one item | Test on brief JSON: each `summary[].item_ids` is non-empty and every ID resolves |
| Every evidence span is verbatim | Test: `span in items.text` for every SUPPORTS/CONTRADICTS edge and claim card |
| Self-reported labels and disclosures | Test: every claim from an official source has `self_reported = true`; every quote by a config-listed person carries `conflict` |
| Privacy | Test: no non-nameable account appears by handle in any API response |
| No future leakage | Test: at random `at_hour` values, nothing returned has `created_at` after that hour |
| Spend | X `reads_total` at most `max_posts`; Gemini spend at most $2/day after the credits end (*proposed*) |
| Frame rate | At least 30 fps on the judging laptop during a full replay (*proposed*) |

## 4. Human review rubric
Seven dimensions, each scored 0–2 (see PRD section 5): Evidence, Status, Labelling, Neutral language, Privacy, Faithfulness (brief), Storyline fit. A 0 on any dimension fails the item. Review 20 random claim cards and the brief before each demo.

## 5. Comprehension test (graduation metric)
6–8 people who don't know the case. Four questions: where the GPU-subsidy storyline started, which platform it reached next, what Sarvam said and whether it is verified, and what the storyline did afterwards. Treatment: 3 minutes in VeriFact. Control: 3 minutes with X search and Google News. Graduate when treatment averages 3 or more of 4 and at least double the control (*proposed*).

## 6. Release criteria
The 17 yes/no criteria in PRD section 8 are the definition of done. Claude Code checks each one before submission.
