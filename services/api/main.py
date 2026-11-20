"""VeriFact API service: read-only FastAPI application.

Serves event configuration, frames, storylines, item detail, claim cards,
neutral timing signals, before/after comparisons, grounded briefs, trust
metrics, and SSE live updates. Supports ``GRAPH_BACKEND=frames`` (local
``FRAMES_DIR`` JSON snapshots or BigQuery) and ``GRAPH_BACKEND=spanner``
(when ``SPANNER_LIVE=true``) with identical response shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
import yaml

from services.api.store import (
    DESCRIPTIVE_COMPARISON_NOTE,
    DataStore,
    FramesStore,
    SpannerStore,
    compute_now_hour,
    format_utc,
)

# ---------------------------------------------------------------------------
# Structured JSON Logging
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Formats log records as structured single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if record.exc_info:
            log_entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configures structured JSON logging on stdout."""
    log = logging.getLogger("verifact.api")
    log.setLevel(level.upper())
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        log.addHandler(handler)
        log.propagate = False
    return log


logger = configure_logging()

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    graph_backend: str = "frames"
    spanner_live: bool = False
    live_ingestion: bool = False
    events_dir: str = "events"
    frames_dir: str | None = None
    gcp_project: str = ""
    bq_dataset: str = "verifact"
    log_level: str = "INFO"
    sse_heartbeat_seconds: float = 15.0

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


# ---------------------------------------------------------------------------
# Pydantic Response Models
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool = True
    graph_backend: str


class EventSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    name: str
    t0_utc: str


class TurningPointDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    at_utc: str | None = None
    confirmed: bool = False
    form: str | None = None


class EventDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    t0_utc: str
    config_file: str
    sources: list[str]
    turning_points: list[TurningPointDetail]
    now_hour: int


class FrameResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: int
    nodes_added: list[dict[str, Any]] = Field(default_factory=list)
    edges_added: list[dict[str, Any]] = Field(default_factory=list)
    storyline_counts: dict[str, int] = Field(default_factory=dict)


class StorylineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    colour_index: int
    count: int = 0
    first_seen: dict[str, Any] | None = None
    platforms_reached: list[dict[str, Any]] = Field(default_factory=list)
    stance_mix: dict[str, int] | None = None
    agreement: float | None = None
    top_amplifiers: list[dict[str, Any]] = Field(default_factory=list)


class StorylineDetailResponse(StorylineItem):
    volume_by_hour: list[dict[str, Any]] = Field(default_factory=list)
    jumps: list[dict[str, Any]] = Field(default_factory=list)


class ItemClaimLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    stance: str
    span: str


class ItemDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    created_at: str
    collected_at: str
    collection_lag_s: int | None = None
    platform: str
    url: str | None = None
    excerpt: str
    storyline_id: str | None = None
    claims: list[ItemClaimLink] = Field(default_factory=list)
    reach: int | None = None
    tags: list[str] = Field(default_factory=list)
    is_official: bool = False
    self_reported: bool = False
    conflict: dict[str, str] | None = None
    lang: str | None = None
    label: str | None = None


class ClaimCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supporting: int = 0
    contradicting: int = 0
    neutral: int = 0


class ClaimCardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    text: str
    status: str = "unverified"
    counts: ClaimCounts = Field(default_factory=ClaimCounts)
    self_reported: bool = False
    self_reported_note: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    storyline_id: str | None = None
    deep_link: str | None = None


class TimingSignalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storyline_id: str
    n_items: int
    window_minutes: int
    text: str


class StorylineBeforeAfter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storyline_id: str
    name: str
    volume_before_per_h: float = 0.0
    volume_after_per_h: float = 0.0
    pattern: str = "unchanged"
    addressed: bool = False


class BeforeAfterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turning_point: TurningPointDetail
    confirmed: bool = False
    form: str | None = None
    note: str = DESCRIPTIVE_COMPARISON_NOTE
    storylines: list[StorylineBeforeAfter]


class BriefSummarySentence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    item_ids: list[str] = Field(default_factory=list)


class BriefStorylineRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    volume: int | None = None
    origin_and_spread: str = ""
    after_response: str = ""
    top_amplifier: str | None = None


class BriefResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: list[BriefSummarySentence] = Field(default_factory=list)
    storylines: list[BriefStorylineRow] = Field(default_factory=list)
    claims: list[ClaimCardResponse] = Field(default_factory=list)
    watch: list[str] = Field(default_factory=list)
    evidence_to_cite: list[BriefSummarySentence] = Field(default_factory=list)
    method_and_limits: str
    coverage: dict[str, Any] = Field(default_factory=dict)
    generated_at: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None


class TrustResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    coverage: dict[str, dict[str, Any]]
    gold: dict[str, Any] | None = None
    unassigned_count: int = 0
    backfilled_share: float | None = None


# ---------------------------------------------------------------------------
# Event Config Loading Helpers
# ---------------------------------------------------------------------------


