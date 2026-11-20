"""VeriFact Check service (Spec 14).

``POST /check`` takes text, a URL or an image and returns claim cards in the shape shared with the
graph inspector, the Check page and the extension (steering_docs/api.md). Public and Gemini-backed,
so it is rate-limited per client, cached by content hash, and capped per day (S3.5). Nothing about
the person checking is stored or logged (P5.4).

Run with ``uvicorn services.check.main:create_app --factory``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.check.fetch import HttpFetcher
from services.check.gemini import PROMPT_VERSION, GeminiError, GeminiGateway
from services.check.pipeline import (
    PERSON_NOTICE,
    CheckError,
    CheckInput,
    Fetcher,
    Gateway,
    analyse,
    looks_like_person_assessment,
)
from verifact_core.config import EventConfig, load_event
from verifact_core.url_safety import is_social_url, normalize_url

logger = logging.getLogger("services.check")

REPORTS_COLLECTION = "check_reports"
REPORT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
CACHE_ENTRIES = 256
MEMORY_REPORTS = 1000


class CheckSettings(BaseSettings):
    """Environment settings. The limits have no defaults: the service refuses to start without them."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    gcp_project: str
    gcp_region: str
    gemini_model: str
    event_id: str
    events_dir: str = "events"
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"
    check_enabled: bool = True
    check_rate_limit_per_min: int = Field(gt=0)
    check_daily_call_cap: int = Field(gt=0)
    check_max_input_chars: int = Field(gt=0)
    check_max_image_bytes: int = Field(gt=0)

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


# ------------------------------------------------------------------------- limits


class RateLimiter:
    """Per-client sliding window. Clients are keyed by a salted hash, so no IP is ever kept."""

    # ponytail: counters live in one instance, so the real ceiling is per_minute x max-instances;
    # keep Cloud Run max-instances small, or move the counters to Firestore if that is not enough.
    def __init__(self, per_minute: int, *, salt: bytes, clock: Callable[[], float] = time.monotonic):
        self._per_minute, self._salt, self._clock = per_minute, salt, clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, client: str) -> float | None:
        """None if allowed, otherwise the seconds until the client may retry."""
        key = hashlib.sha256(self._salt + client.encode()).hexdigest()
        now = self._clock()
        with self._lock:
            window = self._hits.setdefault(key, deque())
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) >= self._per_minute:
                return 60.0 - (now - window[0])
            window.append(now)
            if len(self._hits) > 10_000:
                self._hits = {k: w for k, w in self._hits.items() if w}
            return None


class DailyBudget:
    """Global model-call cap per UTC day (S3.5)."""

    # ponytail: per instance, like RateLimiter; the worst case is cap x max-instances.
    def __init__(self, cap: int, *, now: Callable[[], datetime] | None = None):
        self._cap = cap
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._day, self._used = self._now().date(), 0
        self._lock = threading.Lock()

    def try_reserve(self, n: int = 1) -> bool:
        with self._lock:
            today = self._now().date()
            if today != self._day:
                self._day, self._used = today, 0
            if self._used + n > self._cap:
                return False
            self._used += n
            return True


# ------------------------------------------------------------------------ storage


class MemoryStore:
    """In-process report store: the fallback when Firestore is unavailable, and the test double."""

    def __init__(self) -> None:
        self.reports: OrderedDict[str, dict] = OrderedDict()

    def put(self, report: dict) -> None:
        self.reports[report["report_id"]] = report
        while len(self.reports) > MEMORY_REPORTS:
            self.reports.popitem(last=False)

    def get(self, report_id: str) -> dict | None:
        return self.reports.get(report_id)


class FirestoreStore:
    """Reports in Firestore ``check_reports/{report_id}``; the documents hold no user identity."""

    def __init__(self, project: str) -> None:
        from google.cloud import firestore

        self._collection = firestore.Client(project=project).collection(REPORTS_COLLECTION)

    def put(self, report: dict) -> None:
        self._collection.document(report["report_id"]).set(report)

    def get(self, report_id: str) -> dict | None:
        snapshot = self._collection.document(report_id).get()
        return snapshot.to_dict() if snapshot.exists else None


