"""Enrichment Cloud Run Job: language, embedding, storyline, claims, stance (Spec 06)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from google import genai
from google.cloud import bigquery
from google.genai import types as genai_types
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from verifact_core.bq import write_rows
from verifact_core.config import ClaimToCheck, EventConfig, load_event
from verifact_core.cursors import get_cursor, set_cursor

logger = logging.getLogger("services.enrich")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
VALID_STANCES = frozenset({"supports", "contradicts", "neutral"})
UNINFORMATIVE_LANGS = frozenset({"", "und", "unknown", "qme", "qht", "qam", "zxx", "none"})
MAX_API_RETRIES = 4
BASE_RETRY_BACKOFF = 0.5

LANG_NAME_TO_BCP47: dict[str, str] = {
    "english": "en", "eng": "en", "hindi": "hi", "hin": "hi",
    "hi-en": "hi-en", "hinglish": "hi-en", "hi-latn": "hi-en",
    "tamil": "ta", "tam": "ta", "telugu": "te", "tel": "te",
    "kannada": "kn", "kan": "kn", "malayalam": "ml", "mal": "ml",
    "marathi": "mr", "mar": "mr", "bengali": "bn", "ben": "bn",
    "gujarati": "gu", "guj": "gu", "punjabi": "pa", "pan": "pa",
    "urdu": "ur", "urd": "ur", "french": "fr", "fra": "fr", "fre": "fr",
    "german": "de", "deu": "de", "ger": "de", "spanish": "es", "spa": "es",
    "chinese": "zh", "zho": "zh", "chi": "zh", "japanese": "ja", "jpn": "ja",
    "korean": "ko", "kor": "ko",
}


class StartupValidationError(ValueError):
    """Raised when required startup config or env values are missing."""


class DailyCapReachedError(RuntimeError):
    """Raised internally when ENRICH_DAILY_CALL_CAP is reached."""


class EnrichSettings(BaseSettings):
    """Environment settings for the enrich job."""

    model_config = SettingsConfigDict(extra="ignore")

    gcp_project: str = Field(default="", validation_alias="GCP_PROJECT")
    gcp_region: str = Field(default="", validation_alias="GCP_REGION")
    bq_dataset: str = Field(default="verifact", validation_alias="BQ_DATASET")
    gemini_model: str = Field(default="", validation_alias="GEMINI_MODEL")
    embedding_model: str = Field(default="gemini-embedding-001", validation_alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(default=768, gt=0, validation_alias="EMBEDDING_DIM")
    prompt_version: str = Field(default="v1", validation_alias="PROMPT_VERSION")
    enrich_max_items_per_run: int = Field(default=500, gt=0, validation_alias="ENRICH_MAX_ITEMS_PER_RUN")
    enrich_daily_call_cap: int | None = Field(default=None, validation_alias="ENRICH_DAILY_CALL_CAP")
    llm_concurrency: int = Field(default=8, gt=0, validation_alias="LLM_CONCURRENCY")
    embed_concurrency: int = Field(default=8, gt=0, validation_alias="EMBED_CONCURRENCY")

    @field_validator("enrich_daily_call_cap", mode="before")
    @classmethod
    def _empty_cap_as_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class StanceClaimOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    claim_id: str
    stance: Literal["supports", "contradicts", "neutral"]
    evidence_span: str


class StanceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    lang: str = "und"
    claims: list[StanceClaimOutput] = Field(default_factory=list)


@dataclass(frozen=True)
class ItemRecord:
    item_id: str
    event_id: str
    platform: str
    kind: str | None
    url: str | None
    author_ref: str | None
    created_at: datetime | None
    collected_at: datetime | None
    text: str
    lang: str | None
    query_id: str | None = None


@dataclass(frozen=True)
class StorylineAssignment:
    storyline_id: str
    storyline_similarity: float
    second_storyline_id: str | None
    second_similarity: float | None
    off_topic: bool


@dataclass(frozen=True)
class ScoredItem:
    item_id: str
    text: str
    best_storyline_id: str
    best_similarity: float


@dataclass(frozen=True)
class EnrichRunSummary:
    event_id: str
    prompt_version: str
    items_done: int
    calls_made: int
    embed_calls: int
    llm_calls: int
    prompt_tokens: int
    output_tokens: int
    total_tokens: int
    lag_seconds: float
    cap_reached: bool
    dry_run: bool
    report_similarities: bool


def _log_json(event_name: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, json.dumps({"event": event_name, **fields}, default=str))


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return None


def validate_startup(
    config: EventConfig,
    settings: EnrichSettings,
    *,
    report_similarities: bool = False,
) -> None:
    """Validate startup requirements; raise StartupValidationError naming all missing values."""
    errors: list[str] = []
    if not config.seed_storylines:
        errors.append("seed_storylines (no storylines configured)")
    else:
        empty_ids = [
            s.id for s in config.seed_storylines
            if not s.seeds or all(not seed.strip() for seed in s.seeds)
        ]
        if empty_ids:
            errors.append(f"seed_storylines missing seeds: {', '.join(empty_ids)}")

    if not report_similarities:
        if config.enrichment is None or config.enrichment.assign_threshold is None:
            errors.append("enrichment.assign_threshold")
        if config.enrichment is None or config.enrichment.offtopic_floor is None:
            errors.append("enrichment.offtopic_floor")
        if not settings.gemini_model.strip():
            errors.append("GEMINI_MODEL")

    if settings.enrich_daily_call_cap is None or settings.enrich_daily_call_cap < 0:
        errors.append("ENRICH_DAILY_CALL_CAP")

    if errors:
        raise StartupValidationError(
            "Enrich startup check failed; missing or invalid required values: " + "; ".join(errors)
        )


def compute_seed_hash(seeds: Sequence[str], embedding_model_id: str, embedding_dim: int) -> str:
    """Deterministic SHA-256 hash of a storyline's seeds and embedding settings."""
    payload = json.dumps(
        {"seeds": list(seeds), "embedding_model_id": embedding_model_id, "embedding_dim": embedding_dim},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    """Normalise whitespace for deduplicating model calls."""
    return " ".join(text.split())


def l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [0.0 for _ in vector] if norm == 0.0 else [float(v) / norm for v in vector]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Exact cosine similarity between two equal-length vectors."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return 0.0 if norm_a == 0.0 or norm_b == 0.0 else float(dot / (norm_a * norm_b))


def _term_in_text(term: str, text: str) -> bool:
    cleaned = term.strip()
    if not cleaned or not text:
        return False
    prefix = r"(?<!\w)" if cleaned[0].isalnum() else ""
    suffix = r"(?!\w)" if cleaned[-1].isalnum() else ""
    return re.search(prefix + re.escape(cleaned) + suffix, text, flags=re.IGNORECASE) is not None


def matches_exclude_keywords(text: str, exclude_terms: Sequence[str]) -> bool:
    return any(_term_in_text(term, text) for term in exclude_terms)


def assign_storyline(
    text: str,
    embedding: Sequence[float],
    centres: dict[str, list[float]],
    config: EventConfig,
) -> StorylineAssignment:
    """Assign storyline from cosine similarity to centres, thresholds, and keywords.exclude."""
    scored = sorted(
        ((cosine_similarity(embedding, cvec), sid) for sid, cvec in centres.items()),
        key=lambda pair: (-pair[0], pair[1]),
    )
    best_sim, best_id = scored[0]
    second_sim, second_id = scored[1] if len(scored) > 1 else (None, None)

    assert config.enrichment is not None
    assert config.enrichment.assign_threshold is not None
    assert config.enrichment.offtopic_floor is not None

    off_topic = matches_exclude_keywords(text, config.keywords.exclude) or (
        best_sim < config.enrichment.offtopic_floor
    )
    if off_topic:
        storyline_id = "unassigned"
    elif best_sim >= config.enrichment.assign_threshold:
        storyline_id = best_id
    else:
        storyline_id = "unassigned"

    return StorylineAssignment(
        storyline_id=storyline_id,
        storyline_similarity=best_sim,
        second_storyline_id=second_id,
        second_similarity=second_sim,
        off_topic=off_topic,
    )


def _normalize_bcp47(raw_lang: str | None) -> str | None:
    if not raw_lang or not isinstance(raw_lang, str):
        return None
    cleaned = raw_lang.strip().replace("_", "-")
    lowered = cleaned.lower()
    if lowered in UNINFORMATIVE_LANGS:
        return None
    if lowered in LANG_NAME_TO_BCP47:
        return LANG_NAME_TO_BCP47[lowered]
    parts = cleaned.split("-")
    if len(parts) == 1:
        return parts[0].lower()
    tail = [p.upper() if len(p) == 2 else p.title() for p in parts[1:]]
    candidate = "-".join([parts[0].lower(), *tail])
    return LANG_NAME_TO_BCP47.get(candidate.lower(), candidate)


def resolve_language(platform_lang: str | None, run_a_lang: str | None) -> str:
    """Prefer platform language (mapped to BCP-47), else run A's language, else 'und'."""
    return _normalize_bcp47(platform_lang) or _normalize_bcp47(run_a_lang) or "und"


def extract_entities(text: str, config: EventConfig) -> list[str]:
    """Return configured entity keys whose canonical name or alias appears in text."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(key: str) -> None:
        if key not in seen:
            seen.add(key)
            found.append(key)

    for mapping in (config.entities.org, config.entities.product):
        for key, aliases in mapping.items():
            if _term_in_text(key, text) or any(_term_in_text(a, text) for a in aliases):
                _add(key)
    for related in config.entities.related:
        if _term_in_text(related, text):
            _add(related)
    return found


def _extract_source_id(item_id: str) -> str:
    return item_id.split(":", 1)[1] if ":" in item_id else item_id


def check_is_official(
    item: ItemRecord, config: EventConfig, official_x_author_refs: set[str]
) -> bool:
    """Determine is_official strictly via config string rules (never from model)."""
    official_accounts = {a.lstrip("@").strip().lower() for a in config.official.accounts if a.strip()}
    if item.author_ref:
        if item.author_ref.lstrip("@").strip().lower() in official_accounts:
            return True
        if item.platform == "x" and item.author_ref in official_x_author_refs:
            return True
    if item.platform == "x" and _extract_source_id(item.item_id) in set(config.official.posts):
        return True
    if item.query_id == "official":
        return True
    return any(_term_in_text(person, item.text) for person in config.official.spokespeople)


def extract_conflict_person(item: ItemRecord, config: EventConfig) -> str | None:
    """Return configured conflict person named in text or matching author, else None."""
    norm_author = item.author_ref.lstrip("@").strip().lower() if item.author_ref else ""
    for coi in config.conflicts_of_interest:
        person = coi.person.strip()
        if not person:
            continue
        if _term_in_text(person, item.text):
            return coi.person
        if norm_author and norm_author in {person.lower(), person.replace(" ", "").lower()}:
            return coi.person
    return None


def load_prompt_templates(prompt_version: str) -> tuple[str, str, str]:
    """Load the three stance paraphrase prompts for the configured prompt_version."""
    templates: list[str] = []
    for suffix in ("a", "b", "c"):
        path = PROMPTS_DIR / f"stance_{prompt_version}_{suffix}.txt"
        if not path.exists():
            raise FileNotFoundError(f"Prompt template not found: {path}")
        templates.append(path.read_text(encoding="utf-8"))
    return templates[0], templates[1], templates[2]


def render_prompt(
    template: str, claims_to_check: Sequence[ClaimToCheck], item_text: str
) -> str:
    """Safely render prompt template without interpreting braces in untrusted text."""
    claims_lines = (
        "\n".join(f"- {c.id}: {c.text}" for c in claims_to_check) if claims_to_check else "(none)"
    )
    bounded_text = item_text[:4000]
    return template.replace("{claims_list}", claims_lines).replace("{item_text}", bounded_text)


def _extract_run_claims(
    run_output: Any, item_text: str, valid_claim_ids: set[str]
) -> dict[str, tuple[str, str | None]]:
    run_map: dict[str, tuple[str, str | None]] = {}
    raw_claims = run_output.get("claims") if isinstance(run_output, dict) else None
    if not isinstance(raw_claims, list):
        return run_map
    for entry in raw_claims:
        if not isinstance(entry, dict):
            continue
        claim_id, stance, raw_span = entry.get("claim_id"), entry.get("stance"), entry.get("evidence_span")
        if not isinstance(claim_id, str) or claim_id not in valid_claim_ids:
            continue
        if not isinstance(stance, str) or stance not in VALID_STANCES:
            continue
        valid_span = raw_span if isinstance(raw_span, str) and raw_span and raw_span in item_text else None
        if claim_id not in run_map or (run_map[claim_id][1] is None and valid_span is not None):
            run_map[claim_id] = (stance, valid_span)
    return run_map


def verify_and_aggregate_claims(
    item_text: str, run_outputs: Sequence[dict[str, Any]], valid_claim_ids: set[str]
) -> list[dict[str, Any]]:
    """Verify claim_ids and exact evidence_span substrings; apply 2-of-3 majority voting."""
    per_run = [_extract_run_claims(out, item_text, valid_claim_ids) for out in run_outputs]
    ordered_ids = list(dict.fromkeys(cid for rm in per_run for cid in rm))

    verified: list[dict[str, Any]] = []
    for claim_id in ordered_ids:
        votes = [rm[claim_id][0] if claim_id in rm else "absent" for rm in per_run]
        spans = [rm[claim_id][1] if claim_id in rm else None for rm in per_run]
        present = [v for v in votes if v != "absent"]
        if len(present) < 2:
            continue

        counts = Counter(present)
        best_count = max(counts.values())
        winning = next(v for v in present if counts[v] == best_count)
        chosen_span = next(
            (sp for vt, sp in zip(votes, spans, strict=True) if vt == winning and sp is not None),
            None,
        )
        if chosen_span is None:
            continue

        verified.append({
            "claim_id": claim_id,
            "stance": winning,
            "evidence_span": chosen_span,
            "votes": votes,
            "agreement": round(best_count / 3.0, 2),
        })
    return verified


def format_similarity_report(
    event_id: str, scored_items: Sequence[ScoredItem], config: EventConfig
) -> str:
    """Format per-storyline similarity histogram and top/middle/bottom examples."""
    lines = [f"Similarity report for event={event_id} (items={len(scored_items)})", "=" * 72]
    by_storyline: dict[str, list[ScoredItem]] = {s.id: [] for s in config.seed_storylines}
    for item in scored_items:
        by_storyline.setdefault(item.best_storyline_id, []).append(item)

    for storyline in config.seed_storylines:
        group = sorted(by_storyline.get(storyline.id, []), key=lambda s: (-s.best_similarity, s.item_id))
        lines.extend([f"\nStoryline: {storyline.id} — {storyline.name} (n={len(group)})", "Histogram:"])
        buckets = [0] * 10
        for item in group:
            buckets[int(max(0.0, min(0.999999, item.best_similarity)) * 10)] += 1
        for idx, count in enumerate(buckets):
            bracket = "]" if idx == 9 else ")"
            lines.append(f"  [{idx / 10:.1f}, {(idx + 1) / 10:.1f}{bracket}: {count:4d} {'#' * min(count, 40)}")
        if not group:
            lines.append("  (no items matched this storyline as best)")
            continue
        mid = max(0, (len(group) // 2) - 2)
        for label, sample in (("Top", group[:5]), ("Middle", group[mid : mid + 5]), ("Bottom", group[-5:])):
            lines.append(f"  {label} examples:")
            for ex in sample:
                lines.append(f"    {ex.best_similarity:.3f} [{ex.item_id}] {normalize_text(ex.text)[:120]}")
    return "\n".join(lines)


class DailyCallCapTracker:
    """Thread-safe UTC daily Gemini call counter backed by verifact_core.cursors."""

    def __init__(
        self, *, event_id: str, cap: int, utc_day: str, fs_client: Any | None, persist: bool
    ) -> None:
        self.event_id = event_id
        self.cap = cap
        self.utc_day = utc_day
        self._query_id = f"daily_{utc_day}"
        self._fs_client = fs_client
        self._persist = persist
        self._lock = threading.Lock()
        self.calls_this_run = 0
        self.embed_calls = 0
        self.llm_calls = 0
        self.cap_reached = False
        self.used_today = self._load_initial_count()

    def _load_initial_count(self) -> int:
        if not self._persist:
            return 0
        cursor = get_cursor(self.event_id, "gemini", self._query_id, client=self._fs_client)
        if cursor is None or cursor.cursor is None:
            return 0
        try:
            return max(0, int(cursor.cursor))
        except ValueError:
            return 0

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.cap - self.used_today)

    def reserve(self, count: int, *, kind: Literal["embed", "llm"]) -> bool:
        with self._lock:
            if self.used_today + count > self.cap:
                self.cap_reached = True
                return False
            self.used_today += count
            self.calls_this_run += count
            if kind == "embed":
                self.embed_calls += count
            else:
                self.llm_calls += count
            if self._persist:
                set_cursor(self.event_id, "gemini", self._query_id, str(self.used_today), client=self._fs_client)
            return True


def _is_retryable_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in {429, 500, 502, 503, 504}:
        return True
    msg = str(exc).lower()
    return any(tok in msg for tok in ("429", "resource_exhausted", "rate limit", "temporarily unavailable", "503"))


def _call_with_backoff(fn: Callable[[], Any]) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= MAX_API_RETRIES or not _is_retryable_error(exc):
                raise
            time.sleep(BASE_RETRY_BACKOFF * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


class VertexModelRunner:
    """Thin wrapper over google-genai Vertex AI client with retry, cap, and token accounting."""

    def __init__(self, *, client: Any, settings: EnrichSettings, cap_tracker: DailyCallCapTracker) -> None:
        self._client = client
        self._settings = settings
        self._cap = cap_tracker
        self._token_lock = threading.Lock()
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0

    def embed_one(self, text: str) -> list[float]:
        if not self._cap.reserve(1, kind="embed"):
            raise DailyCapReachedError("Daily Gemini call cap reached before embedding")

        def _invoke() -> Any:
            return self._client.models.embed_content(
                model=self._settings.embedding_model,
                contents=text,
                config=genai_types.EmbedContentConfig(
                    task_type="CLUSTERING",
                    output_dimensionality=self._settings.embedding_dim,
                ),
            )

        response = _call_with_backoff(_invoke)
        return [float(v) for v in response.embeddings[0].values]

    def run_stance_paraphrases(self, prompts: tuple[str, str, str]) -> list[dict[str, Any]]:
        if not self._cap.reserve(3, kind="llm"):
            raise DailyCapReachedError("Daily Gemini call cap reached before stance calls")

        def _call_one(prompt_text: str) -> dict[str, Any]:
            def _invoke() -> Any:
                return self._client.models.generate_content(
                    model=self._settings.gemini_model,
                    contents=prompt_text,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.0,
                        response_mime_type="application/json",
                        response_schema=StanceResponse,
                        thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                    ),
                )

            response = _call_with_backoff(_invoke)
            self._record_tokens(response)
            return _parse_stance_response(response)

        with ThreadPoolExecutor(max_workers=min(3, self._settings.llm_concurrency)) as pool:
            return list(pool.map(_call_one, prompts))

    def _record_tokens(self, response: Any) -> None:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        pt = int(getattr(usage, "prompt_token_count", 0) or 0)
        ot = int(getattr(usage, "candidates_token_count", 0) or 0)
        tt = int(getattr(usage, "total_token_count", 0) or (pt + ot))
        with self._token_lock:
            self.prompt_tokens += pt
            self.output_tokens += ot
            self.total_tokens += tt


def _parse_stance_response(response: Any) -> dict[str, Any]:
    raw_text = getattr(response, "text", None)
    if isinstance(raw_text, str) and raw_text.strip():
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"lang": "und", "claims": []}
    parsed_obj = getattr(response, "parsed", None)
    if isinstance(parsed_obj, BaseModel):
        return parsed_obj.model_dump()
    if isinstance(parsed_obj, dict):
        return parsed_obj
    return {"lang": "und", "claims": []}


def _table_ref(bq_client: Any, settings: EnrichSettings, table: str) -> str:
    project = settings.gcp_project or getattr(bq_client, "project", "") or ""
    return f"{project.strip()}.{settings.bq_dataset}.{table}" if isinstance(project, str) and project.strip() else f"{settings.bq_dataset}.{table}"


def _fetch_storyline_centres(
    bq_client: Any, settings: EnrichSettings, event_id: str
) -> dict[tuple[str, str], list[float]]:
    table = _table_ref(bq_client, settings, "storyline_centres")
    sql = (
        f"SELECT storyline_id, seed_hash, embedding, computed_at "
        f"FROM `{table}` WHERE event_id = @event_id ORDER BY computed_at DESC"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("event_id", "STRING", event_id)]
    )
    centres: dict[tuple[str, str], list[float]] = {}
    for row in bq_client.query(sql, job_config=job_config).result():
        r = dict(row)
        key = (str(r["storyline_id"]), str(r["seed_hash"]))
        emb = r.get("embedding") or []
        if key not in centres and len(emb) == settings.embedding_dim:
            centres[key] = [float(v) for v in emb]
    return centres


def _fetch_items(
    bq_client: Any, settings: EnrichSettings, event_id: str, limit: int, *, include_enriched: bool
) -> list[ItemRecord]:
    items_tbl = _table_ref(bq_client, settings, "items")
    enrich_tbl = _table_ref(bq_client, settings, "enrichments")
    if include_enriched:
        sql = (
            "SELECT item_id, event_id, platform, kind, url, author_ref, created_at, collected_at, text, lang "
            f"FROM `{items_tbl}` WHERE event_id = @event_id ORDER BY created_at ASC LIMIT @limit"
        )
        params = [
            bigquery.ScalarQueryParameter("event_id", "STRING", event_id),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    else:
        sql = (
            "SELECT i.item_id, i.event_id, i.platform, i.kind, i.url, i.author_ref, "
            "i.created_at, i.collected_at, i.text, i.lang "
            f"FROM `{items_tbl}` i LEFT JOIN `{enrich_tbl}` e "
            "ON i.event_id = e.event_id AND i.item_id = e.item_id AND e.prompt_version = @prompt_version "
            "WHERE i.event_id = @event_id AND e.item_id IS NULL ORDER BY i.created_at ASC LIMIT @limit"
        )
        params = [
            bigquery.ScalarQueryParameter("event_id", "STRING", event_id),
            bigquery.ScalarQueryParameter("prompt_version", "STRING", settings.prompt_version),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    records: list[ItemRecord] = []
    for row in bq_client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result():
        r = dict(row)
        records.append(ItemRecord(
            item_id=str(r["item_id"]),
            event_id=str(r.get("event_id") or event_id),
            platform=str(r.get("platform") or ""),
            kind=r.get("kind"),
            url=r.get("url"),
            author_ref=str(r["author_ref"]) if r.get("author_ref") is not None else None,
            created_at=_parse_ts(r.get("created_at")),
            collected_at=_parse_ts(r.get("collected_at")),
            text=str(r.get("text") or ""),
            lang=r.get("lang"),
            query_id=r.get("query_id"),
        ))
    return records


def _fetch_official_x_authors(
    bq_client: Any, settings: EnrichSettings, config: EventConfig, items: Sequence[ItemRecord]
) -> set[str]:
    authors: set[str] = set()
    official_posts = set(config.official.posts)
    for item in items:
        if item.platform == "x" and item.author_ref and (
            _extract_source_id(item.item_id) in official_posts or item.query_id == "official"
        ):
            authors.add(item.author_ref)

    raw_tbl = _table_ref(bq_client, settings, "raw_items")
    sql = (
        "SELECT DISTINCT JSON_VALUE(payload, '$.author_id') AS author_ref "
        f"FROM `{raw_tbl}` WHERE event_id = @event_id AND source = 'x' "
        "AND (source_id IN UNNEST(@official_posts) OR query_id = 'official') "
        "AND JSON_VALUE(payload, '$.author_id') IS NOT NULL"
    )
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("event_id", "STRING", config.event_id),
        bigquery.ArrayQueryParameter("official_posts", "STRING", list(config.official.posts)),
    ])
    for row in bq_client.query(sql, job_config=job_config).result():
        val = dict(row).get("author_ref")
        if val:
            authors.add(str(val))
    return authors


def _fetch_account_map(
    bq_client: Any, settings: EnrichSettings, event_id: str
) -> dict[tuple[str, str], int]:
    acct_tbl = _table_ref(bq_client, settings, "account_map")
    sql = f"SELECT platform, author_ref, MIN(account_n) AS account_n FROM `{acct_tbl}` WHERE event_id = @event_id GROUP BY platform, author_ref"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("event_id", "STRING", event_id)]
    )
    return {
        (str(r["platform"]), str(r["author_ref"])): int(r["account_n"])
        for r in (dict(row) for row in bq_client.query(sql, job_config=job_config).result())
    }


def _ensure_storyline_centres(
    config: EventConfig,
    settings: EnrichSettings,
    *,
    bq_client: Any,
    runner: VertexModelRunner,
    now_iso: str,
    write_enabled: bool,
) -> dict[str, list[float]]:
    cached = _fetch_storyline_centres(bq_client, settings, config.event_id)
    centres: dict[str, list[float]] = {}
    new_rows: list[dict[str, Any]] = []

    for storyline in config.seed_storylines:
        seed_hash = compute_seed_hash(storyline.seeds, settings.embedding_model, settings.embedding_dim)
        if (storyline.id, seed_hash) in cached:
            centres[storyline.id] = cached[(storyline.id, seed_hash)]
            continue
        seed_vecs = [runner.embed_one(seed) for seed in storyline.seeds]
        mean_vec = [sum(v[i] for v in seed_vecs) / len(seed_vecs) for i in range(len(seed_vecs[0]))]
        centre_vec = l2_normalize(mean_vec)
        centres[storyline.id] = centre_vec
        new_rows.append({
            "event_id": config.event_id,
            "storyline_id": storyline.id,
            "embedding": centre_vec,
            "seed_hash": seed_hash,
            "embedding_model_id": settings.embedding_model,
            "computed_at": now_iso,
        })

    if new_rows and write_enabled:
        write_rows("storyline_centres", new_rows, client=bq_client)
    return centres


def _compute_lag_seconds(items: Sequence[ItemRecord], now: datetime) -> float:
    collected = [it.collected_at for it in items if it.collected_at is not None]
    return 0.0 if not collected else max(0.0, (now - min(collected)).total_seconds())


def _build_enrichment_row(
    item: ItemRecord,
    config: EventConfig,
    settings: EnrichSettings,
    official_x_authors: set[str],
    now_iso: str,
    *,
    lang: str,
    embedding: list[float],
    assignment: StorylineAssignment | None,
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "event_id": config.event_id,
        "enriched_at": now_iso,
        "model_id": settings.gemini_model,
        "embedding_model_id": settings.embedding_model,
        "prompt_version": settings.prompt_version,
        "lang": lang,
        "embedding": embedding,
        "storyline_id": assignment.storyline_id if assignment else "unassigned",
        "storyline_similarity": assignment.storyline_similarity if assignment else None,
        "second_storyline_id": assignment.second_storyline_id if assignment else None,
        "second_similarity": assignment.second_similarity if assignment else None,
        "off_topic": assignment.off_topic if assignment else False,
        "claims": claims,
        "entities": extract_entities(item.text, config) if item.text.strip() else [],
        "is_official": check_is_official(item, config, official_x_authors),
        "conflict_person": extract_conflict_person(item, config),
    }


def _plan_account_map_rows(
    enriched_items: Sequence[ItemRecord],
    existing_map: dict[tuple[str, str], int],
    event_id: str,
    now_iso: str,
) -> list[dict[str, Any]]:
    updated = dict(existing_map)
    next_n = max(updated.values(), default=0) + 1
    new_rows: list[dict[str, Any]] = []
    for item in enriched_items:
        if not item.author_ref or not item.platform:
            continue
        key = (item.platform, item.author_ref)
        if key in updated:
            continue
        updated[key] = next_n
        new_rows.append({
            "event_id": event_id,
            "platform": item.platform,
            "author_ref": item.author_ref,
            "account_n": next_n,
            "first_seen": _utc_iso(item.created_at) if item.created_at else now_iso,
        })
        next_n += 1
    return new_rows


def _run_similarity_report(
    config: EventConfig,
    settings: EnrichSettings,
    *,
    items: Sequence[ItemRecord],
    centres: dict[str, list[float]],
    runner: VertexModelRunner,
) -> None:
    _, groups = _group_non_empty_items(items)
    embeddings: dict[str, list[float]] = {}
    with ThreadPoolExecutor(max_workers=settings.embed_concurrency) as pool:
        futs = [(norm, group_items, pool.submit(runner.embed_one, group_items[0].text)) for norm, group_items in groups]
        for norm, _, fut in futs:
            embeddings[norm] = fut.result()

    scored_items: list[ScoredItem] = []
    for norm, group_items in groups:
        ranked = sorted(
            ((cosine_similarity(embeddings[norm], cvec), sid) for sid, cvec in centres.items()),
            key=lambda pair: (-pair[0], pair[1]),
        )
        best_sim, best_sid = ranked[0]
        for item in group_items:
            scored_items.append(ScoredItem(item.item_id, item.text, best_sid, best_sim))
    sys.stdout.write(format_similarity_report(config.event_id, scored_items, config) + "\n")


def _default_genai_client(settings: EnrichSettings) -> Any:
    return genai.Client(
        vertexai=True,
        project=settings.gcp_project or None,
        location=settings.gcp_region or None,
    )


def _group_non_empty_items(
    items: Sequence[ItemRecord],
) -> tuple[list[ItemRecord], list[tuple[str, list[ItemRecord]]]]:
    empty_items: list[ItemRecord] = []
    grouped: dict[str, list[ItemRecord]] = {}
    for item in items:
        if not item.text.strip():
            empty_items.append(item)
        else:
            grouped.setdefault(normalize_text(item.text), []).append(item)
    return empty_items, list(grouped.items())


def _embed_wave(
    wave: Sequence[tuple[str, list[ItemRecord]]],
    centres: dict[str, list[float]],
    config: EventConfig,
    runner: VertexModelRunner,
    concurrency: int,
) -> list[tuple[list[ItemRecord], list[float], StorylineAssignment]]:
    if len(wave) == 1:
        _, group_items = wave[0]
        emb = runner.embed_one(group_items[0].text)
        return [(group_items, emb, assign_storyline(group_items[0].text, emb, centres, config))]
    results: list[tuple[list[ItemRecord], list[float], StorylineAssignment]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [(group_items, pool.submit(runner.embed_one, group_items[0].text)) for _, group_items in wave]
        for group_items, fut in futs:
            emb = fut.result()
            results.append((group_items, emb, assign_storyline(group_items[0].text, emb, centres, config)))
    return results


def _process_text_groups(
    text_groups: Sequence[tuple[str, list[ItemRecord]]],
    *,
    config: EventConfig,
    settings: EnrichSettings,
    bq_client: Any,
    runner: VertexModelRunner,
    cap_tracker: DailyCallCapTracker,
    official_x_authors: set[str],
    now_iso: str,
    write_enabled: bool,
) -> tuple[list[dict[str, Any]], list[ItemRecord]]:
    templates = load_prompt_templates(settings.prompt_version)
    valid_claim_ids = {c.id for c in config.claims_to_check}
    rows: list[dict[str, Any]] = []
    done: list[ItemRecord] = []

    try:
        centres = _ensure_storyline_centres(
            config, settings, bq_client=bq_client, runner=runner, now_iso=now_iso, write_enabled=write_enabled
        )
        idx = 0
        stance_rate_limited = False
        while idx < len(text_groups):
            remaining = cap_tracker.remaining()
            if remaining <= 0:
                cap_tracker.cap_reached = True
                break
            wave_size = min(len(text_groups) - idx, max(1, min(settings.embed_concurrency, remaining // 4 or 1)))
            embedded = _embed_wave(text_groups[idx : idx + wave_size], centres, config, runner, settings.embed_concurrency)
            idx += wave_size

            for group_items, emb, assignment in embedded:
                run_outputs: list[dict[str, Any]] = []
                run_a_lang: str | None = None
                if not assignment.off_topic and not stance_rate_limited:
                    prompts = (
                        render_prompt(templates[0], config.claims_to_check, group_items[0].text),
                        render_prompt(templates[1], config.claims_to_check, group_items[0].text),
                        render_prompt(templates[2], config.claims_to_check, group_items[0].text),
                    )
                    try:
                        run_outputs = runner.run_stance_paraphrases(prompts)
                        run_a_lang = run_outputs[0].get("lang") if run_outputs and isinstance(run_outputs[0], dict) else None
                    except Exception as exc:  # noqa: BLE001
                        if not _is_retryable_error(exc):
                            raise
                        stance_rate_limited = True
                        _log_json("enrich_stance_rate_limited_continuing_embeddings", error=str(exc))

                for item in group_items:
                    claims = [] if (assignment.off_topic or not run_outputs) else verify_and_aggregate_claims(item.text, run_outputs, valid_claim_ids)
                    rows.append(_build_enrichment_row(
                        item, config, settings, official_x_authors, now_iso,
                        lang=resolve_language(item.lang, run_a_lang),
                        embedding=emb,
                        assignment=assignment,
                        claims=claims,
                    ))
                    done.append(item)
    except DailyCapReachedError:
        cap_tracker.cap_reached = True
    except Exception as exc:  # noqa: BLE001
        if not rows or not _is_retryable_error(exc):
            raise
        _log_json("enrich_partial_rate_limit", items_saved=len(rows), error=str(exc))
    return rows, done


def _make_summary(
    *,
    config: EventConfig,
    settings: EnrichSettings,
    items_done: int,
    cap_tracker: DailyCallCapTracker | None,
    runner: VertexModelRunner | None,
    lag_seconds: float,
    dry_run: bool,
    report_similarities: bool,
) -> EnrichRunSummary:
    summary = EnrichRunSummary(
        event_id=config.event_id,
        prompt_version=settings.prompt_version,
        items_done=items_done,
        calls_made=cap_tracker.calls_this_run if cap_tracker else 0,
        embed_calls=cap_tracker.embed_calls if cap_tracker else 0,
        llm_calls=cap_tracker.llm_calls if cap_tracker else 0,
        prompt_tokens=runner.prompt_tokens if runner else 0,
        output_tokens=runner.output_tokens if runner else 0,
        total_tokens=runner.total_tokens if runner else 0,
        lag_seconds=lag_seconds,
        cap_reached=cap_tracker.cap_reached if cap_tracker else False,
        dry_run=dry_run,
        report_similarities=report_similarities,
    )
    _log_json("enrich_run", **summary.__dict__)
    return summary


def run_enrich(
    config: EventConfig,
    settings: EnrichSettings,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    report_similarities: bool = False,
    bq_client: Any | None = None,
    fs_client: Any | None = None,
    genai_client: Any | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> EnrichRunSummary:
    """Execute one enrichment run end to end."""
    validate_startup(config, settings, report_similarities=report_similarities)
    now = (now_fn or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    now_iso, utc_day = _utc_iso(now), now.strftime("%Y-%m-%d")

    bq = bq_client if bq_client is not None else bigquery.Client(project=settings.gcp_project or None)
    write_enabled = not dry_run and not report_similarities
    assert settings.enrich_daily_call_cap is not None

    cap_tracker = DailyCallCapTracker(
        event_id=config.event_id, cap=settings.enrich_daily_call_cap,
        utc_day=utc_day, fs_client=fs_client, persist=write_enabled,
    )
    effective_limit = min(settings.enrich_max_items_per_run, limit) if limit is not None else settings.enrich_max_items_per_run
    items = _fetch_items(bq, settings, config.event_id, effective_limit, include_enriched=report_similarities)
    lag_seconds = _compute_lag_seconds(items, now)

    if not items and not report_similarities:
        return _make_summary(
            config=config, settings=settings, items_done=0, cap_tracker=cap_tracker,
            runner=None, lag_seconds=0.0, dry_run=dry_run, report_similarities=False,
        )

    runner = VertexModelRunner(
        client=genai_client if genai_client is not None else _default_genai_client(settings),
        settings=settings,
        cap_tracker=cap_tracker,
    )
    if report_similarities:
        centres = _ensure_storyline_centres(
            config, settings, bq_client=bq, runner=runner, now_iso=now_iso, write_enabled=False
        )
        _run_similarity_report(config, settings, items=items, centres=centres, runner=runner)
        return _make_summary(
            config=config, settings=settings, items_done=0, cap_tracker=cap_tracker,
            runner=runner, lag_seconds=lag_seconds, dry_run=dry_run, report_similarities=True,
        )

    official_x_authors = _fetch_official_x_authors(bq, settings, config, items)
    existing_account_map = _fetch_account_map(bq, settings, config.event_id)
    empty_items, text_groups = _group_non_empty_items(items)

    enrichment_rows = [
        _build_enrichment_row(
            it, config, settings, official_x_authors, now_iso,
            lang="und", embedding=[], assignment=None, claims=[],
        )
        for it in empty_items
    ]
    completed_items: list[ItemRecord] = list(empty_items)

    if text_groups:
        group_rows, group_done = _process_text_groups(
            text_groups, config=config, settings=settings, bq_client=bq, runner=runner,
            cap_tracker=cap_tracker, official_x_authors=official_x_authors,
            now_iso=now_iso, write_enabled=write_enabled,
        )
        enrichment_rows.extend(group_rows)
        completed_items.extend(group_done)

    if cap_tracker.cap_reached:
        _log_json(
            "cap_reached", level=logging.WARNING, event_id=config.event_id,
            utc_day=utc_day, calls_today=cap_tracker.used_today,
            daily_cap=settings.enrich_daily_call_cap,
        )

    if write_enabled:
        new_acct_rows = _plan_account_map_rows(completed_items, existing_account_map, config.event_id, now_iso)
        if new_acct_rows:
            write_rows("account_map", new_acct_rows, client=bq)
        if enrichment_rows:
            write_rows("enrichments", enrichment_rows, client=bq)

    return _make_summary(
        config=config, settings=settings, items_done=len(enrichment_rows), cap_tracker=cap_tracker,
        runner=runner, lag_seconds=lag_seconds, dry_run=dry_run, report_similarities=False,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VeriFact enrich Cloud Run Job")
    parser.add_argument("--event", required=True, help="Path to event YAML config")
    parser.add_argument("--limit", type=int, default=None, help="Maximum items to process")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to BigQuery")
    parser.add_argument(
        "--report-similarities", action="store_true",
        help="Print similarity histogram and examples with zero writes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    args = _build_arg_parser().parse_args(argv)
    try:
        run_enrich(
            load_event(args.event),
            EnrichSettings(),
            limit=args.limit,
            dry_run=args.dry_run,
            report_similarities=args.report_similarities,
        )
    except (StartupValidationError, Exception) as exc:  # noqa: BLE001
        event = "enrich_startup_error" if isinstance(exc, StartupValidationError) else "enrich_error"
        _log_json(event, level=logging.ERROR, error=str(exc))
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
