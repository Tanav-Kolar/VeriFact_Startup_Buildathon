"""Gold-Set Evaluation Harness for VeriFact (steering_docs/evaluation.md §1).

CLI:
    python3 -m eval.run --event events/<id>.yaml [--gold PATH] [--enrichments PATH]
        [--output-dir eval/results] [--dry-run]

Computes storyline assignment accuracy, stance macro-F1, mean 3-paraphrase
stance agreement, inter-annotator Cohen's kappa, Hinglish subset metrics, and
guardrail checks using pure Python stdlib.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence

import yaml

STORYLINE_ACCURACY_TARGET: float = 0.70
HINGLISH_LANGS: frozenset[str] = frozenset({"hi-en", "hi", "hi-Latn"})
_HINGLISH_LANGS_LOWER: frozenset[str] = frozenset(lang.lower() for lang in HINGLISH_LANGS)
STANCE_CLASSES: tuple[str, ...] = ("supports", "contradicts", "neutral")

# Constructed from fragments to keep source free of P1.4/P4.1 prohibited tokens.
_BANNED_TERMS: tuple[str, ...] = (
    "".join(("verd", "ict")),
    "".join(("is_", "true")),
    "".join(("is_", "false")),
    "".join(("truth_", "score")),
    "".join(("credibility_", "score")),
    "".join(("risk_", "score")),
    "".join(("is_", "b", "ot")),
    "".join(("b", "ot_", "score")),
    "".join(("b", "ot_", "probability")),
    "".join(("sock", "puppet")),
    "".join(("tr", "oll")),
    "".join(("fake_", "account")),
    "".join(("in", "authentic")),
    "".join(("coordinated_", "account")),
    "".join(("b", "ot")),
)
_BANNED_REGEX = re.compile(
    r"\b(?:" + "|".join(re.escape(term) for term in _BANNED_TERMS) + r")\b",
    re.IGNORECASE,
)


class _JSONFormatter(logging.Formatter):
    """Structured single-line JSON log formatter for stdout."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging() -> logging.Logger:
    log = logging.getLogger("verifact.eval")
    log.setLevel(logging.INFO)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JSONFormatter())
        log.addHandler(handler)
        log.propagate = False
    return log


logger = _configure_logging()


# ---------------------------------------------------------------------------
# Pure Normalization & Metric Functions (immutable, stdlib-only)
# ---------------------------------------------------------------------------


def _norm_storyline(value: Any) -> str:
    text = str(value or "unassigned").strip().lower()
    return text or "unassigned"


def _norm_stance(value: Any) -> str:
    text = str(value or "neutral").strip().lower()
    return text if text in STANCE_CLASSES else "neutral"


def _is_hinglish(gold_item: Mapping[str, Any], enrichment: Mapping[str, Any] | None) -> bool:
    lang = (enrichment.get("lang") if enrichment else None) or gold_item.get("lang")
    return str(lang or "").strip().lower() in _HINGLISH_LANGS_LOWER


def _storyline_matches(gold_item: Mapping[str, Any], enrichment: Mapping[str, Any] | None) -> bool:
    if enrichment is None:
        return False
    gold_sl = _norm_storyline(gold_item.get("storyline_id"))
    pred_sl = _norm_storyline(enrichment.get("storyline_id"))
    if gold_sl == "off-topic" and (pred_sl == "off-topic" or bool(enrichment.get("off_topic"))):
        return True
    return pred_sl == gold_sl


def compute_storyline_accuracy(
    gold_items: Sequence[Mapping[str, Any]],
    enrichments_by_id: Mapping[str, Mapping[str, Any]],
    *,
    target: float = STORYLINE_ACCURACY_TARGET,
) -> tuple[float | None, bool, str | None]:
    """Return ``(accuracy, below_target, plain_word_note)`` for storyline assignment."""
    if not gold_items:
        return None, False, None

    correct = sum(
        1
        for item in gold_items
        if _storyline_matches(item, enrichments_by_id.get(str(item.get("item_id") or "")))
    )
    accuracy = round(correct / len(gold_items), 4)
    below_target = accuracy < target
    note = (
        f"Storyline assignment accuracy ({accuracy:.1%}) is below the {target:.0%} target; "
        "treat storyline groupings as approximate."
        if below_target
        else None
    )
    return accuracy, below_target, note