def _default_store(settings: CheckSettings) -> FirestoreStore | MemoryStore:
    try:
        return FirestoreStore(settings.gcp_project)
    except Exception as exc:  # noqa: BLE001 - Firestore is optional; the service must still start
        _log("store_fallback", level=logging.WARNING, reason=type(exc).__name__)
        return MemoryStore()


# ------------------------------------------------------------------------ helpers


def _log(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """One JSON line to stdout. Never pass an IP, input text or any user identity (P5.4)."""
    logger.log(level, json.dumps({"event": event, **fields}, default=str))


def _configure_logging(level: str) -> None:
    logger.setLevel(level.upper())
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


def client_key(request: Request) -> str:
    """The client address as Cloud Run reports it: the last X-Forwarded-For hop."""
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    return hops[-1] if hops else (request.client.host if request.client else "unknown")


def sniff_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _too_large() -> CheckError:
    return CheckError(413, "input_too_large", "That input is too large to check.")


async def read_input(request: Request, settings: CheckSettings) -> CheckInput:
    """Parse and validate the request body: JSON ``{text}`` / ``{url}``, or multipart ``image``."""
    declared = request.headers.get("content-length", "")
    size = int(declared) if declared.isdigit() else 0
    limit = settings.check_max_input_chars

    if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        if size > settings.check_max_image_bytes + 65_536:
            raise _too_large()
        upload = (await request.form()).get("image")
        if not isinstance(upload, UploadFile):
            raise CheckError(422, "invalid_input", "Send an image file in the 'image' field.")
        data = await upload.read(settings.check_max_image_bytes + 1)
        if len(data) > settings.check_max_image_bytes:
            raise _too_large()
        mime = sniff_image_mime(data)
        if mime is None:
            raise CheckError(422, "unsupported_image", "Upload a PNG, JPEG or WebP image.")
        return CheckInput(kind="image", image=(data, mime))

    if size > limit * 4 + 1024:  # UTF-8 uses at most 4 bytes per character
        raise _too_large()
    try:
        body = json.loads(await request.body())
    except ValueError as exc:
        raise CheckError(422, "invalid_input", "The request body must be JSON.") from exc
    text, url = (body.get("text"), body.get("url")) if isinstance(body, dict) else (None, None)
    has_text = isinstance(text, str) and bool(text.strip())
    has_url = isinstance(url, str) and bool(url.strip())
    if has_text == has_url:
        raise CheckError(422, "invalid_input", "Send exactly one of 'text' or 'url'.")

    if isinstance(text, str):
        if len(text.strip()) > limit:
            raise _too_large()
        return CheckInput(kind="text", text=text.strip())

    url = url.strip()
    if len(url) > limit:
        raise _too_large()
    if urlsplit(url).scheme not in {"http", "https"}:
        raise CheckError(422, "invalid_input", "The URL must start with http:// or https://.")
    if is_social_url(url):
        raise CheckError(422, "unsupported_url", "Social media pages are not fetched. Paste the text instead.")
    return CheckInput(kind="url", url=url)


def content_hash(settings: CheckSettings, inp: CheckInput) -> str:
    if inp.kind == "image":
        payload = inp.image[0] if inp.image else b""
    elif inp.kind == "url":
        payload = normalize_url(inp.url or "").encode()
    else:
        payload = " ".join((inp.text or "").split()).encode()
    head = "|".join((PROMPT_VERSION, settings.gemini_model, settings.event_id, inp.kind))
    return hashlib.sha256(head.encode() + b"\0" + payload).hexdigest()


def _public(report: dict) -> dict:
    return {k: report[k] for k in ("report_id", "claims", "matched_event_id")}


# ---------------------------------------------------------------------------- app


def create_app(
    settings: CheckSettings | None = None,
    *,
    gateway: Gateway | None = None,
    fetcher: Fetcher | None = None,
    store: FirestoreStore | MemoryStore | None = None,
    event: EventConfig | None = None,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    settings = settings or CheckSettings()
    event = event or load_event(Path(settings.events_dir) / f"{settings.event_id}.yaml")
    _configure_logging(settings.log_level)
    clock = now or (lambda: datetime.now(timezone.utc))
    gateway = gateway or GeminiGateway(
        project=settings.gcp_project, region=settings.gcp_region, model=settings.gemini_model
    )
    fetcher = fetcher or HttpFetcher()
    store = store or _default_store(settings)
    limiter = RateLimiter(settings.check_rate_limit_per_min, salt=os.urandom(16))
    budget = DailyBudget(settings.check_daily_call_cap, now=clock)
    cache: OrderedDict[str, dict] = OrderedDict()

    app = FastAPI(title="VeriFact Check", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(CheckError)
    async def _check_error(_: Request, exc: CheckError) -> JSONResponse:
        _log("check_refused", status=exc.status, code=exc.code)
        return _error(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(GeminiError)
    async def _gemini_error(_: Request, exc: GeminiError) -> JSONResponse:
        _log("upstream_error", level=logging.ERROR, reason=str(exc))
        return _error(502, "upstream_error", "The checking service is unavailable. Try again shortly.")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, f"http_{exc.status_code}", str(exc.detail))

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "check_enabled": settings.check_enabled}

    @app.post("/check")
    async def post_check(request: Request) -> JSONResponse:
        started = time.monotonic()
        retry = limiter.check(client_key(request))
        if retry is not None:
            raise CheckError(429, "rate_limited", "Too many checks. Please wait a moment.",
                             {"Retry-After": str(max(1, math.ceil(retry)))})
        inp = await read_input(request, settings)

        if inp.kind == "text" and looks_like_person_assessment(inp.text or ""):
            return JSONResponse({"report_id": None, "claims": [], "matched_event_id": None,
                                 "notice": PERSON_NOTICE})

        digest = content_hash(settings, inp)
        if digest in cache:
            cache.move_to_end(digest)
            _log("check_done", input_type=inp.kind, cache_hit=True, hash=digest[:12])
            return JSONResponse(_public(cache[digest]))
        if not settings.check_enabled:
            raise CheckError(503, "check_disabled", "Check is paused. Cached examples only for now.")

        analysis = await analyse(inp, gateway=gateway, fetcher=fetcher, event=event, budget=budget,
                                 now=clock, max_chars=settings.check_max_input_chars)
        if analysis.notice:
            return JSONResponse({"report_id": None, "claims": [], "matched_event_id": None,
                                 "notice": analysis.notice})

        report = {
            "report_id": uuid.uuid4().hex,
            "claims": analysis.cards,
            "matched_event_id": event.event_id if analysis.matched else None,
            "input_type": inp.kind,
            "created_at": _utc_iso(clock()),
            "content_hash": digest,
        }
        try:
            await asyncio.to_thread(store.put, report)
        except Exception as exc:  # noqa: BLE001 - a lost report must not lose the answer
            _log("store_failed", level=logging.ERROR, reason=type(exc).__name__)
        cache[digest] = report
        while len(cache) > CACHE_ENTRIES:
            cache.popitem(last=False)
        _log("check_done", input_type=inp.kind, cache_hit=False, hash=digest[:12],
             claims=len(analysis.cards), latency_ms=round((time.monotonic() - started) * 1000))
        return JSONResponse(_public(report))

    @app.get("/check/{report_id}")
    async def get_check(report_id: str) -> JSONResponse:
        report = await asyncio.to_thread(store.get, report_id) if REPORT_ID_RE.match(report_id) else None
        if report is None:
            raise CheckError(404, "not_found", "No such report.")
        return JSONResponse({k: report[k] for k in
                             ("report_id", "claims", "matched_event_id", "input_type", "created_at")})

    return app
