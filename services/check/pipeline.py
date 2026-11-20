"""Check pipeline: input -> claim cards.

The model only proposes (claims, sources, stances). Everything a user sees is decided here:
status and counts are computed from verified evidence (P1.3), evidence spans must be exact
substrings of the fetched page (P1.2, P7.1), URLs come from grounding metadata and the HTTP
response (P7.3), and self-reported / conflict labels come from the event config (P2, P3).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

from services.check.fetch import FetchedPage, FetchError
from verifact_core.config import ClaimToCheck, EventConfig
from verifact_core.url_safety import extract_host, url_hash

MAX_CLAIMS = 2  # cost ceiling: at most 1 + 2 * MAX_CLAIMS model calls per check
MAX_SOURCES = 5
PAGE_TEXT_LIMIT = 20_000
MAX_SPAN_CHARS = 600

PERSON_NOTICE = (
    "This tool checks factual claims, not people or accounts, so it can't assess who someone is "
    "or why they posted. Paste a specific claim to see the evidence for it."
)

Stance = Literal["supports", "contradicts", "neutral"]


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


class GroundedSource(BaseModel):
    model_config = ConfigDict(extra="ignore")
    uri: str
    title: str | None = None


class StanceOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int
    stance: Stance
    span: str


class Gateway(Protocol):
    async def extract(
        self, *, text: str | None, image: tuple[bytes, str] | None,
        known_claims: list[tuple[str, str]],
    ) -> Extraction: ...

    async def ground(self, claim_text: str) -> list[GroundedSource]: ...

    async def stances(self, claim_text: str, pages: list[str]) -> list[StanceOut]: ...


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


# ---------------------------------------------------------------------- pure helpers

_HANDLE = re.compile(r"(?<![\w.@])@\w{2,30}")
_PERSON_CUES = re.compile(
    r"\b(shills?|paid|puppets?|stooges?|plants?|agents?|liars?|scam\w*|fraud\w*|propagand\w*|"
    r"genuine|legit\w*|real person|sellouts?|mouthpieces?)\b",
    re.IGNORECASE,
)


def looks_like_person_assessment(text: str) -> bool:
    """True when the input asks about an account's character rather than a factual claim.

    ponytail: a keyword heuristic that errs towards declining; the extraction call also flags
    person-targeted input (``about_person``), so this only saves a model call on the obvious cases.
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


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reserve(budget: Budget) -> None:
    if not budget.try_reserve(1):
        raise CheckError(
            429, "daily_cap_reached", "Today's Check limit has been reached. Please try again tomorrow."
        )


def verified_evidence(
    stances: list[StanceOut], pages: list[FetchedPage], texts: list[str], event: EventConfig,
    fetched_at: str,
) -> list[dict]:
    """Keep one entry per page, and only when the quoted span appears verbatim in that page."""
    kept: dict[int, dict] = {}
    for s in stances:
        if not (0 <= s.index < len(pages)) or s.index in kept:
            continue
        if not s.span or len(s.span) > MAX_SPAN_CHARS or s.span not in texts[s.index]:
            continue
        platform, source_type, conflict = classify_source(pages[s.index].url, s.span, event)
        kept[s.index] = {
            "item_id": f"web:{url_hash(pages[s.index].url)}",
            "url": pages[s.index].url,
            "platform": platform,
            "source_type": source_type,
            "stance": s.stance,
            "span": s.span,
            "created_at": fetched_at,  # our own collected_at; page publish time is not known
            "conflict": conflict,
        }
    return [kept[i] for i in sorted(kept)]


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


# ------------------------------------------------------------------- orchestration


async def _card_for(
    claim: ExtractedClaim, config_claim: ClaimToCheck | None, *, gateway: Gateway, fetcher: Fetcher,
    event: EventConfig, budget: Budget, fetched_at: str,
) -> dict:
    text = config_claim.text if config_claim else claim.text
    claim_id = config_claim.id if config_claim else "chk-" + hashlib.sha1(text.encode()).hexdigest()[:12]

    _reserve(budget)
    sources = list({s.uri: s for s in await gateway.ground(text)}.values())[:MAX_SOURCES]
    fetched = await asyncio.gather(*(fetcher.fetch(s.uri) for s in sources), return_exceptions=True)
    pages: list[FetchedPage] = []
    for result in fetched:
        if isinstance(result, FetchError):
            continue
        if isinstance(result, BaseException):
            raise result
        if all(url_hash(result.url) != url_hash(p.url) for p in pages):
            pages.append(result)

    evidence: list[dict] = []
    if pages:
        texts = [p.text[:PAGE_TEXT_LIMIT] for p in pages]
        _reserve(budget)
        evidence = verified_evidence(await gateway.stances(text, texts), pages, texts, event, fetched_at)
    return build_card(claim_id, text, evidence, config_claim, event)


async def analyse(
    inp: CheckInput, *, gateway: Gateway, fetcher: Fetcher, event: EventConfig, budget: Budget,
    now: Callable[[], datetime], max_chars: int,
) -> Analysis:
    text = inp.text
    if inp.kind == "url":
        try:
            text = (await fetcher.fetch(inp.url or "")).text[:max_chars]
        except FetchError as exc:
            raise CheckError(422, "unreadable_url", "That page could not be read.") from exc
        if not text.strip():
            raise CheckError(422, "unreadable_url", "That page has no readable text.")

    known = {c.id: c for c in event.claims_to_check}
    _reserve(budget)
    extraction = await gateway.extract(
        text=text, image=inp.image, known_claims=[(c.id, c.text) for c in event.claims_to_check]
    )
    if extraction.about_person:
        return Analysis(notice=PERSON_NOTICE)

    chosen: list[tuple[ExtractedClaim, ClaimToCheck | None]] = []
    for claim in extraction.claims[:MAX_CLAIMS]:
        config_claim = known.get(claim.matched_claim_id or "")
        if config_claim is None:
            if not claim.text.strip() or (text is not None and claim.text not in text):
                continue  # P7.1: a restated claim must be a verbatim part of the input
        if config_claim and any(c is config_claim for _, c in chosen):
            continue
        chosen.append((claim, config_claim))

    fetched_at = _utc_iso(now())
    cards = await asyncio.gather(*(
        _card_for(c, cfg, gateway=gateway, fetcher=fetcher, event=event, budget=budget,
                  fetched_at=fetched_at)
        for c, cfg in chosen
    ))
    return Analysis(cards=list(cards), matched=any(cfg for _, cfg in chosen))