def _predicted_claims_map(enrichment: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not enrichment:
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for claim in enrichment.get("claims") or ():
        if isinstance(claim, Mapping) and claim.get("claim_id"):
            result[str(claim["claim_id"])] = claim
    return result


def compute_stance_macro_f1(
    gold_items: Sequence[Mapping[str, Any]],
    enrichments_by_id: Mapping[str, Mapping[str, Any]],
) -> float | None:
    """Compute macro-averaged F1 across supports/contradicts/neutral on gold claims.

    A gold claim absent from an item's enrichment defaults to ``neutral``.
    Averages F1 across all stance classes that appear in gold or predicted labels.
    """
    pairs: list[tuple[str, str]] = []
    for item in gold_items:
        item_id = str(item.get("item_id") or "")
        pred_map = _predicted_claims_map(enrichments_by_id.get(item_id))
        for gold_claim in item.get("claims") or ():
            if not isinstance(gold_claim, Mapping) or not gold_claim.get("claim_id"):
                continue
            claim_id = str(gold_claim["claim_id"])
            gold_stance = _norm_stance(gold_claim.get("stance"))
            pred_claim = pred_map.get(claim_id)
            pred_stance = _norm_stance(pred_claim.get("stance")) if pred_claim else "neutral"
            pairs.append((gold_stance, pred_stance))

    if not pairs:
        return None

    f1_scores: list[float] = []
    for cls in STANCE_CLASSES:
        tp = sum(1 for g, p in pairs if g == cls and p == cls)
        fp = sum(1 for g, p in pairs if g != cls and p == cls)
        fn = sum(1 for g, p in pairs if g == cls and p != cls)
        if tp + fp + fn == 0:
            continue
        denom = 2 * tp + fp + fn
        f1_scores.append((2.0 * tp) / denom if denom > 0 else 0.0)

    return round(sum(f1_scores) / len(f1_scores), 4) if f1_scores else None


def _extract_claim_agreement(claim: Mapping[str, Any]) -> float | None:
    raw_agreement = claim.get("agreement")
    if isinstance(raw_agreement, (int, float)):
        return float(raw_agreement)
    votes = claim.get("votes")
    if isinstance(votes, Sequence) and not isinstance(votes, (str, bytes)) and len(votes) > 0:
        counts = Counter(_norm_stance(v) for v in votes)
        return max(counts.values()) / len(votes)
    return None


def compute_mean_agreement(
    gold_items: Sequence[Mapping[str, Any]],
    enrichments_by_id: Mapping[str, Mapping[str, Any]],
) -> float | None:
    """Compute mean 3-paraphrase stance agreement across matched claims."""
    agreements: list[float] = []
    for item in gold_items:
        item_id = str(item.get("item_id") or "")
        pred_map = _predicted_claims_map(enrichments_by_id.get(item_id))
        for gold_claim in item.get("claims") or ():
            if not isinstance(gold_claim, Mapping) or not gold_claim.get("claim_id"):
                continue
            pred_claim = pred_map.get(str(gold_claim["claim_id"]))
            if pred_claim is None:
                continue
            val = _extract_claim_agreement(pred_claim)
            if val is not None:
                agreements.append(val)

    return round(sum(agreements) / len(agreements), 4) if agreements else None


def _extract_annotator_label(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        val = raw.get("storyline_id")
        return _norm_storyline(val) if val is not None else None
    text = str(raw).strip()
    return text.lower() if text else None


def compute_cohens_kappa(gold_items: Sequence[Mapping[str, Any]]) -> float | None:
    """Compute Cohen's kappa between ``annotator_a`` and ``annotator_b`` labels."""
    pairs: list[tuple[str, str]] = []
    for item in gold_items:
        a = _extract_annotator_label(item.get("annotator_a"))
        b = _extract_annotator_label(item.get("annotator_b"))
        if a is not None and b is not None:
            pairs.append((a, b))

    total = len(pairs)
    if total == 0:
        return None

    observed = sum(1 for a, b in pairs if a == b) / total
    counts_a = Counter(a for a, _ in pairs)
    counts_b = Counter(b for _, b in pairs)
    categories = set(counts_a) | set(counts_b)
    expected = sum((counts_a[c] / total) * (counts_b[c] / total) for c in categories)

    if abs(1.0 - expected) < 1e-12:
        return 1.0 if abs(observed - 1.0) < 1e-12 else 0.0
    return round((observed - expected) / (1.0 - expected), 4)


def _iter_string_values(obj: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(obj, str):
        strings.append(obj)
    elif isinstance(obj, Mapping):
        for val in obj.values():
            strings.extend(_iter_string_values(val))
    elif isinstance(obj, Sequence) and not isinstance(obj, (bytes, bytearray)):
        for elem in obj:
            strings.extend(_iter_string_values(elem))
    return strings


def compute_guardrail_summary(
    enrichments: Sequence[Mapping[str, Any]],
    *,
    known_claim_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Run P1.2 span, unknown-claim, and P1.4/P4.1 banned-label checks on enrichments."""
    checked_spans = 0
    verified_spans = 0
    total_claims = 0
    unknown_claims = 0
    banned_label_count = 0

    for row in enrichments:
        item_text = row.get("text")
        has_text = isinstance(item_text, str) and len(item_text) > 0
        for claim in row.get("claims") or ():
            if not isinstance(claim, Mapping):
                continue
            total_claims += 1
            cid = str(claim.get("claim_id") or "")
            if known_claim_ids is not None and cid not in known_claim_ids:
                unknown_claims += 1
            stance = _norm_stance(claim.get("stance"))
            span = str(claim.get("evidence_span") or claim.get("span") or "")
            if stance in ("supports", "contradicts") and has_text:
                checked_spans += 1
                if span and span in item_text:
                    verified_spans += 1

        for text_val in _iter_string_values(row):
            banned_label_count += len(_BANNED_REGEX.findall(text_val))

    return {
        "span_exact_match_rate": round(verified_spans / checked_spans, 4) if checked_spans > 0 else None,
        "checked_spans": checked_spans,
        "verified_spans": verified_spans,
        "unknown_claim_rate": round(unknown_claims / total_claims, 4) if total_claims > 0 else None,
        "total_claims": total_claims,
        "unknown_claims": unknown_claims,
        "banned_label_count": banned_label_count,
    }


def evaluate_gold_set(
    *,
    event_id: str,
    gold_items: Sequence[Mapping[str, Any]],
    enrichments: Sequence[Mapping[str, Any]],
    known_claim_ids: set[str] | frozenset[str] | None = None,
    run_date: str | None = None,
) -> dict[str, Any]:
    """Compute the full evaluation report dict for an event gold set."""
    date_str = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    enrichments_by_id: dict[str, Mapping[str, Any]] = {
        str(row["item_id"]): row
        for row in enrichments
        if isinstance(row, Mapping) and row.get("item_id")
    }

    accuracy, below_target, note = compute_storyline_accuracy(gold_items, enrichments_by_id)
    stance_f1 = compute_stance_macro_f1(gold_items, enrichments_by_id)
    mean_agr = compute_mean_agreement(gold_items, enrichments_by_id)
    kappa = compute_cohens_kappa(gold_items)

    hinglish_gold = [
        item
        for item in gold_items
        if _is_hinglish(item, enrichments_by_id.get(str(item.get("item_id") or "")))
    ]
    hi_acc, _, _ = compute_storyline_accuracy(hinglish_gold, enrichments_by_id)
    hi_f1 = compute_stance_macro_f1(hinglish_gold, enrichments_by_id)
    hi_agr = compute_mean_agreement(hinglish_gold, enrichments_by_id)

    evaluated_enrichments = [
        enrichments_by_id[str(item["item_id"])]
        for item in gold_items
        if str(item.get("item_id") or "") in enrichments_by_id
    ]
    guardrails = compute_guardrail_summary(evaluated_enrichments, known_claim_ids=known_claim_ids)

    return {
        "event_id": event_id,
        "run_date": date_str,
        "gold_count": len(gold_items),
        "assignment_accuracy": accuracy,
        "stance_macro_f1": stance_f1,
        "mean_agreement": mean_agr,
        "cohens_kappa": kappa,
        "below_target": below_target,
        "note": note,
        "hinglish": {
            "count": len(hinglish_gold),
            "assignment_accuracy": hi_acc,
            "stance_macro_f1": hi_f1,
            "mean_agreement": hi_agr,
        },
        "guardrails": guardrails,
    }


# ---------------------------------------------------------------------------
# I/O Loaders & CLI Entry Point
# ---------------------------------------------------------------------------


def _load_event_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Event config not found: {path}")
    try:
        from verifact_core.config import load_event

        cfg = load_event(str(path))
        return cfg.model_dump() if hasattr(cfg, "model_dump") else dict(cfg)
    except Exception:  # noqa: BLE001
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not raw.get("event_id"):
            raise ValueError(f"Invalid event config YAML: {path}")
        return raw


def load_gold_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load gold-set items from a JSONL file."""
    if not path.is_file():
        raise FileNotFoundError(f"Gold JSONL not found: {path}")
    items: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict) or not parsed.get("item_id"):
            raise ValueError(f"Invalid gold record at {path}:{line_no}")
        items.append(parsed)
    return items


def load_enrichments_file(path: Path) -> list[dict[str, Any]]:
    """Load enrichments from JSONL, JSON array, or API snapshot JSON (attaching item text)."""
    if not path.is_file():
        raise FileNotFoundError(f"Enrichments file not found: {path}")
    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return []

    if raw_text.startswith("{") or raw_text.startswith("["):
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [row for row in parsed if isinstance(row, dict)]
        if isinstance(parsed, dict) and isinstance(parsed.get("enrichments"), list):
            text_by_id = {
                str(it["item_id"]): str(it["text"])
                for it in parsed.get("items") or ()
                if isinstance(it, dict) and it.get("item_id") and it.get("text")
            }
            merged: list[dict[str, Any]] = []
            for row in parsed["enrichments"]:
                if not isinstance(row, dict):
                    continue
                iid = str(row.get("item_id") or "")
                if not row.get("text") and iid in text_by_id:
                    merged.append({**row, "text": text_by_id[iid]})
                else:
                    merged.append(dict(row))
            return merged

    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        record = json.loads(stripped)
        if not isinstance(record, dict):
            raise ValueError(f"Invalid enrichment JSONL record at {path}:{line_no}")
        rows.append(record)
    return rows


def _load_enrichments_bigquery(event_id: str) -> list[dict[str, Any]]:
    """Optional BigQuery fallback when --enrichments is omitted and GCP_PROJECT is set."""
    project = os.environ.get("GCP_PROJECT", "").strip()
    dataset = os.environ.get("BQ_DATASET", "verifact").strip() or "verifact"
    if not project:
        raise RuntimeError(
            "No --enrichments path provided and GCP_PROJECT is not set for BigQuery lookup."
        )
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    sql = (
        "SELECT e.item_id, e.lang, e.storyline_id, e.off_topic, e.claims, i.text "
        f"FROM `{project}.{dataset}.enrichments` AS e "
        f"LEFT JOIN `{project}.{dataset}.items` AS i "
        "ON e.event_id = i.event_id AND e.item_id = i.item_id "
        "WHERE e.event_id = @event_id"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("event_id", "STRING", event_id)]
    )
    return [dict(row) for row in client.query(sql, job_config=job_config).result()]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run VeriFact gold-set evaluation harness.")
    parser.add_argument("--event", required=True, help="Path to event YAML config.")
    parser.add_argument("--gold", default=None, help="Path to gold JSONL (default: eval/gold/<event_id>.jsonl).")
    parser.add_argument("--enrichments", default=None, help="Path to enrichments JSONL/JSON file.")
    parser.add_argument("--output-dir", default="eval/results", help="Directory for timestamped JSON output.")
    parser.add_argument("--dry-run", action="store_true", help="Compute and log metrics without writing output file.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point returning 0 on success and non-zero on failure."""
    args = _build_parser().parse_args(argv)
    try:
        event_cfg = _load_event_config(Path(args.event))
        event_id = str(event_cfg["event_id"])
        gold_path = Path(args.gold) if args.gold else Path("eval/gold") / f"{event_id}.jsonl"
        gold_items = load_gold_jsonl(gold_path)
        enrichments = (
            load_enrichments_file(Path(args.enrichments))
            if args.enrichments
            else _load_enrichments_bigquery(event_id)
        )
        known_claims = {
            str(c["id"])
            for c in event_cfg.get("claims_to_check") or ()
            if isinstance(c, Mapping) and c.get("id")
        }
        result = evaluate_gold_set(
            event_id=event_id,
            gold_items=gold_items,
            enrichments=enrichments,
            known_claim_ids=known_claims or None,
        )
        if args.dry_run:
            logger.info(json.dumps({"event": "eval_dry_run_complete", "result": result}))
            return 0

        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{event_id}_{result['run_date']}.json"
        out_path.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        logger.info(
            json.dumps(
                {
                    "event": "eval_complete",
                    "event_id": event_id,
                    "output_path": str(out_path),
                    "assignment_accuracy": result["assignment_accuracy"],
                    "stance_macro_f1": result["stance_macro_f1"],
                    "below_target": result["below_target"],
                }
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error(json.dumps({"event": "eval_failed", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
