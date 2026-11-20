"""VeriFact API service: minimal read-only FastAPI application.

Provides event configuration, storylines, claim cards, trust metrics,
and before/after views driven entirely by event configs from EVENTS_DIR.
"""

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
from sse_starlette.sse import EventSourceResponse
import yaml

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
    logger = logging.getLogger("verifact.api")
    logger.setLevel(level.upper())
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    return logger


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

    cors_origins: str = "http://localhost:5173"
    graph_backend: str = "frames"
    events_dir: str = "events"
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
    note: str = "Descriptive comparison, not a causal estimate."
    storylines: list[StorylineBeforeAfter]


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
    """Loads an event config file.

    Attempts to use verifact_core.config.load_event, falling back cleanly to yaml.safe_load.
    """
    try:
        from verifact_core.config import load_event

        cfg = load_event(str(path))
        if hasattr(cfg, "model_dump"):
            return cfg.model_dump()
        return dict(cfg)
    except Exception as exc:
        logger.debug(
            json.dumps({"msg": "Falling back to PyYAML safe_load", "path": str(path), "error": str(exc)})
        )
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = yaml.safe_load(f)
                if isinstance(content, dict):
                    return content
        except Exception as read_err:
            logger.warning(
                json.dumps({"msg": "Could not parse YAML file", "path": str(path), "error": str(read_err)})
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
    direct_candidates = [
        events_dir / f"{event_id}.yaml",
        events_dir / f"{event_id}.yml",
    ]
    for candidate in direct_candidates:
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
    if isinstance(val, datetime):
        return val.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if val is None:
        return ""
    val_str = str(val).strip()
    if val_str.endswith("+00:00"):
        return val_str[:-6] + "Z"
    return val_str


def calculate_now_hour(t0_utc_str: str) -> int:
    """Calculates hours from t0 until now in UTC."""
    try:
        t0_clean = t0_utc_str.replace("Z", "+00:00")
        t0_dt = datetime.fromisoformat(t0_clean)
        now_dt = datetime.now(timezone.utc)
        return int((now_dt - t0_dt).total_seconds() // 3600)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# FastAPI Application Factory
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    """Creates and configures the FastAPI application instance."""
    app_settings = settings or Settings()
    configure_logging(app_settings.log_level)

    app = FastAPI(
        title="VeriFact API",
        description="Read-only API for narrative intelligence graph and verification data",
        version="0.1.0",
    )

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Standardized error responses
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
        return HealthResponse(ok=True, graph_backend=app_settings.graph_backend)

    @app.get("/events", response_model=list[EventSummary])
    async def list_events() -> list[EventSummary]:
        events_dir = Path(app_settings.events_dir)
        results: list[EventSummary] = []
        for eid, data, _ in get_all_events(events_dir):
            name = data.get("name") or eid
            t0 = data.get("t0", {})
            t0_raw = t0.get("utc") if isinstance(t0, dict) else t0
            t0_utc = format_datetime_utc(t0_raw)
            results.append(EventSummary(event_id=eid, name=name, t0_utc=t0_utc))
        return results

    @app.get("/events/{event_id}", response_model=EventDetailResponse)
    async def get_event(event_id: str) -> EventDetailResponse:
        data, path = get_event_or_404(event_id)
        name = data.get("name") or event_id
        t0 = data.get("t0", {})
        t0_raw = t0.get("utc") if isinstance(t0, dict) else t0
        t0_utc = format_datetime_utc(t0_raw)

        # Enabled sources list
        raw_sources = data.get("sources", {})
        enabled_sources: list[str] = []
        if isinstance(raw_sources, dict):
            for src_name, src_conf in raw_sources.items():
                if isinstance(src_conf, dict) and src_conf.get("enabled") is True:
                    enabled_sources.append(src_name)
        elif isinstance(raw_sources, list):
            enabled_sources = raw_sources

        # Turning points
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

        now_hour = calculate_now_hour(t0_utc)

        return EventDetailResponse(
            name=name,
            t0_utc=t0_utc,
            config_file=str(path),
            sources=enabled_sources,
            turning_points=turning_points,
            now_hour=now_hour,
        )

    @app.get("/events/{event_id}/frames")
    async def get_frames(
        event_id: str,
        from_hour: int | None = Query(default=None),
        to_hour: int | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        # Validate event exists
        get_event_or_404(event_id)
        # In MVP, frames are returned when available; never invent nodes or counts
        return []

    @app.get("/events/{event_id}/storylines", response_model=list[StorylineItem])
    async def get_storylines(
        event_id: str,
        at_hour: int | None = Query(default=None),
    ) -> list[StorylineItem]:
        data, _ = get_event_or_404(event_id)
        seed_storylines = data.get("seed_storylines", []) or []

        storylines: list[StorylineItem] = []
        if isinstance(seed_storylines, list):
            for idx, item in enumerate(seed_storylines):
                if isinstance(item, dict):
                    storylines.append(
                        StorylineItem(
                            id=item.get("id", f"storyline-{idx}"),
                            name=item.get("name", f"Storyline {idx}"),
                            colour_index=idx,
                            count=0,
                            first_seen=None,
                            platforms_reached=[],
                            stance_mix=None,
                            agreement=None,
                            top_amplifiers=[],
                        )
                    )

        # Append standard unassigned row
        storylines.append(
            StorylineItem(
                id="unassigned",
                name="Unassigned",
                colour_index=-1,
                count=0,
                first_seen=None,
                platforms_reached=[],
                stance_mix=None,
                agreement=None,
                top_amplifiers=[],
            )
        )
        return storylines

    @app.get(
        "/events/{event_id}/storylines/{storyline_id}",
        response_model=StorylineDetailResponse,
    )
    async def get_storyline_detail(
        event_id: str,
        storyline_id: str,
        at_hour: int | None = Query(default=None),
    ) -> StorylineDetailResponse:
        data, _ = get_event_or_404(event_id)
        if storyline_id == "unassigned":
            return StorylineDetailResponse(
                id="unassigned",
                name="Unassigned",
                colour_index=-1,
                count=0,
                first_seen=None,
                platforms_reached=[],
                stance_mix=None,
                agreement=None,
                top_amplifiers=[],
                volume_by_hour=[],
                jumps=[],
            )

        seed_storylines = data.get("seed_storylines", []) or []
        for idx, item in enumerate(seed_storylines):
            if isinstance(item, dict) and item.get("id") == storyline_id:
                return StorylineDetailResponse(
                    id=item.get("id", storyline_id),
                    name=item.get("name", storyline_id),
                    colour_index=idx,
                    count=0,
                    first_seen=None,
                    platforms_reached=[],
                    stance_mix=None,
                    agreement=None,
                    top_amplifiers=[],
                    volume_by_hour=[],
                    jumps=[],
                )

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Storyline '{storyline_id}' not found",
        )

    @app.get(
        "/events/{event_id}/claims/{claim_id}",
        response_model=ClaimCardResponse,
    )
    async def get_claim(
        event_id: str,
        claim_id: str,
        at_hour: int | None = Query(default=None),
    ) -> ClaimCardResponse:
        data, _ = get_event_or_404(event_id)

        claim_text = f"[{claim_id}]"
        self_reported = False
        self_reported_note = None
        storyline_id = None

        claims_to_check = data.get("claims_to_check", []) or []
        if isinstance(claims_to_check, list):
            for c in claims_to_check:
                if isinstance(c, dict) and c.get("id") == claim_id:
                    claim_text = c.get("text", claim_text)
                    self_reported = bool(c.get("self_reported", False))
                    if self_reported:
                        self_reported_note = "Company statement, not independently confirmed"
                    storyline_id = c.get("storyline_id")
                    break

        return ClaimCardResponse(
            claim_id=claim_id,
            text=claim_text,
            status="unverified",
            counts=ClaimCounts(supporting=0, contradicting=0, neutral=0),
            self_reported=self_reported,
            self_reported_note=self_reported_note,
            evidence=[],
            storyline_id=storyline_id,
            deep_link=f"/e/{event_id}?claim={claim_id}",
        )

    @app.get(
        "/events/{event_id}/before-after",
        response_model=BeforeAfterResponse,
    )
    async def get_before_after(
        event_id: str,
        turning_point: str | None = Query(default=None),
    ) -> BeforeAfterResponse:
        data, _ = get_event_or_404(event_id)
        tp_raw = data.get("turning_points", {})
        manual_tp: list[dict[str, Any]] = []
        if isinstance(tp_raw, dict):
            manual_tp = tp_raw.get("manual", []) or []
        elif isinstance(tp_raw, list):
            manual_tp = tp_raw

        chosen_tp: dict[str, Any] | None = None
        if turning_point:
            for tp in manual_tp:
                if isinstance(tp, dict) and tp.get("id") == turning_point:
                    chosen_tp = tp
                    break
            if not chosen_tp:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Turning point '{turning_point}' not found",
                )
        else:
            if manual_tp and isinstance(manual_tp[0], dict):
                chosen_tp = manual_tp[0]
            else:
                chosen_tp = {
                    "id": "tp-initial",
                    "label": "Initial event marker",
                    "at_utc": None,
                    "confirmed": False,
                    "form": None,
                }

        tp_detail = TurningPointDetail(
            id=chosen_tp.get("id", "tp-unknown"),
            label=chosen_tp.get("label", ""),
            at_utc=format_datetime_utc(chosen_tp.get("at_utc")) if chosen_tp.get("at_utc") else None,
            confirmed=bool(chosen_tp.get("confirmed", False)),
            form=chosen_tp.get("form"),
        )

        seed_storylines = data.get("seed_storylines", []) or []
        storylines: list[StorylineBeforeAfter] = []
        if isinstance(seed_storylines, list):
            for s in seed_storylines:
                if isinstance(s, dict):
                    storylines.append(
                        StorylineBeforeAfter(
                            storyline_id=s.get("id", ""),
                            name=s.get("name", ""),
                            volume_before_per_h=0.0,
                            volume_after_per_h=0.0,
                            pattern="unchanged",
                            addressed=False,
                        )
                    )

        return BeforeAfterResponse(
            turning_point=tp_detail,
            confirmed=tp_detail.confirmed,
            form=tp_detail.form,
            note="Descriptive comparison, not a causal estimate.",
            storylines=storylines,
        )

    @app.get("/events/{event_id}/trust", response_model=TrustResponse)
    async def get_trust(event_id: str) -> TrustResponse:
        data, _ = get_event_or_404(event_id)
        raw_sources = data.get("sources", {})
        coverage: dict[str, dict[str, Any]] = {}

        if isinstance(raw_sources, dict):
            for src_name, src_conf in raw_sources.items():
                if isinstance(src_conf, dict) and src_conf.get("enabled") is True:
                    cov_entry: dict[str, Any] = {"count": 0}
                    if "max_posts" in src_conf:
                        cov_entry["cap"] = src_conf["max_posts"]
                    elif "max_videos" in src_conf:
                        cov_entry["cap"] = src_conf["max_videos"]
                    coverage[src_name] = cov_entry

        return TrustResponse(
            coverage=coverage,
            gold=None,
            unassigned_count=0,
            backfilled_share=None,
        )

    @app.get("/events/{event_id}/live")
    async def live_stream(event_id: str, request: Request) -> EventSourceResponse:
        data, _ = get_event_or_404(event_id)
        is_live = bool(data.get("window", {}).get("live", False))

        async def event_generator():
            # Send initial live status
            yield {
                "event": "status",
                "data": json.dumps({"live": is_live}),
            }

        from sse_starlette.sse import ServerSentEvent

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

    @app.get("/events/{event_id}/signals")
    async def get_signals(event_id: str) -> list[dict[str, Any]]:
        get_event_or_404(event_id)
        return []

    @app.get("/events/{event_id}/items/{item_id}")
    async def get_item(event_id: str, item_id: str) -> dict[str, Any]:
        get_event_or_404(event_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{item_id}' not found",
        )

    return app


app = create_app()
