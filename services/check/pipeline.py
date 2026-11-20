"""Check pipeline: the deterministic parts.

The ADK agents (agents.py) only propose: claims, candidate pages, quotes, a written report. This
module decides what a user sees. Status and counts are computed from verified evidence (P1.3), a
quote counts only if it appears verbatim in the page it names (P1.2, P7.1), page URLs come from
grounding metadata and the HTTP response (P7.3), and self-reported and conflict labels come from
the event config (P2, P3). Nothing here orchestrates a model call.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.check.fetch import FetchedPage
from verifact_core.config import ClaimToCheck, EventConfig
from verifact_core.url_safety import extract_host, url_hash

PAGE_TEXT_LIMIT = 20_000
MAX_SPAN_CHARS = 600
REPORT_EXCERPT_CHARS = 1500

# The legacy report vocabulary, kept at the project owner's request (a deliberate deviation from
# P1.4 and DECISIONS.md #6). Anything the model returns outside these sets is discarded.
REPORT_TAGS = frozenset({"Misleading", "True", "False", "Partially True", "Unverified"})
CREDIBILITY_CATEGORIES = frozenset({"Highly Credible", "Moderate", "Low Credibility", "Unreliable"})
NO_CONCLUSION = "Unverified: there was not enough verifiable evidence to reach a conclusion."
NO_SUMMARY = "A written summary could not be generated for this check."
NO_CLAIMS_SUMMARY = "No specific claims could be checked in this content."
NO_SOURCES_SUMMARY = "No readable sources were found for the claims in this content."

PERSON_NOTICE = (
    "This tool checks factual claims, not people or accounts, so it can't assess who someone is "
    "or why they posted. Paste a specific claim to see the evidence for it."
)

Stance = Literal["supports", "contradicts", "neutral"]


class GeminiError(RuntimeError):
    """The model run failed or returned output we cannot use."""


class CheckError(Exception):
    """A request the service refuses, with the HTTP status and error code to return."""

    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


class ExtractedClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str
    matched_claim_id: str | None = None


class Extraction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    about_person: bool = False
    claims: list[ExtractedClaim] = Field(default_factory=list)


class ReaderEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    claim_index: int
    url: str
    stance: Stance
    quote: str


class ReportConclusion(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int
    conclusion: str


class ReportSource(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int
    credibility_score: int
    category: str
    flags: list[str] = Field(default_factory=list)
    reasoning: str = ""


class ReportOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tag: str = "Unverified"
    overall_summary: str = ""
    conclusions: list[ReportConclusion] = Field(default_factory=list)
    sources: list[ReportSource] = Field(default_factory=list)


class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


class Budget(Protocol):
    def try_reserve(self, n: int = 1) -> bool: ...


@dataclass(frozen=True)
class CheckInput:
    kind: Literal["text", "url", "image"]
    text: str | None = None
    url: str | None = None
    image: tuple[bytes, str] | None = None


@dataclass
class Analysis:
    cards: list[dict] = field(default_factory=list)
    notice: str | None = None
    matched: bool = False
    report: dict | None = None


# ---------------------------------------------------------------------- pure helpers

_HANDLE = re.compile(r"(?<![\w.@])@\w{2,30}")
_PERSON_CUES = re.compile(
    r"\b(shills?|paid|puppets?|stooges?|plants?|agents?|liars?|scam\w*|fraud\w*|propagand\w*|"
    r"genuine|legit\w*|real person|sellouts?|mouthpieces?)\b",
    re.IGNORECASE,
)


def looks_like_person_assessment(text: str) -> bool:
    """True when the input asks about an account's character rather than a factual claim.

    ponytail: a keyword heuristic that errs towards declining; the extractor agent also flags
    person-targeted input (``about_person``), so this only saves a model run on the obvious cases.
    """
    return bool(_HANDLE.search(text) and _PERSON_CUES.search(text))


def evidence_status(supporting: int, contradicting: int) -> str:
    if supporting and contradicting:
        return "mixed"
    if supporting:
        return "supported"
    if contradicting:
        return "contradicted"
    return "unverified"


def utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_json(value: Any) -> Any:
    """Best-effort JSON from model text: plain, fenced in ``` or wrapped in prose. None if unusable."""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except (ValueError, RecursionError):
                continue
    return None


def parse_model(model: type[BaseModel], value: Any) -> Any:
    data = parse_json(value)
    if data is None:
        return None
    try:
        return model.model_validate(data)
    except ValidationError:
        return None


def _host_under(host: str, domains: set[str]) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in domains)


def classify_source(url: str, span: str, event: EventConfig) -> tuple[str, str, dict | None]:
    """(platform, source_type, conflict) from the config only; the model never sets these."""
    official_hosts = {h for h in (extract_host(u) for u in event.official.urls) if h}
    conflict = next(
        (
            {"person": c.person, "relationship": c.relationship}
            for c in event.conflicts_of_interest
            if c.person in span
        ),
        None,
    )
    if _host_under(extract_host(url), official_hosts):
        return "web", "company_statement", conflict
    return "news", ("investor_with_conflict" if conflict else "press"), conflict


def chosen_claims(extraction: Extraction, event: EventConfig, text: str | None
                  ) -> list[tuple[ExtractedClaim, ClaimToCheck | None]]:
    """The claims worth checking, in order and without a cap.

    A claim matched to a config claim must name an id the config has. Any other claim must be a
    verbatim part of the input (P7.1); for an image there is no text to compare, so it passes.
    """
    known = {c.id: c for c in event.claims_to_check}
    chosen: list[tuple[ExtractedClaim, ClaimToCheck | None]] = []
    for claim in extraction.claims:
        config_claim = known.get(claim.matched_claim_id or "")
        if config_claim is None:
            if not claim.text.strip() or (text is not None and claim.text not in text):
                continue
        if config_claim and any(c is config_claim for _, c in chosen):
            continue
        chosen.append((claim, config_claim))
    return chosen


def verified_evidence(entries: list[ReaderEntry], claim_count: int, pages: dict[str, dict],
                      candidates: list[str], event: EventConfig, fetched_at: str) -> list[list[dict]]:
    """Per claim, the evidence whose quote appears verbatim in the page it names, in page order."""
    order = {url: i for i, url in enumerate(candidates)}
    per_claim: list[dict[str, tuple[int, dict]]] = [{} for _ in range(claim_count)]
    for e in entries:
        page = pages.get(e.url)
        if page is None or not (0 <= e.claim_index < claim_count) or e.url in per_claim[e.claim_index]:
            continue
        if not e.quote or len(e.quote) > MAX_SPAN_CHARS or e.quote not in page["text"]:
            continue
        platform, source_type, conflict = classify_source(page["url"], e.quote, event)
        per_claim[e.claim_index][e.url] = (order.get(e.url, len(order)), {
            "item_id": f"web:{url_hash(page['url'])}",
            "url": page["url"],
            "platform": platform,
            "source_type": source_type,
            "stance": e.stance,
            "span": e.quote,
            "created_at": fetched_at,  # our own collected_at; a page's publish time is not known
            "conflict": conflict,
        })
    return [[ev for _, ev in sorted(found.values(), key=lambda t: t[0])] for found in per_claim]


@dataclass
class Collected:
    chosen: list[tuple[ExtractedClaim, ClaimToCheck | None]]
    evidence: list[list[dict]]
    sources: list[FetchedPage]


def collect(state: dict, event: EventConfig, fetched_at: str) -> Collected | None:
    """Everything the agents left in session state, verified. None if extraction is unusable."""
    extraction = parse_model(Extraction, state.get("extracted"))
    if extraction is None:
        return None
    chosen = chosen_claims(extraction, event, state.get("input_text"))
    pages = state.get("pages") or {}
    candidates = list(state.get("candidates") or [])
    raw = parse_json(state.get("reader_output"))
    entries = [e for e in (parse_model(ReaderEntry, x) for x in (raw if isinstance(raw, list) else [])) if e]
    sources: list[FetchedPage] = []
    for url in candidates:
        page = pages.get(url)
        if page and all(url_hash(page["url"]) != url_hash(s.url) for s in sources):
            sources.append(FetchedPage(url=page["url"], text=page["text"]))
    return Collected(chosen, verified_evidence(entries, len(chosen), pages, candidates, event, fetched_at),
                     sources)


def fact_checks_for(state: dict, claim_count: int) -> list[list[dict]]:
    """Fact-check results per claim, straight from the API as the tool recorded them (P7.3)."""
    raw = state.get("fact_checks") or {}
    results: list[list[dict]] = []
    for i in range(claim_count):
        reviews = raw.get(str(i)) or []
        results.append([
            {"source": r["publisher"],
             "summary": f'Rated "{r["rating"]}": {r["title"]}' if r.get("rating") else r.get("title", ""),
             "url": r["url"]}
            for r in reviews if r.get("url")
        ])
    return results


def build_card(claim_id: str, text: str, evidence: list[dict], config_claim: ClaimToCheck | None,
               event: EventConfig) -> dict:
    counts = {
        "supporting": sum(e["stance"] == "supports" for e in evidence),
        "contradicting": sum(e["stance"] == "contradicts" for e in evidence),
        "neutral": sum(e["stance"] == "neutral" for e in evidence),
    }
    company = [e for e in evidence if e["source_type"] == "company_statement"]
    company_stances = {e["stance"] for e in company}
    independent_confirms = any(
        e["stance"] in company_stances for e in evidence if e["source_type"] != "company_statement"
    )
    self_reported = bool(company) and not independent_confirms  # P2.2: only independent items clear it

    storyline_id = deep_link = None
    if config_claim:
        if any(s.id == config_claim.id for s in event.seed_storylines):
            storyline_id = config_claim.id
        params = ([("storyline", storyline_id)] if storyline_id else []) + [("claim", config_claim.id)]
        deep_link = f"/e/{event.event_id}?{urlencode(params)}"

    return {
        "claim_id": claim_id,
        "text": text,
        "status": evidence_status(counts["supporting"], counts["contradicting"]),
        "counts": counts,
        "self_reported": self_reported,
        "self_reported_note": "Company statement, not independently confirmed" if self_reported else None,
        "evidence": evidence,
        "storyline_id": storyline_id,
        "deep_link": deep_link,
    }


def cards_for(collected: Collected, event: EventConfig) -> list[dict]:
    cards = []
    for (claim, config_claim), evidence in zip(collected.chosen, collected.evidence, strict=True):
        text = config_claim.text if config_claim else claim.text
        claim_id = config_claim.id if config_claim else "chk-" + hashlib.sha1(text.encode()).hexdigest()[:12]
        cards.append(build_card(claim_id, text, evidence, config_claim, event))
    return cards


def build_report(cards: list[dict], pages: list[FetchedPage], out: ReportOut | None,
                 fallback_summary: str = NO_SUMMARY,
                 fact_checks: list[list[dict]] | None = None) -> dict:
    """The legacy-shaped report. Evidence lists are the quote-verified evidence, source URLs are the
    pages we fetched, fact-checks come from the API, and every model-written field is validated or
    replaced."""
    out = out or ReportOut()
    conclusions = {
        c.index: c.conclusion.strip()
        for c in out.conclusions
        if 0 <= c.index < len(cards) and c.conclusion.strip()
    }
    analysed = [
        {
            "claim_text": card["text"],
            "supporting_evidence": [{"summary": e["span"], "source": e["url"]}
                                    for e in card["evidence"] if e["stance"] == "supports"],
            "opposing_evidence": [{"summary": e["span"], "source": e["url"]}
                                  for e in card["evidence"] if e["stance"] == "contradicts"],
            "fact_checking_results": (fact_checks[i] if fact_checks and i < len(fact_checks) else []),
            "conclusion": conclusions.get(i, NO_CONCLUSION),
        }
        for i, card in enumerate(cards)
    ]
    kept: dict[int, dict] = {}
    for s in out.sources:
        if not (0 <= s.index < len(pages)) or s.index in kept or s.category not in CREDIBILITY_CATEGORIES:
            continue
        kept[s.index] = {
            "url": pages[s.index].url,
            "credibility_score": max(0, min(100, s.credibility_score)),
            "category": s.category,
            "flags": [f.strip()[:200] for f in s.flags if f.strip()][:5],
            "reasoning": s.reasoning.strip()[:600],
        }
    return {
        "tag": out.tag if out.tag in REPORT_TAGS else "Unverified",
        "overall_summary": out.overall_summary.strip()[:1200] or fallback_summary,
        "analyzed_claims": analysed,
        "source_credibility_summary": [kept[i] for i in sorted(kept)],
    }
