"""Read-only data store backends for the VeriFact API service.

Provides ``DataStore`` with two concrete backends:
- ``FramesStore``: reads ``FRAMES_DIR`` local JSON snapshots or queries
  BigQuery (``frames``, ``items``, ``enrichments``, ``account_map``) with
  explicit column lists and parameterized ``event_id`` filters (S4.5), falling
  back to empty config-driven structures when no data is collected yet (P7.4).
- ``SpannerStore``: delegates live reads to
  ``verifact_core.spanner_graph.query_graph_at_hour`` when
  ``GRAPH_BACKEND=spanner`` and ``SPANNER_LIVE=true``, returning identical
  response shapes.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Any, Callable, Protocol

logger = logging.getLogger("verifact.api.store")

DESCRIPTIVE_COMPARISON_NOTE = "Descriptive comparison, not a causal estimate."
SELF_REPORTED_NOTE = "Company statement, not independently confirmed"
MAX_EXCERPT_CHARS = 300
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_URL_RE = re.compile(r"https?://\S+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")
_NODE_KEYS = (
    "id", "type", "platform", "kind", "created_at", "hour", "storyline_id",
    "reach", "is_official", "self_reported", "conflict", "lang", "label", "is_response",
)
_EDGE_KEYS = ("source", "target", "type", "created_at", "hour")


# ---------------------------------------------------------------------------
# Pure Timestamp, Privacy & Validation Utilities
# ---------------------------------------------------------------------------


def parse_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 string or datetime into a UTC-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def format_utc(value: Any) -> str:
    """Format a datetime or ISO string as UTC ISO-8601 ending in Z."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if value is None:
        return ""
    text = str(value).strip()
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def extract_t0_utc(event_config: dict[str, Any]) -> str:
    """Extract canonical T0 UTC string from an event config dict."""
    t0 = event_config.get("t0", {})
    return format_utc(t0.get("utc") if isinstance(t0, dict) else t0)


def compute_now_hour(t0_utc_str: str) -> int:
    """Compute whole hours elapsed since T0 in UTC."""
    t0_dt = parse_utc(t0_utc_str)
    return int((datetime.now(timezone.utc) - t0_dt).total_seconds() // 3600) if t0_dt else 0


def make_excerpt(text: str | None, max_len: int = MAX_EXCERPT_CHARS) -> str:
    """Return a short excerpt strictly capped at max_len characters (P5.5)."""
    cleaned = str(text or "").strip()
    return cleaned if len(cleaned) <= max_len else cleaned[: max_len - 3].rstrip() + "..."


def _compute_item_hour(item: dict[str, Any], t0_dt: datetime | None) -> int:
    if item.get("hour") is not None:
        try:
            return int(item["hour"])
        except (TypeError, ValueError):
            pass
    if item.get("hours_from_t0") is not None:
        try:
            return math.floor(float(item["hours_from_t0"]))
        except (TypeError, ValueError):
            pass
    created_dt = parse_utc(item.get("created_at"))
    if created_dt is not None and t0_dt is not None:
        return math.floor((created_dt - t0_dt).total_seconds() / 3600.0)
    return 0


def _compute_reach(item: dict[str, Any]) -> int:
    if item.get("reach") is not None:
        try:
            return max(0, int(item["reach"]))
        except (TypeError, ValueError):
            pass
    followers = item.get("author_followers") or 0
    eng = item.get("engagement") or {}
    eng_total = sum(
        int(eng[k]) for k in ("likes", "reposts", "replies", "views", "score")
        if isinstance(eng, dict) and isinstance(eng.get(k), (int, float))
    )
    try:
        return max(0, int(followers), eng_total)
    except (TypeError, ValueError):
        return 0


def _resolve_conflict(event_config: dict[str, Any], person: str | None) -> dict[str, str] | None:
    """Resolve conflict disclosure strictly from event_config (P3.1)."""
    target = str(person or "").strip().lower()
    if not target:
        return None
    for entry in event_config.get("conflicts_of_interest") or []:
        if isinstance(entry, dict) and str(entry.get("person") or "").strip().lower() == target:
            return {
                "person": str(entry["person"]).strip(),
                "relationship": str(entry.get("relationship") or "").strip(),
            }
    return None


def _resolve_display_label(
    item: dict[str, Any],
    event_config: dict[str, Any],
    account_lookup: dict[tuple[str, str], int],
    node_label: str | None,
    is_official: bool,
) -> str:
    """Return a public name or pseudonym; never exposes raw author_ref (P5.2)."""
    author_ref = str(item.get("author_ref") or "").strip()
    if node_label and node_label != author_ref:
        return node_label
    explicit = str(item.get("label") or item.get("author_display") or "").strip()
    if explicit and explicit != author_ref:
        return explicit

    official_accounts = [str(a) for a in ((event_config.get("official") or {}).get("accounts") or []) if a]
    if author_ref:
        for acct in official_accounts:
            if acct.lower() == author_ref.lower():
                return acct

    platform = str(item.get("platform") or "").strip().lower()
    if author_ref and (platform, author_ref) in account_lookup:
        return f"Account A{account_lookup[(platform, author_ref)]}"
    if is_official and official_accounts:
        return official_accounts[0]
    return "News Outlet" if platform == "news" else "Account A0"


def _sanitize_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (format_utc(node[k]) if k == "created_at" else node[k])
        for k in _NODE_KEYS if k in node
    }


def _sanitize_edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (format_utc(edge[k]) if k == "created_at" else edge[k])
        for k in _EDGE_KEYS if k in edge
    }


def _normalize_frame(raw: dict[str, Any]) -> dict[str, Any]:
    nodes = json.loads(raw["nodes_added"]) if isinstance(raw.get("nodes_added"), str) else (raw.get("nodes_added") or [])
    edges = json.loads(raw["edges_added"]) if isinstance(raw.get("edges_added"), str) else (raw.get("edges_added") or [])
    counts = json.loads(raw["storyline_counts"]) if isinstance(raw.get("storyline_counts"), str) else (raw.get("storyline_counts") or {})
    return {
        "hour": int(raw.get("hour", 0)),
        "nodes_added": [_sanitize_node(n) for n in nodes if isinstance(n, dict)],
        "edges_added": [_sanitize_edge(e) for e in edges if isinstance(e, dict)],
        "storyline_counts": {str(k): int(v) for k, v in counts.items()},
    }


def _verify_span(text: str | None, span: str | None) -> bool:
    """Enforce P1.2: evidence_span must be a non-empty exact substring of text."""
    if not span or not isinstance(span, str):
        return False
    return True if not text else (span in text)


