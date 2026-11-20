"""Frame builder Cloud Run Job and pure graph/snapshot derivation helpers (Spec 08)."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlparse

from google.cloud import bigquery
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from verifact_core.bq import write_rows
from verifact_core.config import EventConfig, load_event

logger = logging.getLogger("services.frame_builder")

MAX_EXCERPT_LEN = 300
SIGNAL_MIN_ITEMS = 3
SIGNAL_MAX_WINDOW_MINUTES = 15
SELF_REPORTED_NOTE = "Company statement, not independently confirmed"
DESCRIPTIVE_NOTE = "Descriptive comparison, not a causal estimate."
NEUTRAL_SIGNAL_TEMPLATE = (
    "{n} near-identical posts within {m} minutes. "
    "This can reflect organic copy-paste or coordination; no account is labelled."
)


class FrameBuilderSettings(BaseSettings):
    """Environment settings for the frame-builder job."""

    model_config = SettingsConfigDict(extra="ignore")

    gcp_project: str = Field(default="", validation_alias="GCP_PROJECT")
    bq_dataset: str = Field(default="verifact", validation_alias="BQ_DATASET")
    prompt_version: str = Field(default="v1", validation_alias="PROMPT_VERSION")


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
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return None


def _compute_hour(ts: datetime, t0: datetime) -> int:
    return math.floor((ts - t0).total_seconds() / 3600.0)


def _normalize_text_for_cluster(text: str) -> str:
    cleaned = re.sub(r"https?://\S+", "", text.lower())
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    return " ".join(cleaned.split())


def _term_in_text(term: str, text: str) -> bool:
    cleaned = term.strip()
    if not cleaned or not text:
        return False
    prefix = r"(?<!\w)" if cleaned[0].isalnum() else ""
    suffix = r"(?!\w)" if cleaned[-1].isalnum() else ""
    return re.search(prefix + re.escape(cleaned) + suffix, text, flags=re.IGNORECASE) is not None


def _truncate_text(text: str, limit: int = MAX_EXCERPT_LEN) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _to_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        return dict(obj.model_dump())
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return dict(obj)


def _coerce_account_lookup(
    account_map: Mapping[tuple[str, str], int] | Sequence[Mapping[str, Any]] | None,
    items: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], int]:
    lookup: dict[tuple[str, str], int] = {}
    if isinstance(account_map, Mapping):
        for k, v in account_map.items():
            if isinstance(k, tuple) and len(k) == 2:
                lookup[(str(k[0]), str(k[1]))] = int(v)
    elif isinstance(account_map, Sequence):
        for row in account_map:
            r = _to_dict(row)
            platform = str(r.get("platform") or "")
            author_ref = str(r.get("author_ref") or "")
            account_n = r.get("account_n")
            if platform and author_ref and account_n is not None:
                lookup[(platform, author_ref)] = int(account_n)

    next_n = max(lookup.values(), default=0) + 1
    sorted_items = sorted(
        (_to_dict(it) for it in items),
        key=lambda r: (
            _parse_ts(r.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
            str(r.get("item_id") or ""),
        ),
    )
    for item in sorted_items:
        platform = str(item.get("platform") or "")
        author_ref = item.get("author_ref")
        if platform and author_ref:
            key = (platform, str(author_ref))
            if key not in lookup:
                lookup[key] = next_n
                next_n += 1
    return lookup


def _resolve_author_label(
    cfg: EventConfig,
    item: Mapping[str, Any],
    account_lookup: Mapping[tuple[str, str], int],
) -> tuple[str, bool]:
    """Return (public_or_pseudonym_label, is_public_figure) obeying data-model.md Privacy rules."""
    platform = str(item.get("platform") or "")
    raw_author = item.get("author_ref")
    author_ref = str(raw_author).strip() if raw_author is not None else ""

    if platform in {"news", "web"}:
        if author_ref:
            return _truncate_text(author_ref, 120), True
        url = str(item.get("url") or "")
        domain = urlparse(url).netloc if url else ""
        return (domain or platform), True

    official_map = {a.lstrip("@").strip().lower(): a.lstrip("@").strip() for a in cfg.official.accounts if a.strip()}
    spokespeople_map = {s.strip().lower(): s.strip() for s in cfg.official.spokespeople if s.strip()}
    coi_map = {c.person.strip().lower(): c.person.strip() for c in cfg.conflicts_of_interest if c.person.strip()}

    norm_author = author_ref.lstrip("@").lower()
    if norm_author and norm_author in official_map:
        return official_map[norm_author], True
    if norm_author and norm_author in spokespeople_map:
        return spokespeople_map[norm_author], True
    if norm_author and norm_author in coi_map:
        return coi_map[norm_author], True

    # Also match without spaces for handle equivalents of named public figures
    nospace_map = {k.replace(" ", ""): v for k, v in {**spokespeople_map, **coi_map}.items()}
    if norm_author and norm_author in nospace_map:
        return nospace_map[norm_author], True

    min_followers = cfg.privacy.min_followers_to_name
    followers_raw = item.get("author_followers")
    followers = int(followers_raw) if isinstance(followers_raw, (int, float)) else None
    if (
        min_followers is not None
        and followers is not None
        and followers >= min_followers
        and author_ref
    ):
        return _truncate_text(author_ref, 80), True

    account_n = account_lookup.get((platform, author_ref), 1)
    return f"Account A{account_n}", False


def _compute_reach(item: Mapping[str, Any]) -> int:
    eng = item.get("engagement")
    eng_dict = _to_dict(eng) if isinstance(eng, Mapping) or hasattr(eng, "__dict__") else {}
    views = int(eng_dict.get("views") or 0)
    likes = int(eng_dict.get("likes") or 0)
    reposts = int(eng_dict.get("reposts") or 0)
    replies = int(eng_dict.get("replies") or 0)
    score = int(eng_dict.get("score") or 0)
    followers = int(item.get("author_followers") or 0)
    weighted = likes + (reposts * 2) + replies + score
    return max(1, views, weighted, followers)


def _lookup_conflict(
    cfg: EventConfig,
    item: Mapping[str, Any],
    enrichment: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    coi_by_lower = {
        c.person.strip().lower(): {"person": c.person.strip(), "relationship": c.relationship}
        for c in cfg.conflicts_of_interest
        if c.person.strip()
    }
    if not coi_by_lower:
        return None

    if enrichment:
        cp = enrichment.get("conflict_person")
        if isinstance(cp, str) and cp.strip().lower() in coi_by_lower:
            return dict(coi_by_lower[cp.strip().lower()])

    author_ref = str(item.get("author_ref") or "").lstrip("@").strip().lower()
    text = str(item.get("text") or "")
    for person_lower, disclosure in coi_by_lower.items():
        if author_ref and author_ref in {person_lower, person_lower.replace(" ", "")}:
            return dict(disclosure)
        if _term_in_text(disclosure["person"], text):
            return dict(disclosure)
    return None


def _is_item_official(
    cfg: EventConfig,
    item: Mapping[str, Any],
    enrichment: Mapping[str, Any] | None,
) -> bool:
    if enrichment and bool(enrichment.get("is_official")):
        return True
    official_accounts = {a.lstrip("@").strip().lower() for a in cfg.official.accounts if a.strip()}
    author_ref = str(item.get("author_ref") or "").lstrip("@").strip().lower()
    if author_ref and author_ref in official_accounts:
        return True
    item_id = str(item.get("item_id") or "")
    source_id = item_id.split(":", 1)[1] if ":" in item_id else item_id
    if str(item.get("platform") or "") == "x" and source_id in set(cfg.official.posts):
        return True
    if str(item.get("kind") or "") == "official_page":
        return True
    text = str(item.get("text") or "")
    return any(_term_in_text(person, text) for person in cfg.official.spokespeople if person.strip())


def _latest_enrichments_by_item(
    enrichments: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_item: dict[str, dict[str, Any]] = {}
    for raw in enrichments:
        row = _to_dict(raw)
        item_id = str(row.get("item_id") or "")
        if not item_id:
            continue
        existing = by_item.get(item_id)
        if existing is None:
            by_item[item_id] = row
            continue
        ts_new = _parse_ts(row.get("enriched_at")) or datetime.min.replace(tzinfo=timezone.utc)
        ts_old = _parse_ts(existing.get("enriched_at")) or datetime.min.replace(tzinfo=timezone.utc)
        if ts_new >= ts_old:
            by_item[item_id] = row
    return by_item


def _build_url_to_item_lookup(items_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    url_map: dict[str, str] = {}
    for item_id, item in items_by_id.items():
        url = str(item.get("url") or "").strip()
        if url:
            url_map[url] = item_id
        if item_id.startswith("x:"):
            post_id = item_id.split(":", 1)[1]
            url_map[f"https://x.com/i/status/{post_id}"] = item_id
            url_map[f"https://twitter.com/i/status/{post_id}"] = item_id
    return url_map


def _extract_linked_item_ids(
    item: Mapping[str, Any],
    items_by_id: Mapping[str, Mapping[str, Any]],
    url_map: Mapping[str, str],
) -> list[str]:
    item_id = str(item.get("item_id") or "")
    raw_links = item.get("links") or []
    targets: list[str] = []
    seen: set[str] = set()

    for raw_link in raw_links:
        link = str(raw_link or "").strip()
        if not link:
            continue
        target_id = url_map.get(link)
        if target_id is None:
            m_x = re.search(r"(?:x\.com|twitter\.com)/[^/]+/status/(\d+)", link)
            if m_x:
                cand = f"x:{m_x.group(1)}"
                if cand in items_by_id:
                    target_id = cand
            m_hn = re.search(r"news\.ycombinator\.com/item\?id=(\d+)", link)
            if target_id is None and m_hn:
                cand = f"hn:{m_hn.group(1)}"
                if cand in items_by_id:
                    target_id = cand
        if target_id and target_id != item_id and target_id not in seen:
            seen.add(target_id)
            targets.append(target_id)
    return targets


def _determine_end_hour(
    cfg: EventConfig,
    item_hours: Sequence[int],
    now: datetime,
) -> int:
    start_hour = -int(cfg.window.pre_hours)
    raw_now_hour = _compute_hour(now, cfg.t0.utc)
    if not cfg.window.live:
        end_hour = min(raw_now_hour, int(cfg.window.post_hours))
    else:
        end_hour = raw_now_hour
    if item_hours:
        end_hour = max(end_hour, max(item_hours))
        if not cfg.window.live:
            end_hour = min(end_hour, int(cfg.window.post_hours))
    return max(start_hour, end_hour)


def build_frames(
    cfg: EventConfig,
    items: Sequence[Mapping[str, Any]],
    enrichments: Sequence[Mapping[str, Any]],
    account_map: Mapping[tuple[str, str], int] | Sequence[Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Build hourly replay frames in the exact data-model.md and api.md shapes."""
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    built_at_iso = _utc_iso(now_dt)
    t0_dt = cfg.t0.utc

    item_dicts = [_to_dict(it) for it in items]
    enrich_by_item = _latest_enrichments_by_item(enrichments)
    account_lookup = _coerce_account_lookup(account_map, item_dicts)
    valid_storylines = {s.id for s in cfg.seed_storylines}
    valid_claims = {c.id for c in cfg.claims_to_check}

    # Check which claims have independent confirmation for P2.2 self_reported resolution
    claim_cards = compute_claim_cards(cfg, item_dicts, enrichments)
    claim_self_reported = {c["claim_id"]: bool(c["self_reported"]) for c in claim_cards}

    prepared_items: dict[str, dict[str, Any]] = {}
    node_info: dict[str, tuple[int, datetime, dict[str, Any]]] = {}

    for item in sorted(item_dicts, key=lambda r: str(r.get("item_id") or "")):
        item_id = str(item.get("item_id") or "").strip()
        created_dt = _parse_ts(item.get("created_at")) or _parse_ts(item.get("collected_at"))
        if not item_id or created_dt is None:
            continue

        hour = _compute_hour(created_dt, t0_dt)
        enr = enrich_by_item.get(item_id)
        raw_sid = str(enr.get("storyline_id") or "unassigned") if enr else "unassigned"
        if enr and bool(enr.get("off_topic")):
            storyline_id = "unassigned"
        elif raw_sid in valid_storylines:
            storyline_id = raw_sid
        else:
            storyline_id = "unassigned"

        platform = str(item.get("platform") or "x")
        kind = str(item.get("kind") or ("article" if platform in {"news", "web"} else "post"))
        node_type = "article" if platform in {"news", "web"} or kind in {"article", "official_page"} else "post"
        is_official = _is_item_official(cfg, item, enr)

        # P2.1 / P2.2: self_reported on official items unless claim is independently confirmed
        item_claims = (enr.get("claims") or []) if enr else []
        confirmed_any = any(
            isinstance(c, Mapping)
            and c.get("claim_id") in claim_self_reported
            and not claim_self_reported[str(c.get("claim_id"))]
            for c in item_claims
        )
        self_reported = bool(is_official and not confirmed_any)
        conflict = _lookup_conflict(cfg, item, enr)
        lang = str((enr.get("lang") if enr else None) or item.get("lang") or "und")
        author_label, _ = _resolve_author_label(cfg, item, account_lookup)

        node_obj: dict[str, Any] = {
            "id": item_id,
            "type": node_type,
            "platform": platform,
            "kind": kind,
            "created_at": _utc_iso(created_dt),
            "hour": hour,
            "storyline_id": storyline_id,
            "reach": _compute_reach(item),
            "is_official": is_official,
            "self_reported": self_reported,
            "conflict": conflict,
            "lang": lang,
            "label": _truncate_text(author_label, MAX_EXCERPT_LEN),
        }
        prepared_items[item_id] = {
            **item,
            "_created_dt": created_dt,
            "_hour": hour,
            "_storyline_id": storyline_id,
            "_enrichment": enr,
        }
        node_info[item_id] = (hour, created_dt, node_obj)

    start_hour = -int(cfg.window.pre_hours)
    end_hour = _determine_end_hour(cfg, [info[0] for info in node_info.values()], now_dt)

    # Filter out items outside the window [start_hour, end_hour]
    in_window_ids = {nid for nid, (h, _, _) in node_info.items() if start_hour <= h <= end_hour}
    node_info = {nid: info for nid, info in node_info.items() if nid in in_window_ids}
    prepared_items = {nid: it for nid, it in prepared_items.items() if nid in in_window_ids}

    url_map = _build_url_to_item_lookup(prepared_items)
    candidate_edges: list[tuple[str, str, str]] = []

    for item_id, item in prepared_items.items():
        parent_id = str(item.get("parent_id") or "").strip()
        if parent_id and parent_id in node_info and parent_id != item_id:
            candidate_edges.append((item_id, parent_id, "replied_to"))

        quote_of = str(item.get("quote_of") or "").strip()
        if quote_of and quote_of in node_info and quote_of != item_id:
            candidate_edges.append((item_id, quote_of, "quoted"))

        for target_id in _extract_linked_item_ids(item, prepared_items, url_map):
            if target_id in node_info:
                candidate_edges.append((item_id, target_id, "links_to"))

        enr = item.get("_enrichment")
        item_text = str(item.get("text") or "")
        for claim_entry in (enr.get("claims") or []) if isinstance(enr, Mapping) else []:
            if not isinstance(claim_entry, Mapping):
                continue
            cid = str(claim_entry.get("claim_id") or "")
            stance = str(claim_entry.get("stance") or "")
            span = claim_entry.get("evidence_span")
            if cid not in valid_claims or stance not in {"supports", "contradicts"}:
                continue
            # P1.2: verify exact substring of item text
            if not isinstance(span, str) or not span or span not in item_text:
                continue
            # If claim node isn't in node_info yet, register it at its first evidence item's timestamp
            if cid not in node_info:
                claim_cfg = next(c for c in cfg.claims_to_check if c.id == cid)
                c_dt = item["_created_dt"]
                c_hour = item["_hour"]
                c_sr = claim_self_reported.get(cid, False)
                claim_node: dict[str, Any] = {
                    "id": cid,
                    "type": "claim",
                    "platform": "manual",
                    "kind": "post",
                    "created_at": _utc_iso(c_dt),
                    "hour": c_hour,
                    "storyline_id": item["_storyline_id"],
                    "reach": 1,
                    "is_official": c_sr,
                    "self_reported": c_sr,
                    "conflict": None,
                    "lang": "en",
                    "label": _truncate_text(claim_cfg.text, MAX_EXCERPT_LEN),
                }
                node_info[cid] = (c_hour, c_dt, claim_node)
            else:
                # Ensure claim node appears at the earliest evidence item's hour
                old_h, old_dt, old_node = node_info[cid]
                if item["_created_dt"] < old_dt:
                    updated_node = {
                        **old_node,
                        "created_at": _utc_iso(item["_created_dt"]),
                        "hour": item["_hour"],
                    }
                    node_info[cid] = (item["_hour"], item["_created_dt"], updated_node)
            candidate_edges.append((item_id, cid, stance))

    nodes_by_hour: dict[int, list[dict[str, Any]]] = {h: [] for h in range(start_hour, end_hour + 1)}
    for _, (h, _, node_obj) in sorted(node_info.items(), key=lambda kv: (kv[1][1], kv[0])):
        if h in nodes_by_hour:
            nodes_by_hour[h].append(node_obj)

    edges_by_hour: dict[int, list[dict[str, Any]]] = {h: [] for h in range(start_hour, end_hour + 1)}
    seen_edges: set[tuple[str, str, str]] = set()
    for src_id, dst_id, edge_type in candidate_edges:
        if (src_id, dst_id, edge_type) in seen_edges:
            continue
        if src_id not in node_info or dst_id not in node_info:
            continue
        seen_edges.add((src_id, dst_id, edge_type))
        src_h, src_dt, _ = node_info[src_id]
        dst_h, dst_dt, _ = node_info[dst_id]
        edge_hour = max(src_h, dst_h)
        edge_dt = max(src_dt, dst_dt)
        if edge_hour in edges_by_hour:
            edges_by_hour[edge_hour].append(
                {
                    "source": src_id,
                    "target": dst_id,
                    "type": edge_type,
                    "created_at": _utc_iso(edge_dt),
                    "hour": edge_hour,
                }
            )

    cumulative_counts: dict[str, int] = {s.id: 0 for s in cfg.seed_storylines}
    cumulative_counts["unassigned"] = 0

    frames: list[dict[str, Any]] = []
    for h in range(start_hour, end_hour + 1):
        for node_obj in nodes_by_hour[h]:
            if node_obj["type"] in {"post", "article"}:
                sid = str(node_obj.get("storyline_id") or "unassigned")
                if sid not in cumulative_counts:
                    sid = "unassigned"
                cumulative_counts[sid] += 1

        frames.append(
            {
                "event_id": cfg.event_id,
                "hour": h,
                "built_at": built_at_iso,
                "nodes_added": list(nodes_by_hour[h]),
                "edges_added": list(edges_by_hour[h]),
                "storyline_counts": dict(cumulative_counts),
            }
        )

    return frames


