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
import ipaddress
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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.check.fetch import HttpFetcher, url_problem
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
from verifact_core.url_safety import normalize_url

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
    # Not cost caps, so they have defaults. Hops: how many proxies append to X-Forwarded-For before
    # the request reaches the service (1 = Cloud Run directly; 2 = behind a load balancer).
    trusted_proxy_hops: int = Field(default=1, ge=1)
    check_request_timeout_s: float = Field(default=45.0, gt=0)

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


# ------------------------------------------------------------------------- limits


class RateLimiter:
    """Per-client sliding window. Clients are keyed by a salted hash, so no IP is ever kept."""

    # ponytail: counters live in one instance, so the real ceiling is per_minute x max-instances;
    # keep Cloud Run max-instances small, or move the counters to Firestore if that is not enough.
    def __init__(self, per_minute: int, *, salt: bytes, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 10_000):
        self._per_minute, self._salt, self._clock, self._max_keys = per_minute, salt, clock, max_keys
        self._hits: dict[str, deque[float]] = {}
        self._last_prune = float("-inf")
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        if now - self._last_prune < 1.0:  # at most once a second, so a flood cannot make it O(n) per call
            return
        self._last_prune = now
        self._hits = {k: w for k, w in self._hits.items() if w and now - w[-1] < 60.0}

    def check(self, client: str) -> float | None:
        """None if allowed, otherwise the seconds until the client may retry."""
        key = hashlib.sha256(self._salt + client.encode()).hexdigest()
        now = self._clock()
        with self._lock:
            window = self._hits.get(key)
            if window is None:
                if len(self._hits) >= self._max_keys:
                    self._prune(now)
                    if len(self._hits) >= self._max_keys:
                        return 60.0  # full of active clients: refuse a new one rather than grow
                window = self._hits[key] = deque()
            while window and now - window[0] >= 60.0:
                window.popleft()
            if len(window) >= self._per_minute:
                return 60.0 - (now - window[0])
            window.append(now)
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


def normalise_client(address: str) -> str:
    """IPv6 clients share a bucket per /64, since one subscriber controls a whole /64."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_key(request: Request, hops: int = 1) -> str:
    """The client address: the ``hops``-th entry from the right of X-Forwarded-For.

    Entries further left are written by the client and can be spoofed. If the header is shorter
    than expected, the request did not come through the expected proxies, so use the socket peer.
    """
    chain = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    if len(chain) >= hops:
        return normalise_client(chain[-hops])
    return normalise_client(request.client.host if request.client else "unknown")


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


async def read_capped(request: Request, limit: int) -> bytes:
    """Read the body, stopping with 413 as soon as it passes ``limit``.

    Counts the bytes as they arrive, so a chunked body with no Content-Length is bounded too.
    """
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _too_large()
    return bytes(body)


async def read_input(request: Request, settings: CheckSettings) -> CheckInput:
    """Parse and validate the request body: JSON ``{text}`` / ``{url}``, or multipart ``image``."""
    limit = settings.check_max_input_chars

    if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raw = await read_capped(request, settings.check_max_image_bytes + 65_536)  # + form overhead
        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": raw, "more_body": False}

        form = await Request(request.scope, replay).form()
        try:
            upload = form.get("image")
            if not isinstance(upload, UploadFile):
                raise CheckError(422, "invalid_input", "Send an image file in the 'image' field.")
            data = await upload.read(settings.check_max_image_bytes + 1)
        finally:
            await form.close()
        if len(data) > settings.check_max_image_bytes:
            raise _too_large()
        mime = sniff_image_mime(data)
        if mime is None:
            raise CheckError(422, "unsupported_image", "Upload a PNG, JPEG or WebP image.")
        return CheckInput(kind="image", image=(data, mime))

    raw = await read_capped(request, limit * 4 + 1024)  # UTF-8 uses at most 4 bytes per character
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError) as exc:  # RecursionError: absurdly nested JSON
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
    problem = url_problem(url)
    if problem == "social":
        raise CheckError(422, "unsupported_url", "Social media pages are not fetched. Paste the text instead.")
    if problem:
        raise CheckError(422, "invalid_input", "That URL cannot be checked. Use a plain http(s) page address.")
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
    return {k: report[k] for k in ("report_id", "claims", "matched_event_id", "report")}


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

    # Registered before CORS on purpose: middleware added later wraps earlier ones, so this sits
    # inside CORS and an unexpected error still comes back as JSON with CORS headers the browser
    # can read (the server-error handler Starlette adds outside CORS would not).
    @app.middleware("http")
    async def _catch_all(request: Request, call_next: Callable) -> Any:
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            _log("internal_error", level=logging.ERROR, reason=type(exc).__name__)
            return _error(500, "internal_error", "Something went wrong. Please try again.")

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
        retry = limiter.check(client_key(request, settings.trusted_proxy_hops))
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

        try:
            async with asyncio.timeout(settings.check_request_timeout_s):
                analysis = await analyse(inp, gateway=gateway, fetcher=fetcher, event=event,
                                         budget=budget, now=clock,
                                         max_chars=settings.check_max_input_chars)
        except TimeoutError as exc:
            raise CheckError(504, "timeout", "The check took too long. Please try again.") from exc
        if analysis.notice:
            return JSONResponse({"report_id": None, "claims": [], "matched_event_id": None,
                                 "notice": analysis.notice})

        report = {
            "report_id": uuid.uuid4().hex,
            "claims": analysis.cards,
            "matched_event_id": event.event_id if analysis.matched else None,
            "report": analysis.report,
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
        report = None
        if REPORT_ID_RE.match(report_id):
            try:
                report = await asyncio.to_thread(store.get, report_id)
            except Exception as exc:  # noqa: BLE001 - a store outage is a 503, not a crash
                _log("store_failed", level=logging.ERROR, reason=type(exc).__name__)
                raise CheckError(503, "store_unavailable", "Reports are temporarily unavailable.") from exc
        if report is None:
            raise CheckError(404, "not_found", "No such report.")
        return JSONResponse({k: report.get(k) for k in
                             ("report_id", "claims", "matched_event_id", "report", "input_type",
                              "created_at")})

    return app