def load_event_file(path: Path) -> dict[str, Any] | None:
    """Loads an event config file via verifact_core.config.load_event with PyYAML fallback."""
    try:
        from verifact_core.config import load_event

        cfg = load_event(str(path))
        if hasattr(cfg, "model_dump"):
            return cfg.model_dump()
        return dict(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            json.dumps(
                {"msg": "Falling back to PyYAML safe_load", "path": str(path), "error": str(exc)}
            )
        )
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = yaml.safe_load(f)
                if isinstance(content, dict):
                    return content
        except Exception as read_err:  # noqa: BLE001
            logger.warning(
                json.dumps(
                    {"msg": "Could not parse YAML file", "path": str(path), "error": str(read_err)}
                )
            )
    return None


def get_all_events(events_dir: Path) -> list[tuple[str, dict[str, Any], Path]]:
    """Discovers and parses all event YAML files in the given directory."""
    events: list[tuple[str, dict[str, Any], Path]] = []
    if not events_dir.is_dir():
        return events

    yaml_files = sorted(events_dir.glob("*.yaml")) + sorted(events_dir.glob("*.yml"))
    for file_path in yaml_files:
        data = load_event_file(file_path)
        if data:
            event_id = data.get("event_id") or file_path.stem
            events.append((event_id, data, file_path))
    return events


def find_event(events_dir: Path, event_id: str) -> tuple[dict[str, Any], Path] | None:
    """Finds a specific event by event_id or filename stem."""
    for candidate in (events_dir / f"{event_id}.yaml", events_dir / f"{event_id}.yml"):
        if candidate.is_file():
            data = load_event_file(candidate)
            if data:
                return data, candidate

    for eid, data, path in get_all_events(events_dir):
        if eid == event_id:
            return data, path
    return None


def format_datetime_utc(val: Any) -> str:
    """Formats a datetime or string representation into ISO 8601 UTC with trailing Z."""
    return format_utc(val)


def calculate_now_hour(t0_utc_str: str) -> int:
    """Calculates hours from t0 until now in UTC."""
    return compute_now_hour(t0_utc_str)


def build_data_store(settings: Settings) -> DataStore:
    """Build the active DataStore respecting GRAPH_BACKEND and SPANNER_LIVE switch."""
    frames_store = FramesStore(
        frames_dir=settings.frames_dir,
        gcp_project=settings.gcp_project,
        bq_dataset=settings.bq_dataset,
    )
    if settings.graph_backend.strip().lower() == "spanner" and settings.spanner_live:
        return SpannerStore(fallback_store=frames_store)
    return frames_store