def _filter_verified_claims(claims: list[dict[str, Any]], text: str | None) -> list[dict[str, Any]]:
    """Drop SUPPORTS/CONTRADICTS claims whose span fails P1.2 substring verification."""
    verified: list[dict[str, Any]] = []
    for cl in claims:
        if not isinstance(cl, dict):
            continue
        stance = str(cl.get("stance") or "neutral").lower()
        span = str(cl.get("evidence_span") or cl.get("span") or "")
        if stance in ("supports", "contradicts") and not _verify_span(text, span):
            continue
        verified.append({
            "claim_id": str(cl.get("claim_id") or ""),
            "stance": stance,
            "evidence_span": span,
            "agreement": cl.get("agreement"),
        })
    return verified


# ---------------------------------------------------------------------------
# Internal Normalized Dataset & Shared Derivations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventDataset:
    """Immutable normalized view of collected event data shared across backends."""

    frames: list[dict[str, Any]] = field(default_factory=list)
    items_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)
    precomputed_signals: list[dict[str, Any]] | None = None
    gold: dict[str, Any] | None = None
    precomputed_brief: dict[str, Any] | None = None
    precomputed_trust: dict[str, Any] | None = None

    def fingerprint(self) -> str:
        raw = json.dumps(
            {"f": len(self.frames), "i": sorted(self.items_by_id), "e": len(self.edges)},
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]


def _seed_storyline_meta(event_config: dict[str, Any]) -> list[tuple[str, str, int]]:
    seeds = event_config.get("seed_storylines") or []
    return [
        (str(e.get("id") or f"storyline-{i}"), str(e.get("name") or e.get("id") or f"Storyline {i}"), i)
        for i, e in enumerate(seeds) if isinstance(e, dict)
    ]


def _items_up_to_hour(dataset: EventDataset, at_hour: int | None) -> list[dict[str, Any]]:
    items = [
        it for it in dataset.items_by_id.values()
        if at_hour is None or int(it.get("hour", 0)) <= at_hour
    ]
    items.sort(key=lambda it: (it.get("created_at") or "", it.get("item_id") or ""))
    return items


def _build_storyline_row(
    storyline_id: str,
    name: str,
    colour_index: int,
    storyline_items: list[dict[str, Any]],
) -> dict[str, Any]:
    if not storyline_items:
        return {
            "id": storyline_id, "name": name, "colour_index": colour_index, "count": 0,
            "first_seen": None, "platforms_reached": [], "stance_mix": None,
            "agreement": None, "top_amplifiers": [],
        }

    ordered = sorted(
        storyline_items,
        key=lambda it: (it.get("created_at") or "", int(it.get("hour", 0)), it.get("item_id") or ""),
    )
    first = ordered[0]
    plat_earliest: dict[str, int] = {}
    stance_counts = {"supports": 0, "contradicts": 0, "neutral": 0}
    agreements: list[float] = []
    amp_reach: dict[str, int] = {}

    for it in ordered:
        plat, hr = str(it.get("platform") or ""), int(it.get("hour", 0))
        if plat and (plat not in plat_earliest or hr < plat_earliest[plat]):
            plat_earliest[plat] = hr
        for cl in it.get("claims") or []:
            st = str(cl.get("stance") or "").lower()
            if st in stance_counts:
                stance_counts[st] += 1
            if isinstance(cl.get("agreement"), (int, float)):
                agreements.append(float(cl["agreement"]))
        lbl, rch = str(it.get("label") or "").strip(), int(it.get("reach") or 0)
        if lbl and rch > 0:
            amp_reach[lbl] = max(amp_reach.get(lbl, 0), rch)

    has_claims = sum(stance_counts.values()) > 0
    return {
        "id": storyline_id,
        "name": name,
        "colour_index": colour_index,
        "count": len(ordered),
        "first_seen": {"item_id": first["item_id"], "platform": first["platform"], "hour": int(first["hour"])},
        "platforms_reached": [
            {"platform": p, "hour": h} for p, h in sorted(plat_earliest.items(), key=lambda x: (x[1], x[0]))
        ],
        "stance_mix": stance_counts if has_claims else None,
        "agreement": round(sum(agreements) / len(agreements), 4) if agreements else None,
        "top_amplifiers": [
            {"label": lbl, "reach": rch}
            for lbl, rch in sorted(amp_reach.items(), key=lambda x: (-x[1], x[0]))[:5]
        ],
    }


def derive_frames(
    dataset: EventDataset,
    from_hour: int | None = None,
    to_hour: int | None = None,
) -> list[dict[str, Any]]:
    """Filter normalized frames cleanly by optional [from_hour, to_hour] bounds."""
    return [
        f for f in sorted(dataset.frames, key=lambda x: int(x["hour"]))
        if (from_hour is None or int(f["hour"]) >= from_hour)
        and (to_hour is None or int(f["hour"]) <= to_hour)
    ]


