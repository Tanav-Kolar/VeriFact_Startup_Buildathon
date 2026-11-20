from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

from verifact_core.config import EventConfig, load_event
from verifact_core.queries import Query, build_queries, compute_time_bounds

REPO_ROOT = Path(__file__).resolve().parent.parent
SARVAM_CONFIG_PATH = REPO_ROOT / "events" / "sarvam-token-factory.yaml"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _make_event_config(
    *,
    core: list[str] | None = None,
    context: list[str] | None = None,
    exclude: list[str] | None = None,
    org: dict[str, list[str]] | None = None,
    pre_hours: int = 24,
    post_hours: int = 72,
    live: bool = False,
) -> EventConfig:
    return EventConfig.model_validate(
        {
            "event_id": "synthetic-event",
            "name": "Synthetic Event",
            "t0": {"utc": "2026-11-16T12:00:00Z"},
            "window": {"pre_hours": pre_hours, "post_hours": post_hours, "live": live},
            "official": {"accounts": ["AcmeOfficial"]},
            "entities": {"org": org if org is not None else {"Acme": ["Acme Labs"]}},
            "keywords": {
                "core": core if core is not None else ["acme launch"],
                "context": context if context is not None else ["pricing", "benchmark"],
                "exclude": exclude if exclude is not None else [],
            },
            "sources": {"hn": {"enabled": True}},
        }
    )


def test_build_queries_on_sarvam_config_ac1() -> None:
    cfg = load_event(SARVAM_CONFIG_PATH)

    for platform in ("hn", "youtube", "gdelt", "x"):
        queries_first = build_queries(cfg, platform)
        queries_second = build_queries(cfg, platform)

        assert len(queries_first) > 0
        # Stable query_ids across repeated calls
        assert queries_first == queries_second
        assert all(isinstance(q, Query) for q in queries_first)

        # Unique, valid slug query_ids
        qids = [q.query_id for q in queries_first]
        assert len(qids) == len(set(qids))
        for qid in qids:
            assert SLUG_RE.match(qid), f"query_id not a valid slug: {qid!r}"

        # Every core keyword appears
        texts = [q.text for q in queries_first]
        for core_term in cfg.keywords.core:
            assert core_term in texts

        # Every generated query is constructed strictly from config values
        allowed_fragments = set(cfg.keywords.core) | set(cfg.keywords.context)
        for org_name, aliases in cfg.entities.org.items():
            allowed_fragments.add(org_name)
            allowed_fragments.update(aliases)
        allowed_lower = [f.lower().strip('"') for f in allowed_fragments]
        for q in queries_first:
            q_lower = q.text.lower()
            assert any(frag in q_lower for frag in allowed_lower)


def test_build_queries_drops_exclude_terms_ac1() -> None:
    cfg = _make_event_config(
        core=["acme launch", "acme spam offer"],
        context=["pricing", "spam", "benchmark"],
        exclude=["SPAM"],
        org={"Acme": ["Acme Labs"]},
    )

    queries = build_queries(cfg, "hn")
    texts_lower = [q.text.lower() for q in queries]

    assert "acme launch" in texts_lower
    assert all("spam" not in t for t in texts_lower)
    # Non-excluded context queries combined with org + alias remain
    assert any("acme" in t and "pricing" in t for t in texts_lower)
    assert any("acme labs" in t and "benchmark" in t for t in texts_lower)


def test_build_queries_quotes_multiword_terms_and_validates_platform() -> None:
    cfg = _make_event_config(
        core=["acme launch"],
        context=["gpu cluster"],
        org={"Acme": ["Acme Labs"]},
    )

    queries = build_queries(cfg, "gdelt")
    texts = [q.text for q in queries]
    assert '"Acme Labs" "gpu cluster"' in texts
    assert 'Acme "gpu cluster"' in texts

    with pytest.raises(ValueError, match="Unsupported platform"):
        build_queries(cfg, "unsupported_platform")


def test_compute_time_bounds_enforces_window_and_live_flag() -> None:
    cfg = _make_event_config(pre_hours=24, post_hours=48, live=False)
    t0 = cfg.t0.utc
    window_start = t0 - timedelta(hours=24)
    window_end = t0 + timedelta(hours=48)

    # First run inside window
    now_in = t0 + timedelta(hours=10)
    bounds = compute_time_bounds(cfg, now=now_in)
    assert bounds.should_run is True
    assert bounds.start == window_start
    assert bounds.end == now_in
    assert bounds.window_start == window_start
    assert bounds.window_end == window_end

    # Cursor advances start
    cursor_iso = (t0 + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    bounds_cursor = compute_time_bounds(cfg, cursor_iso=cursor_iso, now=now_in)
    assert bounds_cursor.start == t0 + timedelta(hours=2)
    assert bounds_cursor.end == now_in

    # --since overrides cursor, clamped to window_start
    since_too_early = (t0 - timedelta(hours=100)).isoformat().replace("+00:00", "Z")
    bounds_since = compute_time_bounds(
        cfg, cursor_iso=cursor_iso, since_iso=since_too_early, now=now_in
    )
    assert bounds_since.start == window_start

    # After window_end with live=False -> stop collecting entirely
    now_after = t0 + timedelta(hours=100)
    bounds_after = compute_time_bounds(cfg, now=now_after)
    assert bounds_after.should_run is False

    # After window_end with live=True -> runs, capped at window_end
    cfg_live = _make_event_config(pre_hours=24, post_hours=48, live=True)
    bounds_live = compute_time_bounds(cfg_live, now=now_after)
    assert bounds_live.should_run is True
    assert bounds_live.end == window_end