# ---------------------------------------------------------------------------
# FastAPI Application Factory
# ---------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    store: DataStore | None = None,
) -> FastAPI:
    """Creates and configures the FastAPI application instance."""
    app_settings = settings or Settings()
    configure_logging(app_settings.log_level)
    active_store: DataStore = store if store is not None else build_data_store(app_settings)

    app = FastAPI(
        title="VeriFact API",
        description="Read-only API for narrative intelligence graph and evidence data",
        version="0.2.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == status.HTTP_404_NOT_FOUND else "http_error"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail)}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )

    def get_event_or_404(event_id: str) -> tuple[dict[str, Any], Path]:
        events_dir = Path(app_settings.events_dir)
        result = find_event(events_dir, event_id)
        if not result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Event '{event_id}' not found",
            )
        return result

    # -----------------------------------------------------------------------
    # Endpoints
    # -----------------------------------------------------------------------

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse(ok=True, graph_backend=active_store.backend_name)

    @app.get("/events", response_model=list[EventSummary])
    async def list_events() -> list[EventSummary]:
        events_dir = Path(app_settings.events_dir)
        results: list[EventSummary] = []
        for eid, data, _ in get_all_events(events_dir):
            name = data.get("name") or eid
            t0 = data.get("t0", {})
            t0_raw = t0.get("utc") if isinstance(t0, dict) else t0
            results.append(EventSummary(event_id=eid, name=name, t0_utc=format_datetime_utc(t0_raw)))
        return results

    @app.get("/events/{event_id}", response_model=EventDetailResponse)
    async def get_event(event_id: str) -> EventDetailResponse:
        data, path = get_event_or_404(event_id)
        name = data.get("name") or event_id
        t0 = data.get("t0", {})
        t0_raw = t0.get("utc") if isinstance(t0, dict) else t0
        t0_utc = format_datetime_utc(t0_raw)

        raw_sources = data.get("sources", {})
        enabled_sources: list[str] = []
        if isinstance(raw_sources, dict):
            for src_name, src_conf in raw_sources.items():
                if isinstance(src_conf, dict) and src_conf.get("enabled") is True:
                    enabled_sources.append(src_name)
        elif isinstance(raw_sources, list):
            enabled_sources = raw_sources

        tp_raw = data.get("turning_points", {})
        manual_tp: list[dict[str, Any]] = []
        if isinstance(tp_raw, dict):
            manual_tp = tp_raw.get("manual", []) or []
        elif isinstance(tp_raw, list):
            manual_tp = tp_raw

        turning_points = [
            TurningPointDetail(
                id=tp.get("id", "tp-unknown"),
                label=tp.get("label", ""),
                at_utc=format_datetime_utc(tp.get("at_utc")) if tp.get("at_utc") else None,
                confirmed=bool(tp.get("confirmed", False)),
                form=tp.get("form"),
            )
            for tp in manual_tp
            if isinstance(tp, dict)
        ]

        return EventDetailResponse(
            name=name,
            t0_utc=t0_utc,
            config_file=str(path),
            sources=enabled_sources,
            turning_points=turning_points,
            now_hour=calculate_now_hour(t0_utc),
        )

    @app.get("/events/{event_id}/frames", response_model=list[FrameResponse])
    async def get_frames(
        event_id: str,
        from_hour: int | None = Query(default=None),
        to_hour: int | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        data, _ = get_event_or_404(event_id)
        return active_store.get_frames(event_id, data, from_hour=from_hour, to_hour=to_hour)

    @app.get("/events/{event_id}/storylines", response_model=list[StorylineItem])
    async def get_storylines(
        event_id: str,
        at_hour: int | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        data, _ = get_event_or_404(event_id)
        return active_store.get_storylines(event_id, data, at_hour=at_hour)

    @app.get(
        "/events/{event_id}/storylines/{storyline_id}",
        response_model=StorylineDetailResponse,
    )
    async def get_storyline_detail(
        event_id: str,
        storyline_id: str,
        at_hour: int | None = Query(default=None),
    ) -> dict[str, Any]:
        data, _ = get_event_or_404(event_id)
        detail = active_store.get_storyline_detail(event_id, data, storyline_id, at_hour=at_hour)
        if detail is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Storyline '{storyline_id}' not found",
            )
        return detail

    @app.get("/events/{event_id}/items/{item_id}", response_model=ItemDetailResponse)
    async def get_item(event_id: str, item_id: str) -> dict[str, Any]:
        data, _ = get_event_or_404(event_id)
        item = active_store.get_item(event_id, data, item_id)
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Item '{item_id}' not found",
            )
        return item

    @app.get(
        "/events/{event_id}/claims/{claim_id}",
        response_model=ClaimCardResponse,
    )
    async def get_claim(
        event_id: str,
        claim_id: str,
        at_hour: int | None = Query(default=None),
    ) -> dict[str, Any]:
        data, _ = get_event_or_404(event_id)
        return active_store.get_claim_card(event_id, data, claim_id, at_hour=at_hour)

    @app.get("/events/{event_id}/signals", response_model=list[TimingSignalResponse])
    async def get_signals(event_id: str) -> list[dict[str, Any]]:
        data, _ = get_event_or_404(event_id)
        return active_store.get_signals(event_id, data)

    @app.get(
        "/events/{event_id}/before-after",
        response_model=BeforeAfterResponse,
    )
    async def get_before_after(
        event_id: str,
        turning_point: str | None = Query(default=None),
    ) -> dict[str, Any]:
        data, _ = get_event_or_404(event_id)
        comparison = active_store.get_before_after(event_id, data, turning_point_id=turning_point)
        if comparison is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Turning point '{turning_point}' not found",
            )
        return comparison

    @app.get("/events/{event_id}/brief", response_model=BriefResponse)
    async def get_brief(event_id: str) -> dict[str, Any]:
        data, _ = get_event_or_404(event_id)
        return active_store.get_brief(event_id, data)

    @app.get("/events/{event_id}/trust", response_model=TrustResponse)
    async def get_trust(event_id: str) -> dict[str, Any]:
        data, _ = get_event_or_404(event_id)
        return active_store.get_trust(event_id, data)

    @app.get("/events/{event_id}/live")
    async def live_stream(event_id: str, request: Request) -> EventSourceResponse:
        data, _ = get_event_or_404(event_id)
        window = data.get("window", {})
        config_live = bool(window.get("live", False)) if isinstance(window, dict) else False
        is_live = config_live or app_settings.live_ingestion

        async def event_generator():
            yield {
                "event": "status",
                "data": json.dumps({"live": is_live}),
            }
            if is_live and not await request.is_disconnected():
                frames = active_store.get_frames(event_id, data)
                if frames:
                    latest = frames[-1]
                    yield {
                        "event": "storyline_counts",
                        "data": json.dumps(latest.get("storyline_counts", {})),
                    }
                    edges_by_src: dict[str, list[dict[str, Any]]] = {}
                    for edge in latest.get("edges_added", []):
                        edges_by_src.setdefault(str(edge.get("source", "")), []).append(edge)
                    for node in latest.get("nodes_added", []):
                        if await request.is_disconnected():
                            break
                        yield {
                            "event": "item_added",
                            "data": json.dumps(
                                {
                                    "node": node,
                                    "edges": edges_by_src.get(str(node.get("id", "")), []),
                                }
                            ),
                        }

        def make_heartbeat() -> ServerSentEvent:
            return ServerSentEvent(
                event="heartbeat",
                data=json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()}),
            )

        ping_interval = max(1, int(app_settings.sse_heartbeat_seconds))
        return EventSourceResponse(
            event_generator(),
            ping=ping_interval,
            ping_message_factory=make_heartbeat,
        )

    return app


app = create_app()