def derive_storylines(
    dataset: EventDataset,
    event_config: dict[str, Any],
    at_hour: int | None = None,
) -> list[dict[str, Any]]:
    """Compute storyline summary rows up to at_hour, including unassigned."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in _items_up_to_hour(dataset, at_hour):
        grouped[str(it.get("storyline_id") or "unassigned")].append(it)

    rows: list[dict[str, Any]] = []
    known: set[str] = set()
    for sid, name, idx in _seed_storyline_meta(event_config):
        known.add(sid)
        rows.append(_build_storyline_row(sid, name, idx, grouped.get(sid, [])))
    for sid in sorted(k for k in grouped if k != "unassigned" and k not in known):
        rows.append(_build_storyline_row(sid, sid, len(rows), grouped[sid]))
    rows.append(_build_storyline_row("unassigned", "Unassigned", -1, grouped.get("unassigned", [])))
    return rows


def derive_storyline_detail(
    dataset: EventDataset,
    event_config: dict[str, Any],
    storyline_id: str,
    at_hour: int | None = None,
) -> dict[str, Any] | None:
    """Compute detailed storyline row with volume_by_hour and cross-platform jumps."""
    match = next((r for r in derive_storylines(dataset, event_config, at_hour) if r["id"] == storyline_id), None)
    if match is None:
        return None

    s_items = [
        it for it in _items_up_to_hour(dataset, at_hour)
        if str(it.get("storyline_id") or "unassigned") == storyline_id
    ]
    s_ids = {it["item_id"] for it in s_items}
    vol_map: dict[int, int] = defaultdict(int)
    for it in s_items:
        vol_map[int(it.get("hour", 0))] += 1

    jumps: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for edge in dataset.edges:
        hr = int(edge.get("hour", 0))
        if (at_hour is not None and hr > at_hour) or edge.get("type") not in ("links_to", "quoted", "replied_to"):
            continue
        src, dst = str(edge.get("source") or ""), str(edge.get("target") or "")
        if src not in s_ids and dst not in s_ids:
            continue
        src_it, dst_it = dataset.items_by_id.get(src), dataset.items_by_id.get(dst)
        if src_it and dst_it and src_it.get("platform") != dst_it.get("platform"):
            key = (src, dst, hr)
            if key not in seen:
                seen.add(key)
                jumps.append({"from_item": src, "to_item": dst, "hour": hr})
    jumps.sort(key=lambda j: (j["hour"], j["from_item"], j["to_item"]))

    return {
        **match,
        "volume_by_hour": [{"hour": h, "count": c} for h, c in sorted(vol_map.items())],
        "jumps": jumps,
    }


def derive_item_detail(
    dataset: EventDataset,
    event_config: dict[str, Any],
    item_id: str,
) -> dict[str, Any] | None:
    """Return single-item inspector payload (<=300 char excerpt, never author_ref)."""
    item = dataset.items_by_id.get(item_id)
    if item is None:
        return None
    created_dt, collected_dt = parse_utc(item.get("created_at")), parse_utc(item.get("collected_at"))
    lag_s = max(0, int((collected_dt - created_dt).total_seconds())) if created_dt and collected_dt else None
    conflict = _resolve_conflict(event_config, item.get("conflict_person"))

    tags = [
        tag for flag, tag in (
            (item.get("is_official"), "official"),
            (item.get("self_reported"), "self-reported"),
            (item.get("time_approximate"), "time-approximate"),
            (item.get("captured_by") == "backfill", "backfill"),
            (conflict is not None, "conflict-disclosed"),
        ) if flag
    ]
    for extra_tag in item.get("tags") or []:
        t_s = str(extra_tag).strip()
        if t_s and t_s not in tags:
            tags.append(t_s)
    for ent in item.get("entities") or []:
        ent_s = str(ent).strip()
        if ent_s and ent_s not in tags:
            tags.append(ent_s)

    return {
        "item_id": item["item_id"],
        "created_at": format_utc(item.get("created_at")),
        "collected_at": format_utc(item.get("collected_at") or item.get("created_at")),
        "collection_lag_s": lag_s,
        "platform": str(item.get("platform") or ""),
        "url": item.get("url"),
        "excerpt": make_excerpt(item.get("text") or item.get("excerpt")),
        "storyline_id": item.get("storyline_id"),
        "claims": [
            {"claim_id": cl["claim_id"], "stance": cl["stance"], "span": cl["evidence_span"]}
            for cl in item.get("claims") or []
        ],
        "reach": item.get("reach"),
        "tags": tags,
        "is_official": bool(item.get("is_official", False)),
        "self_reported": bool(item.get("self_reported", False)),
        "conflict": conflict,
        "lang": item.get("lang"),
        "label": item.get("label"),
    }


def _classify_source_type(item: dict[str, Any], conflict: dict[str, str] | None) -> str:
    if conflict is not None:
        return "investor_with_conflict"
    if item.get("is_official") or item.get("platform") == "web":
        return "company_statement"
    return "press" if item.get("platform") == "news" else "social"


def derive_claim_card(
    dataset: EventDataset,
    event_id: str,
    event_config: dict[str, Any],
    claim_id: str,
    at_hour: int | None = None,
) -> dict[str, Any]:
    """Compute exact Claim card shape (P1.2/P1.3/P2.1-P2.3/P3.1-P3.2)."""
    claim_text, cfg_self_reported, storyline_id = f"[{claim_id}]", False, None
    for cfg_c in event_config.get("claims_to_check") or []:
        if isinstance(cfg_c, dict) and cfg_c.get("id") == claim_id:
            claim_text = str(cfg_c.get("text") or claim_text)
            cfg_self_reported = bool(cfg_c.get("self_reported", False))
            storyline_id = cfg_c.get("storyline_id")
            break

    evidence: list[dict[str, Any]] = []
    counts = {"supporting": 0, "contradicting": 0, "neutral": 0}
    sl_votes: dict[str, int] = defaultdict(int)

    for it in _items_up_to_hour(dataset, at_hour):
        conflict = _resolve_conflict(event_config, it.get("conflict_person"))
        source_type = _classify_source_type(it, conflict)
        for cl in it.get("claims") or []:
            if cl.get("claim_id") != claim_id:
                continue
            stance = cl["stance"]
            if stance == "supports":
                counts["supporting"] += 1
            elif stance == "contradicts":
                counts["contradicting"] += 1
            else:
                counts["neutral"] += 1
            if it.get("storyline_id") and it["storyline_id"] != "unassigned":
                sl_votes[str(it["storyline_id"])] += 1
            evidence.append({
                "item_id": it["item_id"],
                "url": it.get("url"),
                "platform": str(it.get("platform") or ""),
                "source_type": source_type,
                "stance": stance,
                "span": cl["evidence_span"],
                "created_at": format_utc(it.get("created_at")),
                "conflict": conflict,
            })

    if storyline_id is None and sl_votes:
        storyline_id = max(sl_votes.items(), key=lambda x: (x[1], x[0]))[0]

    sup, con = counts["supporting"], counts["contradicting"]
    if sup > 0 and con == 0:
        claim_status = "supported"
    elif con > 0 and sup == 0:
        claim_status = "contradicted"
    elif sup > 0 and con > 0:
        claim_status = "mixed"
    else:
        claim_status = "unverified"

    sup_ev = [e for e in evidence if e["stance"] == "supports"]
    has_indep = any(e["source_type"] not in ("company_statement", "investor_with_conflict") for e in sup_ev)
    has_official = any(e["source_type"] == "company_statement" for e in sup_ev)
    self_reported = False if has_indep else bool(cfg_self_reported or has_official)

    return {
        "claim_id": claim_id,
        "text": claim_text,
        "status": claim_status,
        "counts": counts,
        "self_reported": self_reported,
        "self_reported_note": SELF_REPORTED_NOTE if self_reported else None,
        "evidence": evidence,
        "storyline_id": storyline_id,
        "deep_link": f"/e/{event_id}?claim={claim_id}",
    }


def _normalize_for_cluster(text: str) -> tuple[str, set[str]]:
    tokens = [t for t in _NON_ALNUM_RE.sub(" ", _URL_RE.sub("", text.lower())).split() if t]
    return " ".join(tokens), set(tokens)


def derive_signals(dataset: EventDataset) -> list[dict[str, Any]]:
    """Compute neutral P4.3 timing signals with zero account identifiers."""
    if dataset.precomputed_signals is not None:
        return [
            {
                "storyline_id": str(s["storyline_id"]),
                "n_items": int(s["n_items"]),
                "window_minutes": int(s["window_minutes"]),
                "text": str(s["text"]),
            }
            for s in dataset.precomputed_signals
            if isinstance(s, dict) and all(k in s for k in ("storyline_id", "n_items", "window_minutes", "text"))
        ]

    by_sl: dict[str, list[tuple[datetime, str, set[str]]]] = defaultdict(list)
    for it in dataset.items_by_id.values():
        text, dt = str(it.get("text") or "").strip(), parse_utc(it.get("created_at"))
        if not text or dt is None:
            continue
        norm, toks = _normalize_for_cluster(text)
        if len(toks) >= 4:
            by_sl[str(it.get("storyline_id") or "unassigned")].append((dt, norm, toks))

    signals: list[dict[str, Any]] = []
    for sid in sorted(by_sl):
        clusters: list[list[tuple[datetime, str, set[str]]]] = []
        for entry in sorted(by_sl[sid], key=lambda x: x[0]):
            placed = False
            for cluster in clusters:
                rep_norm, rep_toks = cluster[0][1], cluster[0][2]
                union = rep_toks | entry[2]
                if entry[1] == rep_norm or (union and len(rep_toks & entry[2]) / len(union) >= 0.85):
                    cluster.append(entry)
                    placed = True
                    break
            if not placed:
                clusters.append([entry])

        for cluster in clusters:
            if len(cluster) < 3:
                continue
            span_s = (cluster[-1][0] - cluster[0][0]).total_seconds()
            if span_s > 3600:
                continue
            mins, n_items = max(1, math.ceil(span_s / 60.0)), len(cluster)
            signals.append({
                "storyline_id": sid,
                "n_items": n_items,
                "window_minutes": mins,
                "text": (
                    f"{n_items} near-identical posts within {mins} minutes. "
                    "This can reflect organic copy-paste or coordination; no account is labelled."
                ),
            })
    return signals


def _select_turning_point(event_config: dict[str, Any], tp_id: str | None) -> dict[str, Any] | None:
    tp_raw = event_config.get("turning_points", {})
    manual = [tp for tp in (tp_raw.get("manual", []) if isinstance(tp_raw, dict) else tp_raw or []) if isinstance(tp, dict)]
    if tp_id:
        return next((tp for tp in manual if tp.get("id") == tp_id), None)
    return manual[0] if manual else {
        "id": "tp-initial", "label": "Initial event marker", "at_utc": None, "confirmed": False, "form": None,
    }


def _classify_pattern(vol_before: float, vol_after: float, addressed: bool) -> str:
    if vol_before == 0.0 and vol_after == 0.0:
        return "unchanged"
    if vol_before == 0.0 and vol_after > 0.0 and addressed:
        return "Source of claims"
    if not addressed and vol_after > 0.0:
        return "Not addressed"
    return "Faded" if (vol_before > 0.0 and vol_after <= vol_before * 0.5) else "Sustained"


def derive_before_after(
    dataset: EventDataset,
    event_config: dict[str, Any],
    turning_point_id: str | None = None,
) -> dict[str, Any] | None:
    """Compute descriptive before/after storyline rates around a turning point (P8.1/P8.2)."""
    chosen = _select_turning_point(event_config, turning_point_id)
    if chosen is None:
        return None
    tp_detail = {
        "id": str(chosen.get("id") or "tp-unknown"),
        "label": str(chosen.get("label") or ""),
        "at_utc": format_utc(chosen.get("at_utc")) if chosen.get("at_utc") else None,
        "confirmed": bool(chosen.get("confirmed", False)),
        "form": chosen.get("form"),
    }
    tp_dt, t0_dt = parse_utc(tp_detail["at_utc"]), parse_utc(extract_t0_utc(event_config))
    all_items = list(dataset.items_by_id.values())

    before_h, after_h = 1.0, 1.0
    if tp_dt and all_items:
        dts = [dt for it in all_items if (dt := parse_utc(it.get("created_at"))) is not None]
        if dts:
            earliest = min(dts) if (t0_dt is None or min(dts) <= t0_dt) else t0_dt
            before_h = max(1.0, (tp_dt - earliest).total_seconds() / 3600.0)
            after_h = max(1.0, (max(dts) - tp_dt).total_seconds() / 3600.0)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in all_items:
        grouped[str(it.get("storyline_id") or "unassigned")].append(it)

    rows: list[dict[str, Any]] = []
    for sid, name, _ in _seed_storyline_meta(event_config):
        s_items = grouped.get(sid, [])
        if tp_dt is None or not s_items:
            rows.append({
                "storyline_id": sid, "name": name, "volume_before_per_h": 0.0,
                "volume_after_per_h": 0.0, "pattern": "unchanged", "addressed": False,
            })
            continue
        before = [it for it in s_items if (dt := parse_utc(it.get("created_at"))) and dt < tp_dt]
        after = [it for it in s_items if (dt := parse_utc(it.get("created_at"))) and dt >= tp_dt]
        vb, va = round(len(before) / before_h, 2), round(len(after) / after_h, 2)
        addressed = any(bool(it.get("is_official")) for it in after)
        rows.append({
            "storyline_id": sid, "name": name, "volume_before_per_h": vb,
            "volume_after_per_h": va, "pattern": _classify_pattern(vb, va, addressed), "addressed": addressed,
        })

    return {
        "turning_point": tp_detail,
        "confirmed": tp_detail["confirmed"],
        "form": tp_detail["form"],
        "note": DESCRIPTIVE_COMPARISON_NOTE,
        "storylines": rows,
    }


def _load_latest_eval_gold(
    event_id: str,
    eval_results_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Read the latest eval result JSON for event_id from EVAL_RESULTS_DIR, if present."""
    if not event_id or not _SAFE_IDENT_RE.match(event_id):
        return None
    if eval_results_dir is not None:
        results_dir = Path(eval_results_dir)
    else:
        env_dir = os.environ.get("EVAL_RESULTS_DIR", "").strip()
        results_dir = Path(env_dir) if env_dir else Path("eval/results")
    if not results_dir.is_dir():
        return None

    latest_key: tuple[str, str] | None = None
    latest_gold: dict[str, Any] | None = None
    for path in sorted(results_dir.glob(f"{event_id}_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(json.dumps({"event": "eval_result_read_error", "path": str(path), "error": str(exc)}))
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("run_date") is None and payload.get("assignment_accuracy") is None:
            continue
        key = (str(payload.get("run_date") or ""), path.name)
        if latest_key is None or key > latest_key:
            latest_key = key
            latest_gold = {
                "assignment_accuracy": payload.get("assignment_accuracy"),
                "stance_macro_f1": payload.get("stance_macro_f1"),
                "run_date": payload.get("run_date"),
            }
    return latest_gold


def derive_trust(
    dataset: EventDataset,
    event_config: dict[str, Any],
    *,
    event_id: str | None = None,
    eval_results_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Compute source coverage counts, unassigned_count, backfilled_share, and gold metrics."""
    coverage: dict[str, dict[str, Any]] = {}
    for src, conf in (event_config.get("sources") or {}).items():
        if isinstance(conf, dict) and conf.get("enabled") is True:
            entry: dict[str, Any] = {"count": 0}
            if conf.get("max_posts") is not None:
                entry["cap"] = int(conf["max_posts"])
            elif conf.get("max_videos") is not None:
                entry["cap"] = int(conf["max_videos"])
            coverage[src] = entry

    unassigned, backfilled, cap_known = 0, 0, 0
    for it in dataset.items_by_id.values():
        plat = str(it.get("platform") or "").lower()
        if plat in coverage:
            coverage[plat]["count"] += 1
        elif plat:
            coverage[plat] = {"count": 1}
        if str(it.get("storyline_id") or "unassigned") == "unassigned":
            unassigned += 1
        if it.get("captured_by"):
            cap_known += 1
            if it["captured_by"] == "backfill":
                backfilled += 1

    pre_trust = dataset.precomputed_trust or {}
    backfilled_share = round(backfilled / cap_known, 4) if cap_known > 0 else pre_trust.get("backfilled_share")
    gold_val = dataset.gold if dataset.gold is not None else pre_trust.get("gold")
    if gold_val is None and event_id:
        gold_val = _load_latest_eval_gold(event_id, eval_results_dir=eval_results_dir)
    return {
        "coverage": coverage,
        "gold": gold_val,
        "unassigned_count": unassigned,
        "backfilled_share": backfilled_share,
    }


def _fmt_t_hour(hour: int) -> str:
    return f"T+{hour}h" if hour >= 0 else f"T{hour}h"


def derive_brief(
    dataset: EventDataset,
    event_id: str,
    event_config: dict[str, Any],
) -> dict[str, Any]:
    """Compute a grounded, evidence-linked brief JSON strictly from stored data (P7/P8)."""
    if dataset.precomputed_brief is not None:
        return dict(dataset.precomputed_brief)

    storylines_full = derive_storylines(dataset, event_config)
    ba_by_id = {r["storyline_id"]: r for r in ((derive_before_after(dataset, event_config) or {}).get("storylines") or [])}
    trust = derive_trust(dataset, event_config)
    signals = derive_signals(dataset)

    claim_ids: list[str] = []
    for c in event_config.get("claims_to_check") or []:
        if isinstance(c, dict) and c.get("id") and str(c["id"]) not in claim_ids:
            claim_ids.append(str(c["id"]))
    for it in dataset.items_by_id.values():
        for cl in it.get("claims") or []:
            if cl.get("claim_id") and cl["claim_id"] not in claim_ids:
                claim_ids.append(cl["claim_id"])
    claims_cards = [derive_claim_card(dataset, event_id, event_config, cid) for cid in claim_ids]

    model_id = next((str(it["model_id"]) for it in dataset.items_by_id.values() if it.get("model_id")), None)
    prompt_ver = next((str(it["prompt_version"]) for it in dataset.items_by_id.values() if it.get("prompt_version")), None)

    brief_storylines: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for sl in storylines_full:
        if sl["id"] == "unassigned" and sl["count"] == 0:
            continue
        fs = sl.get("first_seen")
        if fs:
            later = [f"{p['platform']} ({_fmt_t_hour(p['hour'])})" for p in sl.get("platforms_reached") or [] if p["platform"] != fs["platform"]]
            origin = f"First seen on {fs['platform']} at {_fmt_t_hour(fs['hour'])}" + (f"; reached {', '.join(later)}" if later else "")
        else:
            origin = "Not yet seen"
        ba = ba_by_id.get(sl["id"])
        after_str = (
            f"Volume moved from {ba['volume_before_per_h']}/h to {ba['volume_after_per_h']}/h ({ba['pattern']})"
            if ba and (ba["volume_before_per_h"] > 0 or ba["volume_after_per_h"] > 0)
            else "[n]"
        )
        top_amp = (sl.get("top_amplifiers") or [None])[0]
        brief_storylines.append({
            "id": sl["id"], "name": sl["name"], "volume": sl["count"],
            "origin_and_spread": origin, "after_response": after_str,
            "top_amplifier": top_amp["label"] if isinstance(top_amp, dict) else None,
        })
        if sl["count"] > 0 and fs and sl["id"] != "unassigned":
            linked = [it["item_id"] for it in dataset.items_by_id.values() if str(it.get("storyline_id") or "unassigned") == sl["id"]][:5]
            summary.append({"text": f"{sl['name']}: {sl['count']} collected items ({origin}).", "item_ids": linked})

    evidence_to_cite: list[dict[str, Any]] = []
    watch: list[str] = [s["text"] for s in signals]
    for card in claims_cards:
        if card["evidence"]:
            top_ev = card["evidence"][0]
            prefix = "Self-reported — " if card["self_reported"] else ""
            c_note = (
                f" [Disclosed conflict: {top_ev['conflict']['person']} — {top_ev['conflict']['relationship']}]"
                if isinstance(top_ev.get("conflict"), dict) else ""
            )
            ev_ids = [e["item_id"] for e in card["evidence"][:5]]
            evidence_to_cite.append({
                "text": (
                    f"{prefix}{card['text']} ({card['status']}: {card['counts']['supporting']} supporting, "
                    f"{card['counts']['contradicting']} contradicting): \"{top_ev['span']}\"{c_note}"
                ),
                "item_ids": ev_ids,
            })
            summary.append({
                "text": (
                    f"Claim '{card['text']}' is currently {card['status']} "
                    f"({card['counts']['supporting']} supporting, {card['counts']['contradicting']} contradicting)."
                ),
                "item_ids": ev_ids,
            })
        if card["self_reported"] and card["counts"]["supporting"] > 0:
            watch.append(f"Self-reported claim '{card['text']}' has no independent confirmation yet.")

    total = len(dataset.items_by_id)
    method = (
        f"{DESCRIPTIVE_COMPARISON_NOTE} No items have been collected yet for this event; missing counts remain [n]."
        if total == 0
        else f"{DESCRIPTIVE_COMPARISON_NOTE} Computed strictly from {total} collected public items; quotes are verified exact substrings."
    )
    return {
        "summary": summary, "storylines": brief_storylines, "claims": claims_cards,
        "watch": watch, "evidence_to_cite": evidence_to_cite, "method_and_limits": method,
        "coverage": trust["coverage"], "generated_at": format_utc(datetime.now(timezone.utc)),
        "model_id": model_id, "prompt_version": prompt_ver,
    }


# ---------------------------------------------------------------------------
# Dataset Builders (Frames/Snapshot/BigQuery and Spanner)
# ---------------------------------------------------------------------------


def _build_dataset_from_records(
    event_config: dict[str, Any],
    frames_raw: list[dict[str, Any]],
    items_raw: list[dict[str, Any]],
    enrichments_raw: list[dict[str, Any]],
    account_map_raw: list[dict[str, Any]],
    *,
    precomputed_signals: list[dict[str, Any]] | None = None,
    gold: dict[str, Any] | None = None,
    precomputed_brief: dict[str, Any] | None = None,
    precomputed_trust: dict[str, Any] | None = None,
) -> EventDataset:
    t0_dt = parse_utc(extract_t0_utc(event_config))
    frames = sorted([_normalize_frame(f) for f in frames_raw if isinstance(f, dict)], key=lambda x: x["hour"])
    node_by_id = {str(n["id"]): n for f in frames for n in f["nodes_added"] if n.get("id")}
    all_edges = [e for f in frames for e in f["edges_added"]]
    acct_lookup = {
        (str(r["platform"]).strip().lower(), str(r["author_ref"]).strip()): int(r["account_n"])
        for r in account_map_raw
        if isinstance(r, dict) and r.get("platform") and r.get("author_ref") and r.get("account_n") is not None
    }
    enrich_by_id = {str(e["item_id"]): e for e in enrichments_raw if isinstance(e, dict) and e.get("item_id")}

    items_by_id: dict[str, dict[str, Any]] = {}
    for raw_item in items_raw:
        if not isinstance(raw_item, dict) or not raw_item.get("item_id"):
            continue
        iid = str(raw_item["item_id"])
        enr, node = enrich_by_id.get(iid, {}), node_by_id.get(iid, {})
        is_off = bool(enr.get("is_official", raw_item.get("is_official", node.get("is_official", False))))
        raw_conflict = raw_item.get("conflict")
        conflict_person = (
            enr.get("conflict_person")
            or raw_item.get("conflict_person")
            or (raw_conflict.get("person") if isinstance(raw_conflict, dict) else None)
            or (node["conflict"].get("person") if isinstance(node.get("conflict"), dict) else None)
        )
        full_text = raw_item.get("text")
        raw_claims = list(enr.get("claims") or raw_item.get("claims") or [])
        items_by_id[iid] = {
            "item_id": iid,
            "platform": str(raw_item.get("platform") or node.get("platform") or ""),
            "kind": str(raw_item.get("kind") or node.get("kind") or "post"),
            "url": raw_item.get("url"),
            "created_at": format_utc(raw_item.get("created_at") or node.get("created_at")),
            "collected_at": format_utc(raw_item.get("collected_at") or raw_item.get("created_at")),
            "hour": _compute_item_hour({**raw_item, "hour": node.get("hour", raw_item.get("hour"))}, t0_dt),
            "text": full_text,
            "excerpt": raw_item.get("excerpt"),
            "lang": enr.get("lang") or raw_item.get("lang") or node.get("lang"),
            "storyline_id": str(
                enr.get("storyline_id") or raw_item.get("storyline_id") or node.get("storyline_id") or "unassigned"
            ),
            "reach": _compute_reach({**raw_item, "reach": node.get("reach", raw_item.get("reach"))}),
            "is_official": is_off,
            "self_reported": bool(raw_item.get("self_reported", node.get("self_reported", is_off))),
            "conflict_person": conflict_person,
            "captured_by": raw_item.get("captured_by"),
            "time_approximate": bool(raw_item.get("time_approximate", False)),
            "entities": list(enr.get("entities") or raw_item.get("entities") or []),
            "tags": list(raw_item.get("tags") or []),
            "claims": _filter_verified_claims(raw_claims, full_text),
            "label": _resolve_display_label(raw_item, event_config, acct_lookup, str(node.get("label") or "") or None, is_off),
            "model_id": enr.get("model_id") or raw_item.get("model_id"),
            "prompt_version": enr.get("prompt_version") or raw_item.get("prompt_version"),
        }

    for nid, node in node_by_id.items():
        if nid in items_by_id or node.get("type") not in ("post", "article", None):
            continue
        items_by_id[nid] = {
            "item_id": nid, "platform": str(node.get("platform") or ""),
            "kind": str(node.get("kind") or "post"), "url": node.get("url"),
            "created_at": format_utc(node.get("created_at")), "collected_at": format_utc(node.get("created_at")),
            "hour": int(node.get("hour", 0)), "text": None, "excerpt": "", "lang": node.get("lang"),
            "storyline_id": str(node.get("storyline_id") or "unassigned"), "reach": int(node.get("reach") or 0),
            "is_official": bool(node.get("is_official", False)), "self_reported": bool(node.get("self_reported", False)),
            "conflict_person": node["conflict"].get("person") if isinstance(node.get("conflict"), dict) else None,
            "captured_by": None, "time_approximate": False, "entities": [], "tags": [], "claims": [],
            "label": str(node.get("label") or "Account A0"), "model_id": None, "prompt_version": None,
        }

    return EventDataset(
        frames=frames, items_by_id=items_by_id, edges=all_edges,
        precomputed_signals=precomputed_signals, gold=gold,
        precomputed_brief=precomputed_brief, precomputed_trust=precomputed_trust,
    )


def _build_dataset_from_spanner_graph(
    event_config: dict[str, Any],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> EventDataset:
    """Convert Spanner Graph (nodes, edges) into the shared EventDataset shape."""
    t0_dt = parse_utc(extract_t0_utc(event_config))
    claims_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    edges_by_hour: dict[int, list[dict[str, Any]]] = defaultdict(list)
    sanitized_edges: list[dict[str, Any]] = []

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        hr = _compute_item_hour(edge, t0_dt)
        clean_edge = _sanitize_edge({**edge, "hour": hr})
        edges_by_hour[hr].append(clean_edge)
        sanitized_edges.append(clean_edge)
        etype = str(edge.get("type") or "").lower()
        if etype in ("supports", "contradicts") and edge.get("source") and edge.get("target"):
            claims_by_item[str(edge["source"])].append({
                "claim_id": str(edge["target"]), "stance": etype,
                "evidence_span": str(edge.get("evidence_span") or edge.get("span") or ""),
                "agreement": edge.get("agreement", 1.0),
            })

    items_by_id: dict[str, dict[str, Any]] = {}
    nodes_by_hour: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        nid = str(node["id"])
        hr = _compute_item_hour(node, t0_dt)
        nodes_by_hour[hr].append(_sanitize_node({**node, "hour": hr}))
        ntype = str(node.get("type") or "post").lower()
        if ntype in ("post", "article"):
            text_val = node.get("text_excerpt") or node.get("text") or node.get("title") or ""
            items_by_id[nid] = {
                "item_id": nid, "platform": str(node.get("platform") or ""),
                "kind": str(node.get("kind") or ntype), "url": node.get("url"),
                "created_at": format_utc(node.get("created_at")),
                "collected_at": format_utc(node.get("collected_at") or node.get("created_at")),
                "hour": hr, "text": text_val, "excerpt": make_excerpt(text_val), "lang": node.get("lang"),
                "storyline_id": str(node.get("storyline_id") or "unassigned"), "reach": int(node.get("reach") or 0),
                "is_official": bool(node.get("is_official", False)), "self_reported": bool(node.get("self_reported", False)),
                "conflict_person": node.get("conflict_person") or (
                    node["conflict"].get("person") if isinstance(node.get("conflict"), dict) else None
                ),
                "captured_by": node.get("captured_by"), "time_approximate": bool(node.get("time_approximate", False)),
                "entities": list(node.get("entities") or []),
                "claims": _filter_verified_claims(claims_by_item.get(nid, []), text_val),
                "label": str(node.get("label") or "Account A0"),
                "model_id": node.get("model_id"), "prompt_version": node.get("prompt_version"),
            }

    cumulative: dict[str, int] = {sid: 0 for sid, _, _ in _seed_storyline_meta(event_config)}
    cumulative["unassigned"] = 0
    frames: list[dict[str, Any]] = []
    for hr in sorted(set(nodes_by_hour) | set(edges_by_hour)):
        hr_nodes = nodes_by_hour.get(hr, [])
        for n in hr_nodes:
            if str(n.get("type") or "post").lower() in ("post", "article"):
                sid = str(n.get("storyline_id") or "unassigned")
                cumulative[sid] = cumulative.get(sid, 0) + 1
        frames.append({
            "hour": hr, "nodes_added": hr_nodes,
            "edges_added": edges_by_hour.get(hr, []), "storyline_counts": dict(cumulative),
        })

    return EventDataset(frames=frames, items_by_id=items_by_id, edges=sanitized_edges)


# ---------------------------------------------------------------------------
# DataStore Protocol & Concrete Backends
# ---------------------------------------------------------------------------


class DataStore(Protocol):
    """Read-only data store protocol shared across GRAPH_BACKEND implementations."""

    @property
    def backend_name(self) -> str: ...

    def get_frames(self, event_id: str, event_config: dict[str, Any], from_hour: int | None = None, to_hour: int | None = None) -> list[dict[str, Any]]: ...
    def get_storylines(self, event_id: str, event_config: dict[str, Any], at_hour: int | None = None) -> list[dict[str, Any]]: ...
    def get_storyline_detail(self, event_id: str, event_config: dict[str, Any], storyline_id: str, at_hour: int | None = None) -> dict[str, Any] | None: ...
    def get_item(self, event_id: str, event_config: dict[str, Any], item_id: str) -> dict[str, Any] | None: ...
    def get_claim_card(self, event_id: str, event_config: dict[str, Any], claim_id: str, at_hour: int | None = None) -> dict[str, Any]: ...
    def get_signals(self, event_id: str, event_config: dict[str, Any]) -> list[dict[str, Any]]: ...
    def get_before_after(self, event_id: str, event_config: dict[str, Any], turning_point_id: str | None = None) -> dict[str, Any] | None: ...
    def get_brief(self, event_id: str, event_config: dict[str, Any]) -> dict[str, Any]: ...
    def get_trust(self, event_id: str, event_config: dict[str, Any]) -> dict[str, Any]: ...


class _BaseDerivedStore:
    """Shared brief cache and derivation dispatch for concrete DataStore backends."""

    def __init__(self) -> None:
        self._brief_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _load_dataset(self, event_id: str, event_config: dict[str, Any], *, at_hour: int | None = None) -> EventDataset:
        raise NotImplementedError

    def get_frames(self, event_id: str, event_config: dict[str, Any], from_hour: int | None = None, to_hour: int | None = None) -> list[dict[str, Any]]:
        return derive_frames(self._load_dataset(event_id, event_config, at_hour=to_hour), from_hour=from_hour, to_hour=to_hour)

    def get_storylines(self, event_id: str, event_config: dict[str, Any], at_hour: int | None = None) -> list[dict[str, Any]]:
        return derive_storylines(self._load_dataset(event_id, event_config, at_hour=at_hour), event_config, at_hour=at_hour)

    def get_storyline_detail(self, event_id: str, event_config: dict[str, Any], storyline_id: str, at_hour: int | None = None) -> dict[str, Any] | None:
        return derive_storyline_detail(self._load_dataset(event_id, event_config, at_hour=at_hour), event_config, storyline_id, at_hour=at_hour)

    def get_item(self, event_id: str, event_config: dict[str, Any], item_id: str) -> dict[str, Any] | None:
        return derive_item_detail(self._load_dataset(event_id, event_config), event_config, item_id)

    def get_claim_card(self, event_id: str, event_config: dict[str, Any], claim_id: str, at_hour: int | None = None) -> dict[str, Any]:
        return derive_claim_card(self._load_dataset(event_id, event_config, at_hour=at_hour), event_id, event_config, claim_id, at_hour=at_hour)

    def get_signals(self, event_id: str, event_config: dict[str, Any]) -> list[dict[str, Any]]:
        return derive_signals(self._load_dataset(event_id, event_config))

    def get_before_after(self, event_id: str, event_config: dict[str, Any], turning_point_id: str | None = None) -> dict[str, Any] | None:
        return derive_before_after(self._load_dataset(event_id, event_config), event_config, turning_point_id=turning_point_id)

    def get_brief(self, event_id: str, event_config: dict[str, Any]) -> dict[str, Any]:
        ds = self._load_dataset(event_id, event_config)
        key = (event_id, ds.fingerprint())
        if key not in self._brief_cache:
            self._brief_cache[key] = derive_brief(ds, event_id, event_config)
        return dict(self._brief_cache[key])

    def get_trust(self, event_id: str, event_config: dict[str, Any]) -> dict[str, Any]:
        return derive_trust(self._load_dataset(event_id, event_config), event_config, event_id=event_id)


class FramesStore(_BaseDerivedStore):
    """Frames-backed DataStore reading local FRAMES_DIR JSON or BigQuery tables."""

    def __init__(
        self,
        frames_dir: str | Path | None = None,
        gcp_project: str = "",
        bq_dataset: str = "verifact",
        bq_client: Any | None = None,
    ) -> None:
        super().__init__()
        self._frames_dir = Path(frames_dir) if frames_dir else None
        self._gcp_project = gcp_project.strip()
        self._bq_dataset = bq_dataset.strip() or "verifact"
        self._bq_client = bq_client

    @property
    def backend_name(self) -> str:
        return "frames"

    def _read_json(self, path: Path) -> Any:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(json.dumps({"event": "frames_json_read_error", "path": str(path), "error": str(exc)}))
            return None

    def _load_local(self, event_id: str, event_config: dict[str, Any]) -> EventDataset | None:
        if self._frames_dir is None or not self._frames_dir.is_dir():
            return None
        frames_path, snap_path = self._frames_dir / f"{event_id}.frames.json", self._frames_dir / f"{event_id}.snapshot.json"
        if not frames_path.is_file() and not snap_path.is_file():
            return None
        raw_frames = self._read_json(frames_path)
        snap = self._read_json(snap_path)
        snapshot = snap if isinstance(snap, dict) else {}
        if isinstance(raw_frames, list):
            frames_list = raw_frames
        elif isinstance(raw_frames, dict) and isinstance(raw_frames.get("frames"), list):
            frames_list = raw_frames["frames"]
        else:
            frames_list = snapshot.get("frames") or []
        return _build_dataset_from_records(
            event_config, frames_list, snapshot.get("items") or [],
            snapshot.get("enrichments") or [], snapshot.get("account_map") or [],
            precomputed_signals=snapshot.get("signals"), gold=snapshot.get("gold"),
            precomputed_brief=snapshot.get("brief"), precomputed_trust=snapshot.get("trust"),
        )

    def _table_ref(self, table: str) -> str:
        full = f"{self._gcp_project}.{self._bq_dataset}.{table}" if self._gcp_project else f"{self._bq_dataset}.{table}"
        if not _SAFE_IDENT_RE.match(full):
            raise ValueError(f"Unsafe BigQuery table identifier: {full!r}")
        return full

    def _get_bq_client(self) -> Any | None:
        if self._bq_client is not None:
            return self._bq_client
        if not self._gcp_project:
            return None
        try:
            from google.cloud import bigquery

            self._bq_client = bigquery.Client(project=self._gcp_project)
            return self._bq_client
        except Exception as exc:  # noqa: BLE001
            logger.warning(json.dumps({"event": "bq_client_init_failed", "error": str(exc)}))
            return None

    def _run_bq_query(self, client: Any, sql: str, event_id: str) -> list[dict[str, Any]]:
        job_config = None
        try:
            from google.cloud import bigquery

            job_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("event_id", "STRING", event_id)]
            )
        except ImportError:
            pass
        try:
            return [dict(row) for row in client.query(sql, job_config=job_config).result()]
        except Exception as exc:  # noqa: BLE001
            logger.warning(json.dumps({"event": "bq_query_failed", "event_id": event_id, "error": str(exc)}))
            return []

    def _load_bigquery(self, event_id: str, event_config: dict[str, Any]) -> EventDataset | None:
        client = self._get_bq_client()
        if client is None:
            return None
        f_tbl, i_tbl, e_tbl, a_tbl = (self._table_ref(t) for t in ("frames", "items", "enrichments", "account_map"))
        # S4.5: explicit column lists + WHERE event_id = @event_id (no wildcard projections)
        f_rows = self._run_bq_query(
            client,
            f"SELECT hour, nodes_added, edges_added, storyline_counts FROM `{f_tbl}` "
            f"WHERE event_id = @event_id AND built_at = (SELECT MAX(built_at) FROM `{f_tbl}` WHERE event_id = @event_id) "
            "ORDER BY hour ASC",
            event_id,
        )
        i_rows = self._run_bq_query(
            client,
            "SELECT item_id, platform, kind, url, author_ref, author_followers, created_at, collected_at, "
            "text, lang, parent_id, quote_of, links, engagement, hours_from_t0, captured_by, time_approximate "
            f"FROM `{i_tbl}` WHERE event_id = @event_id",
            event_id,
        )
        e_rows = self._run_bq_query(
            client,
            "SELECT item_id, enriched_at, model_id, embedding_model_id, prompt_version, lang, "
            "storyline_id, storyline_similarity, off_topic, claims, entities, is_official, conflict_person "
            f"FROM `{e_tbl}` WHERE event_id = @event_id",
            event_id,
        )
        a_rows = self._run_bq_query(
            client,
            f"SELECT platform, author_ref, account_n, first_seen FROM `{a_tbl}` WHERE event_id = @event_id",
            event_id,
        )
        if not f_rows and not i_rows:
            return EventDataset()
        return _build_dataset_from_records(event_config, f_rows, i_rows, e_rows, a_rows)

    def _load_dataset(self, event_id: str, event_config: dict[str, Any], *, at_hour: int | None = None) -> EventDataset:
        return self._load_local(event_id, event_config) or self._load_bigquery(event_id, event_config) or EventDataset()


class SpannerStore(_BaseDerivedStore):
    """Spanner Graph DataStore delegating live reads to verifact_core.spanner_graph."""

    def __init__(
        self,
        database: Any | None = None,
        query_fn: Callable[..., tuple[list[dict[str, Any]], list[dict[str, Any]]]] | None = None,
        fallback_store: FramesStore | None = None,
    ) -> None:
        super().__init__()
        self._database = database
        self._query_fn = query_fn
        self._fallback_store = fallback_store

    @property
    def backend_name(self) -> str:
        return "spanner"

    def _resolve_query_fn(self) -> Callable[..., tuple[list[dict[str, Any]], list[dict[str, Any]]]] | None:
        if self._query_fn is not None:
            return self._query_fn
        try:
            from verifact_core.spanner_graph import query_graph_at_hour

            return query_graph_at_hour
        except Exception as exc:  # noqa: BLE001
            logger.warning(json.dumps({"event": "spanner_graph_import_failed", "error": str(exc)}))
            return None

    def _load_dataset(self, event_id: str, event_config: dict[str, Any], *, at_hour: int | None = None) -> EventDataset:
        query_fn = self._resolve_query_fn()
        if query_fn is None:
            return self._fallback_store._load_dataset(event_id, event_config, at_hour=at_hour) if self._fallback_store else EventDataset()
        t0_utc = extract_t0_utc(event_config)
        window = event_config.get("window") or {}
        post_h = int(window.get("post_hours", 48)) if isinstance(window, dict) else 48
        horizon = at_hour if at_hour is not None else max(compute_now_hour(t0_utc), post_h, 1)
        try:
            nodes, edges = query_fn(event_id, horizon, t0_utc, database=self._database)
        except Exception as exc:  # noqa: BLE001
            logger.warning(json.dumps({"event": "spanner_query_failed", "event_id": event_id, "error": str(exc)}))
            return self._fallback_store._load_dataset(event_id, event_config, at_hour=at_hour) if self._fallback_store else EventDataset()
        return _build_dataset_from_spanner_graph(event_config, nodes or [], edges or [])
