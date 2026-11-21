"""Export privacy-sanitized static JSON API responses from local frames_cache.

Uses the real FastAPI app (services.api.main.create_app) via TestClient so every
exported response passes through the exact production redaction, pseudonym, and
schema-validation pipeline (P5.2, P5.5).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from services.api.main import Settings, create_app


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def export_static_api(frames_dir: str, out_dir: str, events_dir: str = "events") -> dict[str, int]:
    settings = Settings(
        graph_backend="frames",
        frames_dir=frames_dir,
        events_dir=events_dir,
        spanner_live=False,
        live_ingestion=False,
    )
    app = create_app(settings)
    client = TestClient(app)
    out = Path(out_dir)

    events_resp = client.get("/events")
    events_resp.raise_for_status()
    events = events_resp.json()
    _write_json(out / "events.json", events)

    summary = {"events": len(events), "frames": 0, "items": 0, "storylines": 0, "claims": 0}

    for ev in events:
        event_id = ev["event_id"]
        ev_dir = out / "events" / event_id

        detail = client.get(f"/events/{event_id}").json()
        _write_json(ev_dir / "detail.json", detail)

        frames = client.get(f"/events/{event_id}/frames").json()
        _write_json(ev_dir / "frames.json", frames)
        summary["frames"] += len(frames)

        storylines = client.get(f"/events/{event_id}/storylines").json()
        _write_json(ev_dir / "storylines.json", storylines)
        summary["storylines"] += len(storylines)

        storyline_details: dict[str, Any] = {}
        for row in storylines:
            sid = row["id"]
            r = client.get(f"/events/{event_id}/storylines/{sid}")
            if r.status_code == 200:
                storyline_details[sid] = r.json()
        _write_json(ev_dir / "storyline_details.json", storyline_details)

        items_map: dict[str, Any] = {}
        seen_ids: set[str] = set()
        for frame in frames:
            for node in frame.get("nodes_added", []):
                nid = node.get("id")
                if not nid or nid in seen_ids:
                    continue
                seen_ids.add(nid)
                r = client.get(f"/events/{event_id}/items/{nid}")
                if r.status_code == 200:
                    items_map[nid] = r.json()
        _write_json(ev_dir / "items.json", items_map)
        summary["items"] += len(items_map)

        claims_map: dict[str, Any] = {}
        claim_ids: set[str] = set()
        for claim in detail.get("claims", []):
            cid = claim.get("id") or claim.get("claim_id")
            if cid:
                claim_ids.add(str(cid))
        for sd in storyline_details.values():
            for claim in sd.get("claims", []):
                cid = claim.get("id") or claim.get("claim_id")
                if cid:
                    claim_ids.add(str(cid))
        for item in items_map.values():
            for claim in item.get("claims", []):
                cid = claim.get("id") or claim.get("claim_id")
                if cid:
                    claim_ids.add(str(cid))
        for cid in sorted(claim_ids):
            r = client.get(f"/events/{event_id}/claims/{cid}")
            if r.status_code == 200:
                claims_map[cid] = r.json()
        _write_json(ev_dir / "claims.json", claims_map)
        summary["claims"] += len(claims_map)

        signals_r = client.get(f"/events/{event_id}/signals")
        _write_json(ev_dir / "signals.json", signals_r.json() if signals_r.status_code == 200 else [])

        ba_r = client.get(f"/events/{event_id}/before-after")
        if ba_r.status_code == 200:
            _write_json(ev_dir / "before_after.json", ba_r.json())

        brief_r = client.get(f"/events/{event_id}/brief")
        if brief_r.status_code == 200:
            _write_json(ev_dir / "brief.json", brief_r.json())

        trust_r = client.get(f"/events/{event_id}/trust")
        if trust_r.status_code == 200:
            _write_json(ev_dir / "trust.json", trust_r.json())

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Export static JSON API bundle from frames_cache.")
    parser.add_argument("--frames-dir", default="./frames_cache", help="Directory containing precomputed frames.")
    parser.add_argument("--out-dir", default="web/public/static-api", help="Output directory for static JSON.")
    parser.add_argument("--events-dir", default="events", help="Directory containing event YAML files.")
    args = parser.parse_args()
    summary = export_static_api(args.frames_dir, args.out_dir, args.events_dir)
    print(json.dumps({"event": "static_api_exported", **summary}))


if __name__ == "__main__":
    main()
