from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from verifact_core.config import EventConfig, load_event  # noqa: E402

SARVAM_CONFIG_PATH = REPO_ROOT / "events" / "sarvam-token-factory.yaml"


def _minimal_valid_dict(event_id: str = "sample-event") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "name": "Sample Event Launch",
        "t0": {
            "utc": "2026-11-16T13:37:16Z",
            "source_url": "https://x.com/example/status/100",
            "note": "Synthetic test note",
        },
        "window": {
            "pre_hours": 72,
            "post_hours": 240,
            "live": True,
        },
        "official": {
            "accounts": ["ExampleOrg"],
            "posts": ["100"],
            "urls": ["https://example.org/blog"],
            "spokespeople": ["Alice Example"],
        },
        "entities": {
            "org": {"Example": ["Example Org"]},
            "product": {"Widget": ["Example Widget"]},
            "related": ["OtherModel"],
        },
        "keywords": {
            "core": ["example widget"],
            "context": ["inference"],
            "exclude": [],
        },
        "languages": ["en"],
        "sources": {
            "x": {"enabled": True, "max_posts": 1500, "thread_ids": ["100"]},
            "reddit": {"enabled": False, "subreddits": ["india"]},
            "hn": {"enabled": True},
            "youtube": {"enabled": True, "max_videos": 25},
            "news": {"enabled": True, "gdelt": True, "google_news_rss": True},
            "manual": {"enabled": False, "platforms": ["linkedin"]},
        },
        "turning_points": {
            "detect_official_response": True,
            "manual": [
                {
                    "id": "response-1",
                    "label": "Clarification statement",
                    "at_utc": None,
                    "confirmed": False,
                    "form": None,
                }
            ],
        },
        "seed_storylines": [
            {"id": "story-one", "name": "Story One", "seeds": []},
        ],
        "claims_to_check": [
            {"id": "claim-one", "text": "Claim one text"},
        ],
        "conflicts_of_interest": [
            {"person": "Bob Investor", "relationship": "Partner at Fund (per Outlet)."}
        ],
        "known_coverage": ["Outlet One"],
        "privacy": {
            "min_followers_to_name": None,
        },
    }


def _write_yaml(tmp_path: Path, filename: str, data: dict[str, Any]) -> Path:
    path = tmp_path / filename
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_load_sarvam_event_config_meets_ac1_and_ac2() -> None:
    cfg = load_event(str(SARVAM_CONFIG_PATH))
    assert isinstance(cfg, EventConfig)
    assert cfg.event_id == "sarvam-token-factory"
    assert cfg.t0.utc == datetime(2026, 11, 16, 13, 37, 16, tzinfo=timezone.utc)
    assert cfg.sources.x is not None
    assert cfg.sources.x.enabled is True
    assert cfg.sources.x.max_posts == 1500
    assert cfg.sources.reddit is not None and cfg.sources.reddit.enabled is False
    assert cfg.sources.manual is not None and cfg.sources.manual.enabled is False
    assert cfg.sources.hn is not None and cfg.sources.hn.enabled is True
    assert cfg.sources.youtube is not None and cfg.sources.youtube.enabled is True
    assert cfg.sources.news is not None and cfg.sources.news.enabled is True
    assert len(cfg.seed_storylines) == 9
    assert all(s.seeds == [] for s in cfg.seed_storylines)
    assert len(cfg.claims_to_check) == 8
    assert len(cfg.conflicts_of_interest) == 1
    assert "per MediaNama" in cfg.conflicts_of_interest[0].relationship
    assert len(cfg.turning_points.manual) == 1
    tp = cfg.turning_points.manual[0]
    assert tp.id == "response-1"
    assert tp.label == "Sarvam: GPUs privately procured / financed"
    assert tp.at_utc is None
    assert tp.confirmed is False
    assert tp.form is None
    assert cfg.privacy.min_followers_to_name is None


def test_event_config_is_immutable() -> None:
    cfg = load_event(SARVAM_CONFIG_PATH)
    with pytest.raises(ValidationError):
        cfg.name = "Mutated"  # type: ignore[misc]


def test_rejects_unknown_key(tmp_path: Path) -> None:
    data = _minimal_valid_dict("sample-event")
    data["unexpected_field"] = 123
    path = _write_yaml(tmp_path, "sample-event.yaml", data)
    with pytest.raises((ValidationError, ValueError), match="unexpected_field"):
        load_event(path)


def test_rejects_duplicate_claim_id(tmp_path: Path) -> None:
    data = _minimal_valid_dict("sample-event")
    data["claims_to_check"] = [
        {"id": "dup-claim", "text": "First claim"},
        {"id": "dup-claim", "text": "Second claim"},
    ]
    path = _write_yaml(tmp_path, "sample-event.yaml", data)
    with pytest.raises((ValidationError, ValueError), match="claims_to_check|dup-claim"):
        load_event(path)


def test_rejects_duplicate_storyline_id(tmp_path: Path) -> None:
    data = _minimal_valid_dict("sample-event")
    data["seed_storylines"] = [
        {"id": "dup-story", "name": "First", "seeds": []},
        {"id": "dup-story", "name": "Second", "seeds": []},
    ]
    path = _write_yaml(tmp_path, "sample-event.yaml", data)
    with pytest.raises((ValidationError, ValueError), match="seed_storylines|dup-story"):
        load_event(path)


def test_rejects_missing_t0_utc(tmp_path: Path) -> None:
    data = _minimal_valid_dict("sample-event")
    del data["t0"]["utc"]
    path = _write_yaml(tmp_path, "sample-event.yaml", data)
    with pytest.raises((ValidationError, ValueError), match="utc"):
        load_event(path)


def test_rejects_non_utc_t0(tmp_path: Path) -> None:
    data = _minimal_valid_dict("sample-event")
    data["t0"]["utc"] = "2026-11-16T19:07:16+05:30"
    path = _write_yaml(tmp_path, "sample-event.yaml", data)
    with pytest.raises((ValidationError, ValueError), match="UTC|utc"):
        load_event(path)


def test_rejects_event_id_mismatch_with_filename(tmp_path: Path) -> None:
    data = _minimal_valid_dict("wrong-slug")
    path = _write_yaml(tmp_path, "expected-slug.yaml", data)
    with pytest.raises(ValueError, match="event_id.*filename|expected-slug"):
        load_event(path)


def test_rejects_non_slug_event_id(tmp_path: Path) -> None:
    data = _minimal_valid_dict("Invalid_Slug!")
    path = _write_yaml(tmp_path, "Invalid_Slug!.yaml", data)
    with pytest.raises((ValidationError, ValueError), match="event_id|slug"):
        load_event(path)


def test_rejects_enabled_x_without_max_posts(tmp_path: Path) -> None:
    data = _minimal_valid_dict("sample-event")
    data["sources"]["x"] = {"enabled": True}
    path = _write_yaml(tmp_path, "sample-event.yaml", data)
    with pytest.raises((ValidationError, ValueError), match="max_posts"):
        load_event(path)


def test_rejects_no_enabled_sources(tmp_path: Path) -> None:
    data = _minimal_valid_dict("sample-event")
    for key in data["sources"]:
        data["sources"][key]["enabled"] = False
    path = _write_yaml(tmp_path, "sample-event.yaml", data)
    with pytest.raises((ValidationError, ValueError), match="sources|enabled"):
        load_event(path)
