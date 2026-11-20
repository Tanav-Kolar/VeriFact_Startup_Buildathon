"""Spanner Graph mutation builder, idempotent batch upsert, and GQL query module (Spec 07)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from google.cloud import spanner
from google.cloud.spanner_v1 import JsonObject, param_types
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from verifact_core.config import EventConfig

logger = logging.getLogger(__name__)

GRAPH_NODE_COLUMNS: tuple[str, ...] = (
    "event_id",
    "node_id",
    "label",
    "valid_from",
    "platform",
    "storyline_id",
    "properties",
    "updated_at",
)

GRAPH_EDGE_COLUMNS: tuple[str, ...] = (
    "event_id",
    "node_id",
    "edge_label",
    "dest_id",
    "valid_from",
    "properties",
    "updated_at",
)

MAX_EXCERPT_LEN = 300
DEFAULT_BATCH_SIZE = 500
ARTICLE_KINDS = frozenset({"article", "official_page"})
ARTICLE_PLATFORMS = frozenset({"news", "web"})

_X_STATUS_RE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/(\d+)", re.IGNORECASE)
_HN_ITEM_RE = re.compile(r"^https?://news\.ycombinator\.com/item\?id=(\d+)", re.IGNORECASE)


class SpannerSettings(BaseSettings):
    """Environment configuration for Cloud Spanner Graph access."""

    model_config = SettingsConfigDict(extra="ignore")

    gcp_project: str = Field(default="", validation_alias="GCP_PROJECT")
    spanner_instance: str = Field(default="", validation_alias="SPANNER_INSTANCE")
    spanner_database: str = Field(default="verifact", validation_alias="SPANNER_DATABASE")


class GraphNodeRow(BaseModel):
    """Immutable representation of a GraphNode row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    node_id: str
    label: str
    valid_from: datetime
    platform: str | None = None
    storyline_id: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class GraphEdgeRow(BaseModel):
    """Immutable representation of a GraphEdge row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    node_id: str
    edge_label: str
    dest_id: str
    valid_from: datetime
    properties: dict[str, Any] = Field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class GraphBatch(BaseModel):
    """Immutable collection of GraphNode rows, active GraphEdge rows, and deferred edges."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: list[GraphNodeRow] = Field(default_factory=list)
    edges: list[GraphEdgeRow] = Field(default_factory=list)
    deferred_edges: list[GraphEdgeRow] = Field(default_factory=list)