def compute_neutral_signals(
    items: Sequence[Mapping[str, Any]],
    enrichments: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Detect near-identical text bursts within a short window with neutral P4.3 wording and zero account IDs."""
    enrich_by_item = _latest_enrichments_by_item(enrichments)
    by_cluster: dict[tuple[str, str], list[datetime]] = {}

    for raw in items:
        item = _to_dict(raw)
        item_id = str(item.get("item_id") or "")
        text = str(item.get("text") or "")
        created_dt = _parse_ts(item.get("created_at"))
        if not item_id or not text.strip() or created_dt is None:
            continue
        norm = _normalize_text_for_cluster(text)
        if len(norm) < 20:
            continue
        enr = enrich_by_item.get(item_id)
        storyline_id = str(enr.get("storyline_id") or "unassigned") if enr else "unassigned"
        by_cluster.setdefault((storyline_id, norm), []).append(created_dt)

    signals: list[dict[str, Any]] = []
    for (storyline_id, _), timestamps in sorted(by_cluster.items()):
        if len(timestamps) < SIGNAL_MIN_ITEMS:
            continue
        ordered = sorted(timestamps)
        best_count = 0
        best_minutes = 0

        left = 0
        for right in range(len(ordered)):
            while (ordered[right] - ordered[left]).total_seconds() > SIGNAL_MAX_WINDOW_MINUTES * 60:
                left += 1
            count = right - left + 1
            if count >= SIGNAL_MIN_ITEMS and count > best_count:
                best_count = count
                span_seconds = max(0.0, (ordered[right] - ordered[left]).total_seconds())
                best_minutes = max(1, math.ceil(span_seconds / 60.0))

        if best_count >= SIGNAL_MIN_ITEMS:
            signals.append(
                {
                    "storyline_id": storyline_id,
                    "n_items": best_count,
                    "window_minutes": best_minutes,
                    "text": NEUTRAL_SIGNAL_TEMPLATE.format(n=best_count, m=best_minutes),
                }
            )
    return signals


def _classify_source_type(
    is_official: bool,
    conflict: dict[str, str] | None,
    platform: str,
) -> str:
    if conflict is not None:
        return "investor_with_conflict"
    if is_official:
        return "company_statement"
    if platform == "news":
        return "press"
    return "social"


def compute_claim_cards(
    cfg: EventConfig,
    items: Sequence[Mapping[str, Any]],
    enrichments: Sequence[Mapping[str, Any]],
    *,
    at_hour: int | None = None,
) -> list[dict[str, Any]]:
    """Compute Claim cards for every configured claim_to_check following P1, P2, P3, and api.md."""
    t0_dt = cfg.t0.utc
    enrich_by_item = _latest_enrichments_by_item(enrichments)
    item_dicts = [_to_dict(it) for it in items]

    items_by_id: dict[str, dict[str, Any]] = {}
    for item in item_dicts:
        item_id = str(item.get("item_id") or "")
        created_dt = _parse_ts(item.get("created_at")) or _parse_ts(item.get("collected_at"))
        if not item_id or created_dt is None:
            continue
        hour = _compute_hour(created_dt, t0_dt)
        if at_hour is not None and hour > at_hour:
            continue
        items_by_id[item_id] = {**item, "_created_dt": created_dt, "_hour": hour}

    cards: list[dict[str, Any]] = []
    for claim in cfg.claims_to_check:
        supporting = 0
        contradicting = 0
        neutral = 0
        evidence: list[dict[str, Any]] = []
        storyline_votes: Counter[str] = Counter()
        has_official_source = False
        has_independent_confirm = False

        for item_id, item in sorted(
            items_by_id.items(),
            key=lambda kv: (kv[1]["_created_dt"], kv[0]),
        ):
            enr = enrich_by_item.get(item_id)
            if not enr:
                continue
            item_text = str(item.get("text") or "")
            raw_claims = enr.get("claims") or []
            if not isinstance(raw_claims, Sequence):
                continue

            for entry in raw_claims:
                if not isinstance(entry, Mapping):
                    continue
                if str(entry.get("claim_id") or "") != claim.id:
                    continue
                stance = str(entry.get("stance") or "")
                span = entry.get("evidence_span")
                if stance not in {"supports", "contradicts", "neutral"}:
                    continue
                # P1.2: exact substring verification in code
                if not isinstance(span, str) or not span or span not in item_text:
                    continue

                if stance == "supports":
                    supporting += 1
                elif stance == "contradicts":
                    contradicting += 1
                else:
                    neutral += 1

                is_official = _is_item_official(cfg, item, enr)
                conflict = _lookup_conflict(cfg, item, enr)
                platform = str(item.get("platform") or "x")
                source_type = _classify_source_type(is_official, conflict, platform)

                if is_official and stance in {"supports", "contradicts"}:
                    has_official_source = True
                if not is_official and conflict is None and stance == "supports":
                    has_independent_confirm = True

                sid = str(enr.get("storyline_id") or "unassigned")
                if sid != "unassigned":
                    storyline_votes[sid] += 1

                evidence.append(
                    {
                        "item_id": item_id,
                        "url": str(item.get("url") or ""),
                        "platform": platform,
                        "source_type": source_type,
                        "stance": stance,
                        "span": span,
                        "created_at": _utc_iso(item["_created_dt"]),
                        "conflict": conflict,
                    }
                )

        if supporting > 0 and contradicting == 0:
            status = "supported"
        elif contradicting > 0 and supporting == 0:
            status = "contradicted"
        elif supporting > 0 and contradicting > 0:
            status = "mixed"
        else:
            status = "unverified"

        # P2.1 / P2.2: self_reported is True when supported/stated by official source without independent confirmation
        self_reported = bool(has_official_source and not has_independent_confirm)
        top_storyline = storyline_votes.most_common(1)[0][0] if storyline_votes else None

        cards.append(
            {
                "claim_id": claim.id,
                "text": claim.text,
                "status": status,
                "counts": {
                    "supporting": supporting,
                    "contradicting": contradicting,
                    "neutral": neutral,
                },
                "self_reported": self_reported,
                "self_reported_note": SELF_REPORTED_NOTE if self_reported else None,
                "evidence": evidence,
                "storyline_id": top_storyline,
                "deep_link": f"/e/{cfg.event_id}?claim={claim.id}",
            }
        )

    return cards


def _classify_before_after_pattern(
    vol_before: float,
    vol_after: float,
    addressed: bool,
    has_official_origin: bool,
) -> str:
    if has_official_origin and addressed:
        return "Source of claims"
    if not addressed:
        return "Not addressed"
    if vol_before == 0.0 and vol_after == 0.0:
        return "unchanged"
    if vol_after <= vol_before * 0.6:
        return "Faded"
    return "Sustained"


def compute_before_after(
    cfg: EventConfig,
    items: Sequence[Mapping[str, Any]],
    enrichments: Sequence[Mapping[str, Any]],
    *,
    turning_point_id: str | None = None,
) -> dict[str, Any]:
    """Compute descriptive before/after comparison around a configured turning point (P8.1, P8.2)."""
    manual_tps = list(cfg.turning_points.manual)
    chosen_tp = None
    if turning_point_id:
        for tp in manual_tps:
            if tp.id == turning_point_id:
                chosen_tp = tp
                break
        if chosen_tp is None:
            raise ValueError(f"Turning point {turning_point_id!r} not found")
    elif manual_tps:
        chosen_tp = manual_tps[0]

    if chosen_tp is not None:
        tp_id = chosen_tp.id
        tp_label = chosen_tp.label
        tp_dt = chosen_tp.at_utc
        tp_confirmed = bool(chosen_tp.confirmed)
        tp_form = chosen_tp.form
    else:
        tp_id = "tp-initial"
        tp_label = "Initial event marker"
        tp_dt = None
        tp_confirmed = False
        tp_form = None

    t0_dt = cfg.t0.utc
    split_dt = tp_dt if tp_dt is not None else datetime.fromtimestamp(t0_dt.timestamp() + 24 * 3600, tz=timezone.utc)

    enrich_by_item = _latest_enrichments_by_item(enrichments)
    item_dicts = [_to_dict(it) for it in items]

    before_counts: Counter[str] = Counter()
    after_counts: Counter[str] = Counter()
    addressed_storylines: set[str] = set()
    official_origin_storylines: set[str] = set()
    all_dts: list[datetime] = []

    for item in item_dicts:
        item_id = str(item.get("item_id") or "")
        created_dt = _parse_ts(item.get("created_at"))
        if not item_id or created_dt is None:
            continue
        all_dts.append(created_dt)
        enr = enrich_by_item.get(item_id)
        sid = str(enr.get("storyline_id") or "unassigned") if enr else "unassigned"
        if created_dt < split_dt:
            before_counts[sid] += 1
        else:
            after_counts[sid] += 1
        if _is_item_official(cfg, item, enr):
            if created_dt >= split_dt:
                addressed_storylines.add(sid)
            if _compute_hour(created_dt, t0_dt) <= 1:
                official_origin_storylines.add(sid)

    if all_dts:
        min_dt = min(min(all_dts), t0_dt)
        max_dt = max(max(all_dts), split_dt)
        hours_before = max(1.0, (split_dt - min_dt).total_seconds() / 3600.0)
        hours_after = max(1.0, (max_dt - split_dt).total_seconds() / 3600.0)
    else:
        hours_before = 1.0
        hours_after = 1.0

    storyline_rows: list[dict[str, Any]] = []
    for storyline in cfg.seed_storylines:
        sid = storyline.id
        b_count = before_counts.get(sid, 0)
        a_count = after_counts.get(sid, 0)
        vol_before = round(b_count / hours_before, 2) if all_dts else 0.0
        vol_after = round(a_count / hours_after, 2) if all_dts else 0.0
        addressed = sid in addressed_storylines
        pattern = (
            _classify_before_after_pattern(
                vol_before,
                vol_after,
                addressed,
                sid in official_origin_storylines,
            )
            if all_dts
            else "unchanged"
        )
        storyline_rows.append(
            {
                "storyline_id": sid,
                "name": storyline.name,
                "volume_before_per_h": vol_before,
                "volume_after_per_h": vol_after,
                "pattern": pattern,
                "addressed": addressed,
            }
        )

    return {
        "turning_point": {
            "id": tp_id,
            "label": tp_label,
            "at_utc": _utc_iso(tp_dt) if tp_dt is not None else None,
            "confirmed": tp_confirmed,
            "form": tp_form,
        },
        "confirmed": tp_confirmed,
        "form": tp_form,
        "note": DESCRIPTIVE_NOTE,
        "storylines": storyline_rows,
    }


def compute_storylines(
    cfg: EventConfig,
    items: Sequence[Mapping[str, Any]],
    enrichments: Sequence[Mapping[str, Any]],
    account_map: Mapping[tuple[str, str], int] | Sequence[Mapping[str, Any]] | None = None,
    *,
    at_hour: int | None = None,
) -> list[dict[str, Any]]:
    """Compute per-storyline facts (first_seen, platforms_reached, stance_mix, agreement, top_amplifiers, volume_by_hour, jumps)."""
    t0_dt = cfg.t0.utc
    item_dicts = [_to_dict(it) for it in items]
    enrich_by_item = _latest_enrichments_by_item(enrichments)
    account_lookup = _coerce_account_lookup(account_map, item_dicts)
    valid_storylines = {s.id for s in cfg.seed_storylines}

    filtered_items: dict[str, dict[str, Any]] = {}
    for item in item_dicts:
        item_id = str(item.get("item_id") or "")
        created_dt = _parse_ts(item.get("created_at")) or _parse_ts(item.get("collected_at"))
        if not item_id or created_dt is None:
            continue
        hour = _compute_hour(created_dt, t0_dt)
        if at_hour is not None and hour > at_hour:
            continue
        enr = enrich_by_item.get(item_id)
        raw_sid = str(enr.get("storyline_id") or "unassigned") if enr else "unassigned"
        if enr and bool(enr.get("off_topic")):
            sid = "unassigned"
        elif raw_sid in valid_storylines:
            sid = raw_sid
        else:
            sid = "unassigned"
        filtered_items[item_id] = {
            **item,
            "_created_dt": created_dt,
            "_hour": hour,
            "_storyline_id": sid,
            "_enrichment": enr,
        }

    url_map = _build_url_to_item_lookup(filtered_items)
    ordered_defs = [(s.id, s.name, idx) for idx, s in enumerate(cfg.seed_storylines)] + [
        ("unassigned", "Unassigned", -1)
    ]

    results: list[dict[str, Any]] = []
    for sid, name, colour_index in ordered_defs:
        group = sorted(
            (it for it in filtered_items.values() if it["_storyline_id"] == sid),
            key=lambda it: (it["_created_dt"], str(it["item_id"])),
        )
        if not group:
            results.append(
                {
                    "id": sid,
                    "name": name,
                    "colour_index": colour_index,
                    "count": 0,
                    "first_seen": None,
                    "platforms_reached": [],
                    "stance_mix": None,
                    "agreement": None,
                    "top_amplifiers": [],
                    "volume_by_hour": [],
                    "jumps": [],
                }
            )
            continue

        first_it = group[0]
        first_seen = {
            "item_id": str(first_it["item_id"]),
            "platform": str(first_it.get("platform") or "x"),
            "hour": int(first_it["_hour"]),
        }

        platform_first: dict[str, int] = {}
        vol_counter: Counter[int] = Counter()
        stance_counts = {"supports": 0, "contradicts": 0, "neutral": 0}
        agreements: list[float] = []
        amp_reach: dict[str, int] = {}
        jumps: list[dict[str, Any]] = []

        for it in group:
            item_id = str(it["item_id"])
            plat = str(it.get("platform") or "x")
            h = int(it["_hour"])
            if plat not in platform_first:
                platform_first[plat] = h
            vol_counter[h] += 1

            label, _ = _resolve_author_label(cfg, it, account_lookup)
            reach = _compute_reach(it)
            amp_reach[label] = max(amp_reach.get(label, 0), reach)

            enr = it.get("_enrichment")
            for c_entry in (enr.get("claims") or []) if isinstance(enr, Mapping) else []:
                if not isinstance(c_entry, Mapping):
                    continue
                st = str(c_entry.get("stance") or "")
                if st in stance_counts:
                    stance_counts[st] += 1
                agr = c_entry.get("agreement")
                if isinstance(agr, (int, float)):
                    agreements.append(float(agr))

            # Explicit cross-platform jumps
            targets: list[str] = []
            for ref_key in ("parent_id", "quote_of"):
                ref_val = str(it.get(ref_key) or "").strip()
                if ref_val and ref_val in filtered_items:
                    targets.append(ref_val)
            targets.extend(_extract_linked_item_ids(it, filtered_items, url_map))

            for target_id in dict.fromkeys(targets):
                target_it = filtered_items.get(target_id)
                if target_it is None:
                    continue
                if str(target_it.get("platform") or "") != plat:
                    earlier, later = (
                        (target_it, it)
                        if target_it["_created_dt"] <= it["_created_dt"]
                        else (it, target_it)
                    )
                    jumps.append(
                        {
                            "from_item": str(earlier["item_id"]),
                            "to_item": str(later["item_id"]),
                            "hour": int(later["_hour"]),
                        }
                    )

        platforms_reached = [
            {"platform": p, "hour": h}
            for p, h in sorted(platform_first.items(), key=lambda kv: (kv[1], kv[0]))
        ]
        top_amplifiers = [
            {"label": lbl, "reach": r}
            for lbl, r in sorted(amp_reach.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        ]
        volume_by_hour = [
            {"hour": h, "count": cnt} for h, cnt in sorted(vol_counter.items())
        ]
        stance_mix = stance_counts if any(stance_counts.values()) else None
        avg_agreement = round(sum(agreements) / len(agreements), 2) if agreements else None

        results.append(
            {
                "id": sid,
                "name": name,
                "colour_index": colour_index,
                "count": len(group),
                "first_seen": first_seen,
                "platforms_reached": platforms_reached,
                "stance_mix": stance_mix,
                "agreement": avg_agreement,
                "top_amplifiers": top_amplifiers,
                "volume_by_hour": volume_by_hour,
                "jumps": jumps,
            }
        )

    return results


def compute_trust(
    cfg: EventConfig,
    items: Sequence[Mapping[str, Any]],
    enrichments: Sequence[Mapping[str, Any]],
    *,
    gold: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute trust strip metrics (coverage per enabled source, gold, unassigned_count, backfilled_share)."""
    item_dicts = [_to_dict(it) for it in items]
    enrich_by_item = _latest_enrichments_by_item(enrichments)
    valid_storylines = {s.id for s in cfg.seed_storylines}

    platform_counts: Counter[str] = Counter()
    backfilled_count = 0
    unassigned_count = 0

    for item in item_dicts:
        item_id = str(item.get("item_id") or "")
        plat = str(item.get("platform") or "")
        if plat:
            platform_counts[plat] += 1
        if str(item.get("captured_by") or "") == "backfill":
            backfilled_count += 1
        enr = enrich_by_item.get(item_id)
        sid = str(enr.get("storyline_id") or "unassigned") if enr else "unassigned"
        if sid not in valid_storylines or (enr and bool(enr.get("off_topic"))):
            unassigned_count += 1

    sources_dump = cfg.sources.model_dump()
    coverage: dict[str, dict[str, Any]] = {}
    for src_name, src_conf in sources_dump.items():
        if isinstance(src_conf, Mapping) and src_conf.get("enabled") is True:
            entry: dict[str, Any] = {"count": platform_counts.get(src_name, 0)}
            if src_conf.get("max_posts") is not None:
                entry["cap"] = int(src_conf["max_posts"])
            elif src_conf.get("max_videos") is not None:
                entry["cap"] = int(src_conf["max_videos"])
            coverage[src_name] = entry

    total_items = len(item_dicts)
    backfilled_share = round(backfilled_count / total_items, 4) if total_items > 0 else None

    return {
        "coverage": coverage,
        "gold": gold,
        "unassigned_count": unassigned_count,
        "backfilled_share": backfilled_share,
    }


def _compute_public_items(
    cfg: EventConfig,
    items: Sequence[Mapping[str, Any]],
    enrichments: Sequence[Mapping[str, Any]],
    account_map: Mapping[tuple[str, str], int] | Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Precompute sanitized item detail responses (excerpt <=300 chars, never author_ref)."""
    item_dicts = [_to_dict(it) for it in items]
    enrich_by_item = _latest_enrichments_by_item(enrichments)
    account_lookup = _coerce_account_lookup(account_map, item_dicts)
    valid_storylines = {s.id for s in cfg.seed_storylines}

    public_items: dict[str, dict[str, Any]] = {}
    for item in item_dicts:
        item_id = str(item.get("item_id") or "")
        created_dt = _parse_ts(item.get("created_at"))
        collected_dt = _parse_ts(item.get("collected_at")) or created_dt
        if not item_id or created_dt is None or collected_dt is None:
            continue

        lag_s = max(0, int((collected_dt - created_dt).total_seconds()))
        enr = enrich_by_item.get(item_id)
        raw_sid = str(enr.get("storyline_id") or "unassigned") if enr else "unassigned"
        storyline_id = (
            "unassigned"
            if (enr and bool(enr.get("off_topic"))) or raw_sid not in valid_storylines
            else raw_sid
        )

        item_text = str(item.get("text") or "")
        verified_claims: list[dict[str, Any]] = []
        for c_entry in (enr.get("claims") or []) if isinstance(enr, Mapping) else []:
            if not isinstance(c_entry, Mapping):
                continue
            cid = str(c_entry.get("claim_id") or "")
            st = str(c_entry.get("stance") or "")
            sp = c_entry.get("evidence_span")
            if cid and st in {"supports", "contradicts", "neutral"} and isinstance(sp, str) and sp in item_text:
                verified_claims.append({"claim_id": cid, "stance": st, "span": sp})

        is_official = _is_item_official(cfg, item, enr)
        conflict = _lookup_conflict(cfg, item, enr)
        lang = str((enr.get("lang") if enr else None) or item.get("lang") or "und")
        label, _ = _resolve_author_label(cfg, item, account_lookup)

        tags: list[str] = [str(item.get("platform") or "x"), f"lang:{lang}"]
        if is_official:
            tags.append("self-reported")
        if conflict is not None:
            tags.append(f"conflict:{conflict['person']}")
        if bool(item.get("time_approximate")):
            tags.append("time approximate")

        public_items[item_id] = {
            "item_id": item_id,
            "created_at": _utc_iso(created_dt),
            "collected_at": _utc_iso(collected_dt),
            "collection_lag_s": lag_s,
            "platform": str(item.get("platform") or "x"),
            "url": str(item.get("url") or ""),
            "excerpt": _truncate_text(item_text, MAX_EXCERPT_LEN),
            "storyline_id": storyline_id,
            "claims": verified_claims,
            "reach": _compute_reach(item),
            "label": label,
            "is_official": is_official,
            "self_reported": is_official,
            "conflict": conflict,
            "tags": tags,
        }
    return public_items


def build_snapshot(
    cfg: EventConfig,
    items: Sequence[Mapping[str, Any]],
    enrichments: Sequence[Mapping[str, Any]],
    account_map: Mapping[tuple[str, str], int] | Sequence[Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build complete derived snapshot JSON without private fields."""
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    public_items_by_id = _compute_public_items(cfg, items, enrichments, account_map)
    return {
        "event_id": cfg.event_id,
        "built_at": _utc_iso(now_dt),
        "storylines": compute_storylines(cfg, items, enrichments, account_map),
        "claims": compute_claim_cards(cfg, items, enrichments),
        "signals": compute_neutral_signals(items, enrichments),
        "before_after": compute_before_after(cfg, items, enrichments),
        "trust": compute_trust(cfg, items, enrichments),
        "items": list(public_items_by_id.values()),
    }


def _table_ref(bq_client: Any, settings: FrameBuilderSettings, table: str) -> str:
    project = settings.gcp_project or getattr(bq_client, "project", "") or ""
    if isinstance(project, str) and project.strip():
        return f"{project.strip()}.{settings.bq_dataset}.{table}"
    return f"{settings.bq_dataset}.{table}"


def _fetch_bq_inputs(
    bq_client: Any,
    settings: FrameBuilderSettings,
    event_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch items, enrichments, and account_map for event_id with explicit column lists (S4.5)."""
    param = [bigquery.ScalarQueryParameter("event_id", "STRING", event_id)]
    job_cfg = bigquery.QueryJobConfig(query_parameters=param)

    items_tbl = _table_ref(bq_client, settings, "items")
    items_sql = (
        "SELECT item_id, event_id, platform, kind, url, author_ref, author_followers, "
        "created_at, collected_at, text, lang, parent_id, quote_of, links, engagement, "
        "hours_from_t0, captured_by, time_approximate "
        f"FROM `{items_tbl}` WHERE event_id = @event_id ORDER BY created_at ASC"
    )
    items = [dict(row) for row in bq_client.query(items_sql, job_config=job_cfg).result()]

    enrich_tbl = _table_ref(bq_client, settings, "enrichments")
    enrich_sql = (
        "SELECT item_id, event_id, enriched_at, model_id, embedding_model_id, prompt_version, "
        "lang, storyline_id, storyline_similarity, off_topic, claims, entities, "
        "is_official, conflict_person "
        f"FROM `{enrich_tbl}` WHERE event_id = @event_id ORDER BY enriched_at ASC"
    )
    enrichments = [dict(row) for row in bq_client.query(enrich_sql, job_config=job_cfg).result()]

    acct_tbl = _table_ref(bq_client, settings, "account_map")
    acct_sql = (
        "SELECT event_id, platform, author_ref, account_n, first_seen "
        f"FROM `{acct_tbl}` WHERE event_id = @event_id ORDER BY account_n ASC"
    )
    account_map = [dict(row) for row in bq_client.query(acct_sql, job_config=job_cfg).result()]

    return items, enrichments, account_map


def run_frame_builder(
    *,
    event_path: str | Path,
    input_json: str | Path | None = None,
    output_dir: str | Path | None = None,
    dry_run: bool = False,
    bq_client: Any | None = None,
    settings: FrameBuilderSettings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run frame precomputation from local JSON or BigQuery and write outputs."""
    cfg = load_event(event_path)
    fb_settings = settings or FrameBuilderSettings()
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if input_json is not None:
        bundle = json.loads(Path(input_json).read_text(encoding="utf-8"))
        items = list(bundle.get("items") or [])
        enrichments = list(bundle.get("enrichments") or [])
        account_map = list(bundle.get("account_map") or [])
    else:
        bq = bq_client if bq_client is not None else bigquery.Client(project=fb_settings.gcp_project or None)
        items, enrichments, account_map = _fetch_bq_inputs(bq, fb_settings, cfg.event_id)

    frames = build_frames(cfg, items, enrichments, account_map, now=now_dt)
    snapshot = build_snapshot(cfg, items, enrichments, account_map, now=now_dt)

    if output_dir is not None:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        (out_path / f"{cfg.event_id}.frames.json").write_text(
            json.dumps(frames, indent=2), encoding="utf-8"
        )
        (out_path / f"{cfg.event_id}.snapshot.json").write_text(
            json.dumps(snapshot, indent=2), encoding="utf-8"
        )

    written_to_bq = 0
    if not dry_run:
        bq = bq_client if bq_client is not None else bigquery.Client(project=fb_settings.gcp_project or None)
        res = write_rows("frames", frames, client=bq)
        written_to_bq = res.written

    summary = {
        "event_id": cfg.event_id,
        "frames_count": len(frames),
        "items_count": len(items),
        "written_to_bq": written_to_bq,
        "dry_run": dry_run,
    }
    _log_json("frame_builder_run", **summary)
    return summary


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VeriFact frame-builder Cloud Run Job")
    parser.add_argument("--event", required=True, help="Path to event YAML config")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to BigQuery")
    parser.add_argument("--output-dir", default=None, help="Directory to write <event_id>.frames.json and .snapshot.json")
    parser.add_argument("--input-json", default=None, help="Path to local JSON bundle of items/enrichments/account_map")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    args = _build_arg_parser().parse_args(argv)
    try:
        run_frame_builder(
            event_path=args.event,
            input_json=args.input_json,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        _log_json("frame_builder_error", level=logging.ERROR, error=str(exc))
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