class UpsertResult(BaseModel):
    """Summary returned by upsert_graph_batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes_upserted: int
    edges_upserted: int
    deferred_edges: int = 0
    batches_committed: int = 0


def _parse_utc(value: Any, fallback: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        dt = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    if fallback is not None:
        return fallback
    raise ValueError(f"Expected UTC timestamp, got {value!r}")


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate_text(text: Any, limit: int = MAX_EXCERPT_LEN) -> str:
    return "" if text is None else str(text)[:limit]


def _sanitize_properties(props: Mapping[str, Any]) -> dict[str, Any]:
    """Ensure author_ref is never present and all string values respect the 300-char ceiling."""
    return {
        key: (_truncate_text(val) if isinstance(val, str) else val)
        for key, val in props.items()
        if key != "author_ref"
    }


def _to_mapping(obj: Any) -> dict[str, Any]:
    if isinstance(obj, Mapping):
        return dict(obj)
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if hasattr(obj, "__dict__"):
        return dict(vars(obj))
    raise TypeError(f"Unsupported record type: {type(obj)!r}")


def _coerce_event_config(cfg: EventConfig | Mapping[str, Any]) -> EventConfig:
    return cfg if isinstance(cfg, EventConfig) else EventConfig.model_validate(dict(cfg))


def _normalize_enrichments(
    enrichments: Sequence[Any] | Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not enrichments:
        return {}
    if isinstance(enrichments, Mapping):
        return {str(k): _to_mapping(v) for k, v in enrichments.items()}
    by_item: dict[str, dict[str, Any]] = {}
    for raw in enrichments:
        row = _to_mapping(raw)
        item_id = str(row.get("item_id") or "").strip()
        if item_id:
            by_item[item_id] = row
    return by_item


def _normalize_account_map(
    account_map: Mapping[Any, Any] | Sequence[Any] | None,
    items: Sequence[dict[str, Any]],
) -> dict[tuple[str, str], int]:
    lookup: dict[tuple[str, str], int] = {}
    if isinstance(account_map, Mapping):
        for k, v in account_map.items():
            if isinstance(k, tuple) and len(k) == 2:
                lookup[(str(k[0]), str(k[1]))] = int(v)
            elif isinstance(k, str) and ":" in k:
                plat, aref = k.split(":", 1)
                lookup[(plat, aref)] = int(v)
    elif isinstance(account_map, Sequence):
        for entry in account_map:
            row = _to_mapping(entry)
            plat = str(row.get("platform") or "").strip()
            aref = str(row.get("author_ref") or "").strip()
            if plat and aref and row.get("account_n") is not None:
                lookup[(plat, aref)] = int(row["account_n"])

    next_n = max(lookup.values(), default=0) + 1
    for item in items:
        if _is_article_item(item):
            continue
        plat = str(item.get("platform") or "").strip()
        aref = str(item.get("author_ref") or "").strip()
        if plat and aref and (plat, aref) not in lookup:
            lookup[(plat, aref)] = next_n
            next_n += 1
    return lookup


def _is_article_item(item: Mapping[str, Any]) -> bool:
    kind = str(item.get("kind") or "").lower()
    platform = str(item.get("platform") or "").lower()
    return kind in ARTICLE_KINDS or platform in ARTICLE_PLATFORMS


def _extract_source_id(item_id: str) -> str:
    return item_id.split(":", 1)[1] if ":" in item_id else item_id


def _term_in_text(term: str, text: str) -> bool:
    cleaned = term.strip()
    if not cleaned or not text:
        return False
    prefix = r"(?<!\w)" if cleaned[0].isalnum() else ""
    suffix = r"(?!\w)" if cleaned[-1].isalnum() else ""
    return re.search(prefix + re.escape(cleaned) + suffix, text, flags=re.IGNORECASE) is not None


def _is_official_item(
    item: Mapping[str, Any],
    enrichment: Mapping[str, Any] | None,
    cfg: EventConfig,
) -> bool:
    if enrichment and bool(enrichment.get("is_official", False)):
        return True
    official_accts = {a.lstrip("@").strip().lower() for a in cfg.official.accounts if a.strip()}
    author_ref = str(item.get("author_ref") or "").lstrip("@").strip().lower()
    if author_ref and author_ref in official_accts:
        return True
    item_id = str(item.get("item_id") or "")
    if str(item.get("platform") or "") == "x" and _extract_source_id(item_id) in set(cfg.official.posts):
        return True
    url = str(item.get("url") or "").strip()
    if (url and url in set(cfg.official.urls)) or str(item.get("kind") or "") == "official_page":
        return True
    text = str(item.get("text") or "")
    return any(_term_in_text(person, text) for person in cfg.official.spokespeople)


def _compute_reach(item: Mapping[str, Any]) -> int:
    if item.get("reach") is not None:
        return int(item["reach"])
    eng = item.get("engagement")
    if isinstance(eng, Mapping):
        views = eng.get("views")
        if views is not None and int(views) > 0:
            return int(views)
        total = sum(int(eng.get(k) or 0) for k in ("likes", "reposts", "replies", "score"))
        if total > 0:
            return total
    return int(item["author_followers"]) if item.get("author_followers") is not None else 0


def _resolve_account_display(
    author_ref: str,
    followers: int | None,
    candidate_display: str | None,
    account_n: int,
    is_official_author: bool,
    cfg: EventConfig,
) -> tuple[str, bool]:
    """Determine (display, is_public_figure) under P5.2 and data-model.md Privacy."""
    norm_ref = author_ref.lstrip("@").strip().lower()
    for acct in cfg.official.accounts:
        cleaned = acct.lstrip("@").strip()
        if cleaned and cleaned.lower() == norm_ref:
            return cleaned, True

    if is_official_author and cfg.official.accounts:
        return cfg.official.accounts[0].lstrip("@").strip(), True

    public_figures = [
        *(p.strip() for p in cfg.official.spokespeople if p.strip()),
        *(c.person.strip() for c in cfg.conflicts_of_interest if c.person.strip()),
    ]
    for person in public_figures:
        compact = person.replace(" ", "").lower()
        cand_norm = candidate_display.lstrip("@").strip().lower() if candidate_display else ""
        if norm_ref in {person.lower(), compact} or cand_norm in {person.lower(), compact}:
            return person, True

    min_followers = cfg.privacy.min_followers_to_name
    if (
        min_followers is not None
        and followers is not None
        and int(followers) >= int(min_followers)
        and candidate_display
        and candidate_display.strip()
        and candidate_display.strip() != author_ref
    ):
        return _truncate_text(candidate_display.strip()), True

    return f"Account A{account_n}", False


def _extract_outlet_id(item: Mapping[str, Any]) -> str:
    explicit = str(item.get("outlet_id") or "").strip()
    if explicit:
        return explicit.lower().removeprefix("www.")
    url = str(item.get("url") or "").strip()
    if url:
        host = (urlparse(url).hostname or "").strip().lower().removeprefix("www.")
        if host:
            return host
    aref = str(item.get("author_ref") or "").strip().lower().removeprefix("www.")
    return aref if ("." in aref and " " not in aref) else "unknown-outlet"


def _resolve_outlet_name(outlet_id: str, cfg: EventConfig) -> str:
    compact_domain = re.sub(r"[^a-z0-9]", "", outlet_id.split(".")[0].lower())
    for known in cfg.known_coverage:
        compact_known = re.sub(r"[^a-z0-9]", "", known.lower())
        if compact_known and compact_known == compact_domain:
            return known
    return outlet_id


def _extract_article_title(item: Mapping[str, Any]) -> str:
    explicit = str(item.get("title") or "").strip()
    if explicit:
        return _truncate_text(explicit)
    text = str(item.get("text") or "").strip()
    return _truncate_text(text.split("\n\n", 1)[0].split("\n", 1)[0].strip()) if text else ""


def _resolve_link_target(link: str, url_to_item_id: Mapping[str, str]) -> str | None:
    cleaned = link.strip()
    if not cleaned:
        return None
    if cleaned in url_to_item_id:
        return url_to_item_id[cleaned]
    if re.match(r"^(?:x|hn|yt|gdelt|web|manual):[A-Za-z0-9_:\-]+$", cleaned):
        return cleaned
    x_match = _X_STATUS_RE.match(cleaned)
    if x_match:
        return f"x:{x_match.group(1)}"
    hn_match = _HN_ITEM_RE.match(cleaned)
    if hn_match:
        return f"hn:{hn_match.group(1)}"
    parsed = urlparse(cleaned)
    if parsed.hostname and "youtube.com" in parsed.hostname.lower():
        vid = parse_qs(parsed.query).get("v", [None])[0]
        if vid:
            return f"yt:{vid}"
    return None


def _collect_verified_claim_edges(
    cfg: EventConfig,
    items_by_id: Mapping[str, dict[str, Any]],
    item_valid_from: Mapping[str, datetime],
    enrich_by_id: Mapping[str, dict[str, Any]],
) -> tuple[list[GraphEdgeRow], dict[str, list[str]], dict[str, list[str]]]:
    """Verify evidence_span exact substring (P1.2) and source_item_id (P1.1) for claim edges."""
    claim_texts = {c.id: c.text for c in cfg.claims_to_check}
    edges: list[GraphEdgeRow] = []
    supporting: dict[str, list[str]] = {c.id: [] for c in cfg.claims_to_check}
    addressing: dict[str, list[str]] = {c.id: [] for c in cfg.claims_to_check}

    for item_id in sorted(items_by_id, key=lambda iid: (item_valid_from[iid], iid)):
        enrichment = enrich_by_id.get(item_id)
        if not enrichment or bool(enrichment.get("off_topic", False)):
            continue
        item_text = str(items_by_id[item_id].get("text") or "")
        for entry in enrichment.get("claims") or []:
            if not isinstance(entry, Mapping):
                continue
            cid = str(entry.get("claim_id") or "").strip()
            stance = str(entry.get("stance") or "").strip().lower()
            span = entry.get("evidence_span")
            if cid not in claim_texts or stance not in {"supports", "contradicts"}:
                continue
            if not isinstance(span, str) or not span or span not in item_text:
                continue
            src_id = str(entry.get("source_item_id") or item_id).strip()
            if not src_id or src_id not in items_by_id:
                continue
            addressing[cid].append(item_id)
            if stance == "supports":
                supporting[cid].append(item_id)
            edges.append(
                GraphEdgeRow(
                    event_id=cfg.event_id,
                    node_id=item_id,
                    edge_label="SUPPORTS" if stance == "supports" else "CONTRADICTS",
                    dest_id=cid,
                    valid_from=item_valid_from[item_id],
                    properties=_sanitize_properties(
                        {
                            "evidence_span": span,
                            "agreement": float(entry.get("agreement", 1.0)),
                            "source_item_id": src_id,
                        }
                    ),
                )
            )
    return edges, supporting, addressing


def _build_claim_nodes(
    cfg: EventConfig,
    item_valid_from: Mapping[str, datetime],
    item_is_official: Mapping[str, bool],
    supporting_by_claim: Mapping[str, list[str]],
    addressing_by_claim: Mapping[str, list[str]],
) -> tuple[list[GraphNodeRow], dict[str, bool]]:
    """Build Claim nodes with P2.1/P2.2 self_reported and confirmed_by properties."""
    claim_nodes: list[GraphNodeRow] = []
    claim_self_reported: dict[str, bool] = {}

    for claim_cfg in cfg.claims_to_check:
        cid = claim_cfg.id
        addr_items = addressing_by_claim.get(cid, [])
        if not addr_items:
            continue
        supp_items = supporting_by_claim.get(cid, [])
        official_supp = [iid for iid in supp_items if item_is_official.get(iid, False)]
        official_addr = [iid for iid in addr_items if item_is_official.get(iid, False)]

        source_item_id = (official_supp or supp_items or official_addr or addr_items)[0]
        independent_confirmations = [
            iid
            for iid in supp_items
            if not item_is_official.get(iid, False) and iid != source_item_id
        ]
        if not item_is_official.get(source_item_id, False) and source_item_id in supp_items:
            independent_confirmations = [source_item_id, *independent_confirmations]

        originates_from_official = bool(
            official_supp or item_is_official.get(source_item_id, False)
        )
        is_self_reported = originates_from_official and len(independent_confirmations) == 0
        claim_self_reported[cid] = is_self_reported

        claim_nodes.append(
            GraphNodeRow(
                event_id=cfg.event_id,
                node_id=cid,
                label="Claim",
                valid_from=min(item_valid_from[iid] for iid in addr_items),
                platform=None,
                storyline_id=None,
                properties=_sanitize_properties(
                    {
                        "text": claim_cfg.text,
                        "source_item_id": source_item_id,
                        "self_reported": is_self_reported,
                        "confirmed_by": independent_confirmations,
                    }
                ),
            )
        )
    return claim_nodes, claim_self_reported


def _add_article_and_outlet(
    iid: str,
    item: Mapping[str, Any],
    config: EventConfig,
    *,
    valid_from: datetime,
    platform: str | None,
    storyline_id: str,
    lang: Any,
    self_rep: bool,
    conflict_person: Any,
    nodes_by_id: dict[str, GraphNodeRow],
    candidate_edges: list[GraphEdgeRow],
) -> None:
    outlet_id = _extract_outlet_id(item)
    nodes_by_id[iid] = GraphNodeRow(
        event_id=config.event_id,
        node_id=iid,
        label="Article",
        valid_from=valid_from,
        platform=platform,
        storyline_id=storyline_id,
        properties=_sanitize_properties(
            {
                "title": _extract_article_title(item),
                "url": item.get("url"),
                "outlet_id": outlet_id,
                "time_approximate": bool(item.get("time_approximate", False)),
                "lang": lang,
                "self_reported": self_rep,
                "conflict_person": conflict_person,
            }
        ),
    )
    existing_outlet = nodes_by_id.get(outlet_id)
    outlet_ts = min(existing_outlet.valid_from, valid_from) if existing_outlet else valid_from
    nodes_by_id[outlet_id] = GraphNodeRow(
        event_id=config.event_id,
        node_id=outlet_id,
        label="Outlet",
        valid_from=outlet_ts,
        platform=platform,
        storyline_id=None,
        properties=_sanitize_properties({"name": _resolve_outlet_name(outlet_id, config)}),
    )
    candidate_edges.append(
        GraphEdgeRow(
            event_id=config.event_id,
            node_id=iid,
            edge_label="PUBLISHED_BY",
            dest_id=outlet_id,
            valid_from=valid_from,
            properties={},
        )
    )


def _add_post_and_account(
    iid: str,
    item: Mapping[str, Any],
    config: EventConfig,
    acct_lookup: Mapping[tuple[str, str], int],
    *,
    valid_from: datetime,
    platform: str | None,
    storyline_id: str,
    lang: Any,
    is_off: bool,
    self_rep: bool,
    conflict_person: Any,
    nodes_by_id: dict[str, GraphNodeRow],
    candidate_edges: list[GraphEdgeRow],
) -> None:
    nodes_by_id[iid] = GraphNodeRow(
        event_id=config.event_id,
        node_id=iid,
        label="Post",
        valid_from=valid_from,
        platform=platform,
        storyline_id=storyline_id,
        properties=_sanitize_properties(
            {
                "kind": str(item.get("kind") or "post"),
                "text_excerpt": _truncate_text(item.get("text") or ""),
                "reach": _compute_reach(item),
                "lang": lang,
                "is_official": is_off,
                "self_reported": self_rep,
                "conflict_person": conflict_person,
                "url": item.get("url"),
            }
        ),
    )
    author_ref = str(item.get("author_ref") or "").strip()
    if not (platform and author_ref and (platform, author_ref) in acct_lookup):
        return
    acct_n = acct_lookup[(platform, author_ref)]
    acct_id = f"{platform}:acct:{acct_n}"
    followers = int(item["author_followers"]) if item.get("author_followers") is not None else None
    cand_display = item.get("author_display") or item.get("author_name") or item.get("display")
    display, is_pub = _resolve_account_display(
        author_ref,
        followers,
        str(cand_display) if cand_display is not None else None,
        acct_n,
        is_off,
        config,
    )
    existing_acct = nodes_by_id.get(acct_id)
    acct_ts = min(existing_acct.valid_from, valid_from) if existing_acct else valid_from
    nodes_by_id[acct_id] = GraphNodeRow(
        event_id=config.event_id,
        node_id=acct_id,
        label="Account",
        valid_from=acct_ts,
        platform=platform,
        storyline_id=None,
        properties=_sanitize_properties(
            {"display": display, "is_public_figure": is_pub, "followers": followers}
        ),
    )
    candidate_edges.append(
        GraphEdgeRow(
            event_id=config.event_id,
            node_id=iid,
            edge_label="AUTHORED_BY",
            dest_id=acct_id,
            valid_from=valid_from,
            properties={},
        )
    )


def _add_item_reference_edges(
    iid: str,
    item: Mapping[str, Any],
    enrichment: Mapping[str, Any],
    config: EventConfig,
    *,
    valid_from: datetime,
    storyline_id: str,
    item_valid_from: Mapping[str, datetime],
    url_to_item_id: Mapping[str, str],
    candidate_edges: list[GraphEdgeRow],
) -> None:
    if storyline_id and storyline_id != "unassigned" and not bool(enrichment.get("off_topic", False)):
        sim = enrichment.get("storyline_similarity")
        candidate_edges.append(
            GraphEdgeRow(
                event_id=config.event_id,
                node_id=iid,
                edge_label="BELONGS_TO",
                dest_id=storyline_id,
                valid_from=valid_from,
                properties={"similarity": float(sim) if sim is not None else None},
            )
        )
    parent_id = str(item.get("parent_id") or "").strip()
    if parent_id:
        candidate_edges.append(
            GraphEdgeRow(
                event_id=config.event_id,
                node_id=iid,
                edge_label="REPLIED_TO",
                dest_id=parent_id,
                valid_from=max(valid_from, item_valid_from.get(parent_id, valid_from)),
                properties={},
            )
        )
    quote_of = str(item.get("quote_of") or "").strip()
    if quote_of:
        candidate_edges.append(
            GraphEdgeRow(
                event_id=config.event_id,
                node_id=iid,
                edge_label="QUOTED",
                dest_id=quote_of,
                valid_from=max(valid_from, item_valid_from.get(quote_of, valid_from)),
                properties={},
            )
        )
    for raw_link in item.get("links") or []:
        target_id = _resolve_link_target(str(raw_link), url_to_item_id)
        if target_id and target_id != iid:
            candidate_edges.append(
                GraphEdgeRow(
                    event_id=config.event_id,
                    node_id=iid,
                    edge_label="LINKS_TO",
                    dest_id=target_id,
                    valid_from=max(valid_from, item_valid_from.get(target_id, valid_from)),
                    properties={"explicit": True},
                )
            )


def build_graph_mutations(
    cfg: EventConfig | Mapping[str, Any],
    items: Sequence[Any],
    enrichments: Sequence[Any] | Mapping[str, Any] | None,
    account_map: Mapping[Any, Any] | Sequence[Any] | None,
    *,
    existing_node_ids: set[str] | None = None,
) -> GraphBatch:
    """Build GraphNode, GraphEdge, and deferred GraphEdge rows from event items and enrichments."""
    config = _coerce_event_config(cfg)
    t0_dt = config.t0.utc
    item_dicts = [_to_mapping(it) for it in items]
    enrich_by_id = _normalize_enrichments(enrichments)
    acct_lookup = _normalize_account_map(account_map, item_dicts)

    items_by_id: dict[str, dict[str, Any]] = {}
    item_valid_from: dict[str, datetime] = {}
    item_is_official: dict[str, bool] = {}
    url_to_item_id: dict[str, str] = {}

    for item in item_dicts:
        iid = str(item.get("item_id") or "").strip()
        if not iid:
            continue
        items_by_id[iid] = item
        item_valid_from[iid] = _parse_utc(item.get("created_at") or item.get("collected_at"), fallback=t0_dt)
        item_is_official[iid] = _is_official_item(item, enrich_by_id.get(iid), config)
        url = str(item.get("url") or "").strip()
        if url:
            url_to_item_id[url] = iid

    claim_edges, supporting_by_claim, addressing_by_claim = _collect_verified_claim_edges(
        config, items_by_id, item_valid_from, enrich_by_id
    )
    claim_nodes, claim_self_reported = _build_claim_nodes(
        config, item_valid_from, item_is_official, supporting_by_claim, addressing_by_claim
    )

    earliest_any = min(item_valid_from.values(), default=t0_dt)
    base_ts = min(t0_dt, earliest_any)
    nodes_by_id: dict[str, GraphNodeRow] = {
        config.event_id: GraphNodeRow(
            event_id=config.event_id,
            node_id=config.event_id,
            label="Event",
            valid_from=base_ts,
            properties=_sanitize_properties({"name": config.name, "t0": _utc_iso(t0_dt)}),
        )
    }
    for idx, sl in enumerate(config.seed_storylines):
        nodes_by_id[sl.id] = GraphNodeRow(
            event_id=config.event_id,
            node_id=sl.id,
            label="Storyline",
            valid_from=base_ts,
            storyline_id=sl.id,
            properties=_sanitize_properties({"name": sl.name, "colour_index": idx}),
        )
    for cnode in claim_nodes:
        nodes_by_id[cnode.node_id] = cnode

    candidate_edges: list[GraphEdgeRow] = []
    for iid, item in items_by_id.items():
        enrichment = enrich_by_id.get(iid, {})
        valid_from = item_valid_from[iid]
        platform = str(item.get("platform") or "").strip() or None
        storyline_id = str(enrichment.get("storyline_id") or item.get("storyline_id") or "unassigned")
        lang = enrichment.get("lang") or item.get("lang")
        conflict_person = enrichment.get("conflict_person") or item.get("conflict_person")
        is_off = item_is_official[iid]
        supp_cids = [
            str(c.get("claim_id"))
            for c in (enrichment.get("claims") or [])
            if isinstance(c, Mapping) and str(c.get("stance") or "") == "supports"
        ]
        self_rep = (
            (any(claim_self_reported.get(cid, True) for cid in supp_cids) if supp_cids else True)
            if is_off
            else False
        )
        if _is_article_item(item):
            _add_article_and_outlet(
                iid, item, config, valid_from=valid_from, platform=platform,
                storyline_id=storyline_id, lang=lang, self_rep=self_rep,
                conflict_person=conflict_person, nodes_by_id=nodes_by_id,
                candidate_edges=candidate_edges,
            )
        else:
            _add_post_and_account(
                iid, item, config, acct_lookup, valid_from=valid_from, platform=platform,
                storyline_id=storyline_id, lang=lang, is_off=is_off, self_rep=self_rep,
                conflict_person=conflict_person, nodes_by_id=nodes_by_id,
                candidate_edges=candidate_edges,
            )
        _add_item_reference_edges(
            iid, item, enrichment, config, valid_from=valid_from, storyline_id=storyline_id,
            item_valid_from=item_valid_from, url_to_item_id=url_to_item_id,
            candidate_edges=candidate_edges,
        )

    candidate_edges.extend(claim_edges)
    known_ids = set(nodes_by_id.keys()) | set(existing_node_ids or set())
    active_edges: dict[tuple[str, str, str, str], GraphEdgeRow] = {}
    deferred_edges: dict[tuple[str, str, str, str], GraphEdgeRow] = {}

    for edge in candidate_edges:
        ekey = (edge.event_id, edge.node_id, edge.edge_label, edge.dest_id)
        if edge.node_id in known_ids and edge.dest_id in known_ids:
            active_edges[ekey] = edge
        else:
            deferred_edges[ekey] = edge

    return GraphBatch(
        nodes=list(nodes_by_id.values()),
        edges=list(active_edges.values()),
        deferred_edges=list(deferred_edges.values()),
    )


def _get_spanner_database(
    database: Any | None = None,
    settings: SpannerSettings | None = None,
) -> Any:
    if database is not None:
        return database
    cfg = settings or SpannerSettings()
    if not cfg.spanner_instance.strip():
        raise ValueError("SPANNER_INSTANCE environment variable is required when database is not provided")
    client = spanner.Client(project=cfg.gcp_project or None)
    return client.instance(cfg.spanner_instance.strip()).database(cfg.spanner_database.strip() or "verifact")


def _node_to_spanner_tuple(node: GraphNodeRow) -> tuple[Any, ...]:
    return (
        node.event_id,
        node.node_id,
        node.label,
        node.valid_from,
        node.platform,
        node.storyline_id,
        JsonObject(_sanitize_properties(node.properties)),
        spanner.COMMIT_TIMESTAMP,
    )


def _edge_to_spanner_tuple(edge: GraphEdgeRow) -> tuple[Any, ...]:
    return (
        edge.event_id,
        edge.node_id,
        edge.edge_label,
        edge.dest_id,
        edge.valid_from,
        JsonObject(_sanitize_properties(edge.properties)),
        spanner.COMMIT_TIMESTAMP,
    )


def upsert_graph_batch(
    batch: GraphBatch,
    *,
    database: Any | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> UpsertResult:
    """Idempotently upsert GraphNode and GraphEdge rows using insert_or_update mutations."""
    if not batch.nodes and not batch.edges:
        return UpsertResult(
            nodes_upserted=0,
            edges_upserted=0,
            deferred_edges=len(batch.deferred_edges),
            batches_committed=0,
        )

    db = _get_spanner_database(database)
    effective_batch_size = max(1, int(batch_size))
    node_tuples = [_node_to_spanner_tuple(n) for n in batch.nodes]
    edge_tuples = [_edge_to_spanner_tuple(e) for e in batch.edges]
    batches_committed = 0

    if len(node_tuples) + len(edge_tuples) <= effective_batch_size:
        with db.batch() as mutation_batch:
            if node_tuples:
                mutation_batch.insert_or_update(
                    table="GraphNode", columns=GRAPH_NODE_COLUMNS, values=node_tuples
                )
            if edge_tuples:
                mutation_batch.insert_or_update(
                    table="GraphEdge", columns=GRAPH_EDGE_COLUMNS, values=edge_tuples
                )
        batches_committed = 1
    else:
        for start in range(0, len(node_tuples), effective_batch_size):
            with db.batch() as mutation_batch:
                mutation_batch.insert_or_update(
                    table="GraphNode",
                    columns=GRAPH_NODE_COLUMNS,
                    values=node_tuples[start : start + effective_batch_size],
                )
            batches_committed += 1
        for start in range(0, len(edge_tuples), effective_batch_size):
            with db.batch() as mutation_batch:
                mutation_batch.insert_or_update(
                    table="GraphEdge",
                    columns=GRAPH_EDGE_COLUMNS,
                    values=edge_tuples[start : start + effective_batch_size],
                )
            batches_committed += 1

    logger.info(
        json.dumps(
            {
                "event": "spanner_graph_upserted",
                "nodes_upserted": len(node_tuples),
                "edges_upserted": len(edge_tuples),
                "deferred_edges": len(batch.deferred_edges),
                "batches_committed": batches_committed,
            }
        )
    )
    return UpsertResult(
        nodes_upserted=len(node_tuples),
        edges_upserted=len(edge_tuples),
        deferred_edges=len(batch.deferred_edges),
        batches_committed=batches_committed,
    )


def _decode_json_props(raw_props: Any) -> dict[str, Any]:
    if isinstance(raw_props, Mapping):
        return _sanitize_properties(dict(raw_props))
    if isinstance(raw_props, str) and raw_props.strip():
        parsed = json.loads(raw_props)
        if isinstance(parsed, Mapping):
            return _sanitize_properties(dict(parsed))
    return {}


def _unpack_node_row(
    row: Any, t0_dt: datetime
) -> tuple[str, str, datetime, str | None, str | None, dict[str, Any]]:
    if isinstance(row, Mapping):
        return (
            str(row["node_id"]),
            str(row["label"]),
            _parse_utc(row["valid_from"], fallback=t0_dt),
            row.get("platform"),
            row.get("storyline_id"),
            _decode_json_props(row.get("properties")),
        )
    nid, lbl, vfrom_raw, plat, sid, props_raw = row
    return (
        str(nid),
        str(lbl),
        _parse_utc(vfrom_raw, fallback=t0_dt),
        plat,
        sid,
        _decode_json_props(props_raw),
    )


def _unpack_edge_row(row: Any, t0_dt: datetime) -> tuple[str, str, str, datetime, dict[str, Any]]:
    if isinstance(row, Mapping):
        return (
            str(row["node_id"]),
            str(row["edge_label"]),
            str(row["dest_id"]),
            _parse_utc(row["valid_from"], fallback=t0_dt),
            _decode_json_props(row.get("properties")),
        )
    src, elbl, dst, vfrom_raw, props_raw = row
    return (
        str(src),
        str(elbl),
        str(dst),
        _parse_utc(vfrom_raw, fallback=t0_dt),
        _decode_json_props(props_raw),
    )


def _format_api_node(
    nid: str,
    lbl: str,
    vfrom: datetime,
    plat: str | None,
    sid: str | None,
    props: Mapping[str, Any],
    *,
    t0_dt: datetime,
    author_of_post: Mapping[str, str],
    outlet_of_article: Mapping[str, str],
    account_displays: Mapping[str, str],
    outlet_names: Mapping[str, str],
) -> dict[str, Any]:
    ntype = lbl.lower()
    hour = int(math.floor((vfrom - t0_dt).total_seconds() / 3600.0))
    if ntype == "post":
        acct_id = author_of_post.get(nid)
        display_label = (
            account_displays.get(acct_id, "")
            if acct_id
            else str(props.get("label") or props.get("text_excerpt") or nid)
        ) or "Account A0"
        kind = str(props.get("kind") or "post")
    elif ntype == "article":
        out_id = outlet_of_article.get(nid) or str(props.get("outlet_id") or "")
        display_label = outlet_names.get(out_id) or out_id or str(props.get("title") or nid)
        kind = "article"
    elif ntype == "account":
        display_label = str(props.get("display") or "Account A0")
        kind = "account"
    elif ntype == "claim":
        display_label = str(props.get("text") or nid)
        kind = "claim"
    else:
        display_label = str(props.get("name") or nid)
        kind = ntype

    node_dict: dict[str, Any] = {
        "id": nid,
        "type": ntype,
        "platform": plat,
        "kind": kind,
        "created_at": _utc_iso(vfrom),
        "hour": hour,
        "storyline_id": sid,
        "reach": int(props.get("reach") or props.get("followers") or 0),
        "is_official": bool(props.get("is_official", False)),
        "self_reported": bool(props.get("self_reported", False)),
        "conflict": props.get("conflict") if "conflict" in props else props.get("conflict_person"),
        "lang": props.get("lang"),
        "label": _truncate_text(display_label),
    }
    for optional_key in (
        "url",
        "text_excerpt",
        "title",
        "outlet_id",
        "time_approximate",
        "conflict_person",
        "colour_index",
        "source_item_id",
        "confirmed_by",
        "is_public_figure",
        "followers",
    ):
        if optional_key in props:
            node_dict[optional_key] = props[optional_key]
    return node_dict


def query_graph_at_hour(
    event_id: str,
    at_hour: int | float | None,
    t0_utc: datetime | str,
    *,
    database: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute GQL queries on verifact_graph returning api.md Node and Edge dicts with valid_from <= @t."""
    t0_dt = _parse_utc(t0_utc)
    cutoff_dt = datetime.now(timezone.utc) if at_hour is None else t0_dt + timedelta(hours=float(at_hour))
    db = _get_spanner_database(database)

    node_gql = (
        "GRAPH verifact_graph "
        "MATCH (n) "
        "WHERE n.event_id = @event_id AND n.valid_from <= @t "
        "RETURN n.node_id AS node_id, n.label AS label, n.valid_from AS valid_from, "
        "n.platform AS platform, n.storyline_id AS storyline_id, n.properties AS properties"
    )
    edge_gql = (
        "GRAPH verifact_graph "
        "MATCH (s)-[e]->(d) "
        "WHERE e.event_id = @event_id AND e.valid_from <= @t "
        "AND s.valid_from <= @t AND d.valid_from <= @t "
        "RETURN e.node_id AS node_id, e.edge_label AS edge_label, e.dest_id AS dest_id, "
        "e.valid_from AS valid_from, e.properties AS properties"
    )
    params = {"event_id": event_id, "t": cutoff_dt}
    p_types = {"event_id": param_types.STRING, "t": param_types.TIMESTAMP}

    with db.snapshot() as snapshot:
        raw_nodes = list(snapshot.execute_sql(node_gql, params=params, param_types=p_types))
        raw_edges = list(snapshot.execute_sql(edge_gql, params=params, param_types=p_types))

    parsed_nodes: list[tuple[str, str, datetime, str | None, str | None, dict[str, Any]]] = []
    account_displays: dict[str, str] = {}
    outlet_names: dict[str, str] = {}
    valid_node_ids: set[str] = set()

    for row in raw_nodes:
        nid, lbl, vfrom, plat, sid, props = _unpack_node_row(row, t0_dt)
        if vfrom > cutoff_dt:
            continue
        valid_node_ids.add(nid)
        parsed_nodes.append((nid, lbl, vfrom, plat, sid, props))
        if lbl.lower() == "account":
            account_displays[nid] = str(props.get("display") or "Account A0")
        elif lbl.lower() == "outlet":
            outlet_names[nid] = str(props.get("name") or nid)

    author_of_post: dict[str, str] = {}
    outlet_of_article: dict[str, str] = {}
    edges_out: list[dict[str, Any]] = []

    for row in raw_edges:
        src, elbl, dst, vfrom, props = _unpack_edge_row(row, t0_dt)
        if vfrom > cutoff_dt or src not in valid_node_ids or dst not in valid_node_ids:
            continue
        etype = elbl.lower()
        if etype == "authored_by":
            author_of_post[src] = dst
        elif etype == "published_by":
            outlet_of_article[src] = dst
        edge_dict: dict[str, Any] = {
            "source": src,
            "target": dst,
            "type": etype,
            "created_at": _utc_iso(vfrom),
            "hour": int(math.floor((vfrom - t0_dt).total_seconds() / 3600.0)),
        }
        for extra_key in ("evidence_span", "agreement", "source_item_id", "similarity", "explicit"):
            if extra_key in props:
                edge_dict[extra_key] = props[extra_key]
        edges_out.append(edge_dict)

    nodes_out = [
        _format_api_node(
            nid, lbl, vfrom, plat, sid, props,
            t0_dt=t0_dt,
            author_of_post=author_of_post,
            outlet_of_article=outlet_of_article,
            account_displays=account_displays,
            outlet_names=outlet_names,
        )
        for nid, lbl, vfrom, plat, sid, props in parsed_nodes
    ]
    return nodes_out, edges_out
