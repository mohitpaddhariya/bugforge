"""API-side telemetry: contextvars, middleware, SQL listeners, ``emit()``.

Implements spec §6.1, §6.2 and §6.4 for the ``api`` service.

What this module records
------------------------
``request``   method, route template, status, duration_ms, user_id, plus the
              top-level keys of a JSON response body (``response_keys``, capped
              at 30) — the shortcut that makes BUG-003 diagnosable.
``sql``       one event per statement via SQLAlchemy ``before_cursor_execute`` /
              ``after_cursor_execute``, with redacted params and duration_ms.
``business``  explicit domain events emitted by routers via :func:`emit`.
``error``     exception type, message and the FULL traceback, with ``file``,
              ``line`` and ``function`` pointing at the innermost *application*
              frame. The robot reads these fields directly — they are exact.

Guarantees
----------
* Telemetry never blocks a request: every event goes onto a bounded in-memory
  queue and a daemon worker thread batches them to
  ``POST http://collector:8001/ingest``.
* A collector outage never breaks the API: failed batches (and queue overflow)
  are dropped silently and the worker keeps going.
* ``/api/debug/*`` is excluded from telemetry entirely — no request, sql, error
  or business events — so harness control-plane calls never pollute the
  timeline the robot is reading.

Wiring (from ``main.py``)::

    from app.db import engine
    from app import telemetry

    app = FastAPI()
    telemetry.install(app, engine)
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import threading
import time
import traceback
import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.engine import Engine
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

SOURCE = "api"

COLLECTOR_URL: str = os.getenv("COLLECTOR_URL", "http://collector:8001").rstrip("/")
INGEST_URL: str = f"{COLLECTOR_URL}/ingest"

#: Routes under these prefixes are excluded from telemetry entirely (spec §5).
EXCLUDED_PREFIXES: tuple[str, ...] = ("/api/debug",)

#: Max events held in memory before new ones are dropped.
QUEUE_MAX: int = int(os.getenv("TELEMETRY_QUEUE_MAX", "10000"))
#: Max events per POST to the collector.
BATCH_MAX: int = int(os.getenv("TELEMETRY_BATCH_MAX", "100"))
#: Max seconds a partial batch waits before being flushed.
BATCH_INTERVAL: float = float(os.getenv("TELEMETRY_BATCH_INTERVAL", "1.0"))
#: HTTP timeout for a single ingest POST.
POST_TIMEOUT: float = float(os.getenv("TELEMETRY_POST_TIMEOUT", "2.0"))
#: Telemetry can be switched off wholesale (used by unit tests / seed scripts).
ENABLED: bool = os.getenv("TELEMETRY_ENABLED", "1").lower() not in {"0", "false", "no"}

#: Max top-level response keys recorded on a request event.
RESPONSE_KEYS_MAX: int = 30
#: Longest SQL statement recorded verbatim.
SQL_STATEMENT_MAX: int = 4000
#: Longest single stringified SQL parameter.
PARAM_VALUE_MAX: int = 200
#: Longest single stringified attribute value (a full traceback must fit).
ATTR_VALUE_MAX: int = 20000
#: Longest traceback recorded on an ``error`` event.
TRACEBACK_MAX: int = 20000
#: Response bodies larger than this are not parsed for response_keys.
RESPONSE_PARSE_MAX_BYTES: int = 2 * 1024 * 1024

#: Parameter names whose values are replaced with ``"[redacted]"``.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "password_hash",
        "pwd",
        "secret",
        "token",
        "sf_session",
        "session_token",
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "cookie",
        "credit_card",
        "card_number",
        "cvv",
    }
)

#: Directory that counts as "application code" when picking the innermost frame.
APP_ROOT: str = os.path.abspath(
    os.getenv("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)


# --------------------------------------------------------------------------- #
#  Context — one binding per request (spec §6.1)
# --------------------------------------------------------------------------- #

#: The context holds a *mutable dict*, not scalars, on purpose.
#:
#: Starlette's ``BaseHTTPMiddleware`` runs the endpoint in a child task with a
#: **copy** of the context, so a ``ContextVar.set()`` performed inside a route
#: handler (e.g. the auth dependency binding ``user_id``) would be invisible to
#: the middleware afterwards. Both contexts reference the same dict object, so
#: mutating it propagates in every direction.
_state: ContextVar[dict[str, Any] | None] = ContextVar("bugforge_state", default=None)


def _new_state(
    trace_id: str | None = None,
    session_id: str | None = None,
    user_id: int | None = None,
    excluded: bool = False,
    route: str | None = None,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "session_id": session_id,
        "user_id": user_id,
        "excluded": excluded,
        "route": route,
    }


def _state_read() -> dict[str, Any]:
    """Current context state, or an empty one when running outside a request."""
    return _state.get() or _EMPTY_STATE


def _state_write() -> dict[str, Any]:
    """Current context state, creating one if this context has none."""
    state = _state.get()
    if state is None:
        state = _new_state()
        _state.set(state)
    return state


_EMPTY_STATE: dict[str, Any] = _new_state()


def new_trace_id() -> str:
    """Mint a trace id in the same shape the web tracker uses."""
    return f"t_{uuid.uuid4().hex[:12]}"


def current_trace_id() -> str | None:
    """The trace id bound to the in-flight request, if any."""
    return _state_read().get("trace_id")


def current_session_id() -> str | None:
    """The browser session id bound to the in-flight request, if any."""
    return _state_read().get("session_id")


def current_user_id() -> int | None:
    """The authenticated user id bound to the in-flight request, if any."""
    return _state_read().get("user_id")


def current_route() -> str | None:
    """The matched route template of the in-flight request, if known."""
    return _state_read().get("route")


def set_user(user_id: int | None) -> None:
    """Bind the authenticated user to the current request context.

    Auth dependencies call this once the session cookie has been resolved so
    every later event in the request (sql, business, error, and the closing
    ``request`` event) carries ``user_id``.
    """
    state = _state_write()
    try:
        state["user_id"] = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        state["user_id"] = None


def bind(
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    user_id: int | None = None,
) -> None:
    """Bind ids explicitly. Used by scripts (ghost runs) outside a request."""
    state = _state_write()
    if trace_id is not None:
        state["trace_id"] = trace_id
    if session_id is not None:
        state["session_id"] = session_id
    if user_id is not None:
        set_user(user_id)


def is_excluded_path(path: str) -> bool:
    """True when the path is part of the harness control plane (``/api/debug``)."""
    return any(path.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #


def _iso(ts: datetime) -> str:
    """UTC ISO8601 with millisecond precision, e.g. ``2026-08-08T12:04:22.118Z``."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def _now_iso() -> str:
    """Current UTC timestamp in the wire format the collector expects."""
    return _iso(datetime.now(timezone.utc))


def _jsonable(value: Any, depth: int = 0, limit: int = ATTR_VALUE_MAX) -> Any:
    """Best-effort conversion of arbitrary values into JSON-safe data.

    ``limit`` caps string length. It is deliberately generous for event attrs
    (a full traceback must survive intact) and tight for SQL bind parameters.
    """
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return str(value)
        return value
    if isinstance(value, str):
        return _truncate(value, limit)
    if depth >= 4:
        return _truncate(repr(value), limit)
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1, limit) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v, depth + 1, limit) for v in list(value)[:50]]
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return _truncate(repr(value), limit)


def _truncate(text: str, limit: int = ATTR_VALUE_MAX) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def redact_params(params: Any) -> Any:
    """Redact secrets out of SQL bind parameters, then make them JSON-safe."""
    if params is None:
        return None
    if isinstance(params, dict):
        out: dict[str, Any] = {}
        for key, value in list(params.items())[:50]:
            name = str(key)
            if name.lower() in REDACTED_KEYS:
                out[name] = "[redacted]"
            else:
                out[name] = _jsonable(value, 1, PARAM_VALUE_MAX)
        return out
    if isinstance(params, (list, tuple)):
        # executemany() hands us a sequence of parameter sets.
        return [redact_params(item) for item in list(params)[:10]]
    return _jsonable(params, 1, PARAM_VALUE_MAX)


# --------------------------------------------------------------------------- #
#  Delivery — bounded queue + batching worker thread
# --------------------------------------------------------------------------- #


class _Shipper:
    """Fire-and-forget event delivery to ``collector /ingest``.

    Every failure mode is a no-op: a full queue drops the event, a failed POST
    drops the batch. Telemetry must never block or break a request.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._client: httpx.Client | None = None
        self.dropped = 0
        self.sent = 0
        self.failed_batches = 0

    # -- lifecycle -------------------------------------------------------- #

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._run, name="bugforge-telemetry", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    # -- producer --------------------------------------------------------- #

    def put(self, event: dict[str, Any]) -> None:
        if not ENABLED:
            return
        self.start()
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1
        except Exception:
            self.dropped += 1

    # -- consumer --------------------------------------------------------- #

    def _run(self) -> None:
        while True:
            batch = self._drain()
            if batch:
                self._post(batch)
            elif self._stopping.is_set():
                return

    def _drain(self) -> list[dict[str, Any]]:
        """Block for one event, then gather up to ``BATCH_MAX`` for ``BATCH_INTERVAL``."""
        batch: list[dict[str, Any]] = []
        try:
            batch.append(self._queue.get(timeout=0.25))
        except queue.Empty:
            return batch
        deadline = time.monotonic() + BATCH_INTERVAL
        while len(batch) < BATCH_MAX:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(self._queue.get(timeout=remaining))
            except queue.Empty:
                break
        return batch

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=POST_TIMEOUT)
        return self._client

    def _post(self, batch: list[dict[str, Any]]) -> None:
        try:
            response = self._http().post(INGEST_URL, json={"events": batch})
            if response.status_code >= 400:
                self.failed_batches += 1
            else:
                self.sent += len(batch)
        except Exception:
            # Collector down / DNS gone / timeout — drop the batch and move on.
            self.failed_batches += 1
            try:
                client, self._client = self._client, None
                if client is not None:
                    client.close()
            except Exception:
                self._client = None

    def flush(self, timeout: float = 2.0) -> None:
        """Best-effort synchronous drain (used at shutdown and by ghost runs)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            batch: list[dict[str, Any]] = []
            while len(batch) < BATCH_MAX:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if not batch:
                return
            self._post(batch)

    def stats(self) -> dict[str, int]:
        return {
            "queued": self._queue.qsize(),
            "sent": self.sent,
            "dropped": self.dropped,
            "failed_batches": self.failed_batches,
        }


_shipper = _Shipper()


def _shutdown() -> None:
    try:
        _shipper.flush(timeout=1.5)
        _shipper.stop(timeout=1.5)
    except Exception:
        pass


atexit.register(_shutdown)


def flush(timeout: float = 2.0) -> None:
    """Drain the queue synchronously. Never raises."""
    try:
        _shipper.flush(timeout=timeout)
    except Exception:
        pass


def stats() -> dict[str, int]:
    """Delivery counters — handy from ``/api/debug`` or a test."""
    return _shipper.stats()


# --------------------------------------------------------------------------- #
#  emit() — the single entry point for every api-side event (spec §6.2)
# --------------------------------------------------------------------------- #

VALID_KINDS: frozenset[str] = frozenset(
    {"click", "nav", "fetch", "console", "error", "request", "sql", "business", "vitals"}
)
VALID_LEVELS: frozenset[str] = frozenset({"debug", "info", "warn", "error"})


def emit(
    kind: str,
    name: str,
    level: str = "info",
    duration_ms: float | None = None,
    **attrs: Any,
) -> None:
    """Queue one telemetry event. Fire-and-forget; never raises, never blocks.

    ``emit("business", "coupon_applied", code="SAVE20", uses=5, max_uses=5)``
    """
    if not ENABLED:
        return
    try:
        state = _state_read()
        if state.get("excluded"):
            return
        event_payload: dict[str, Any] = {
            "ts": _now_iso(),
            "trace_id": state.get("trace_id"),
            "session_id": state.get("session_id"),
            "user_id": state.get("user_id"),
            "source": SOURCE,
            "kind": kind if kind in VALID_KINDS else "business",
            "name": str(name),
            "level": level if level in VALID_LEVELS else "info",
            "duration_ms": round(float(duration_ms), 3) if duration_ms is not None else None,
            "attrs": _jsonable(attrs) if attrs else {},
        }
        route = state.get("route")
        if route and "route" not in event_payload["attrs"]:
            event_payload["attrs"]["route"] = route
        _shipper.put(event_payload)
    except Exception:
        # Telemetry failures are always swallowed.
        return


def business(name: str, level: str = "info", duration_ms: float | None = None, **attrs: Any) -> None:
    """Shorthand for ``emit("business", ...)`` used by routers."""
    emit("business", name, level=level, duration_ms=duration_ms, **attrs)


# --------------------------------------------------------------------------- #
#  Exception capture (spec §6.4) — file:line of the innermost app frame
# --------------------------------------------------------------------------- #


#: This module must never be reported as the culprit: it wraps every request,
#: so its own frame is present in essentially every traceback.
_SELF_FILE: str = os.path.abspath(__file__)


def _is_library_frame(filename: str) -> bool:
    """True for third-party / stdlib frames — never the answer to 'where is the bug'."""
    if not filename or filename.startswith("<"):
        return True
    path = os.path.abspath(filename)
    for marker in ("site-packages", "dist-packages", "lib-dynload", "importlib"):
        if os.sep + marker + os.sep in path or path.endswith(os.sep + marker):
            return True
    for prefix in (
        os.path.abspath(os.path.join(os.__file__, os.pardir)),  # stdlib dir
    ):
        if path.startswith(prefix + os.sep):
            return True
    return False


def _is_app_frame(filename: str) -> bool:
    """True for ShopForge application code (this module excluded)."""
    if not filename:
        return False
    path = os.path.abspath(filename)
    if path == _SELF_FILE or _is_library_frame(path):
        return False
    if path.startswith(APP_ROOT + os.sep) or path == APP_ROOT:
        return True
    # Container layout: the api package is mounted at /app.
    return path.startswith("/app" + os.sep)


def _frame_summary(exc: BaseException) -> dict[str, Any]:
    """Locate the frame the robot should open.

    Preference order, innermost first at every tier:
      1. application code (under ``APP_ROOT`` / ``/app``, excluding this module)
      2. any non-library frame
      3. the innermost frame, whatever it is
    """
    tb = exc.__traceback__
    frames = traceback.extract_tb(tb) if tb is not None else []
    chosen = None
    for frame in frames:  # innermost application frame wins
        if _is_app_frame(frame.filename):
            chosen = frame
    if chosen is None:
        # A DBAPI error raised entirely inside the driver has no application
        # frame in its traceback. The code that *issued* the statement is on
        # the live call stack, and that is the file the robot needs to open.
        try:
            for frame in traceback.extract_stack():
                if _is_app_frame(frame.filename):
                    chosen = frame
        except Exception:
            chosen = None
    if chosen is None:
        for frame in frames:
            if not _is_library_frame(frame.filename) and os.path.abspath(
                frame.filename
            ) != _SELF_FILE:
                chosen = frame
    if chosen is None and frames:
        chosen = frames[-1]
    if chosen is None:
        return {
            "file": None,
            "line": None,
            "function": None,
            "code": None,
            "origin_file": None,
            "origin_line": None,
            "frames": [],
        }
    innermost = frames[-1] if frames else chosen
    return {
        "file": chosen.filename,
        "line": chosen.lineno,
        "function": chosen.name,
        "code": (chosen.line or "").strip() or None,
        "origin_file": innermost.filename,
        "origin_line": innermost.lineno,
        "frames": [
            {
                "file": f.filename,
                "line": f.lineno,
                "function": f.name,
                "code": (f.line or "").strip() or None,
                "app": _is_app_frame(f.filename),
            }
            for f in frames[-25:]
        ],
    }


def capture_exception(exc: BaseException, name: str | None = None, **attrs: Any) -> None:
    """Emit an ``error`` event with the full traceback and exact file:line.

    ``attrs`` always contains ``file``, ``line``, ``function`` and
    ``traceback`` — the robot reads those fields verbatim.
    """
    if not ENABLED:
        return
    try:
        summary = _frame_summary(exc)
        formatted = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        payload: dict[str, Any] = {
            "exception_type": type(exc).__name__,
            "exception_module": type(exc).__module__,
            "message": _truncate(str(exc), 2000),
            "file": summary["file"],
            "line": summary["line"],
            "function": summary["function"],
            "code": summary.get("code"),
            "origin_file": summary.get("origin_file"),
            "origin_line": summary.get("origin_line"),
            "traceback": formatted[-TRACEBACK_MAX:],
            "frames": summary["frames"],
        }
        payload.update(attrs)
        emit(
            "error",
            name or type(exc).__name__,
            level="error",
            **payload,
        )
    except Exception:
        return


# --------------------------------------------------------------------------- #
#  Response shape capture (spec §6.4) — needed for BUG-003
# --------------------------------------------------------------------------- #


def _looks_json(content_type: str | None) -> bool:
    if not content_type:
        return False
    ct = content_type.split(";", 1)[0].strip().lower()
    return ct == "application/json" or ct.endswith("+json")


def response_keys(body: bytes, content_type: str | None) -> list[str] | None:
    """Top-level keys of a JSON response body, truncated to 30.

    Lists return the keys of their first object element (that is what the web
    app destructures), prefixed so the shape is unambiguous.
    """
    if not _looks_json(content_type) or not body or len(body) > RESPONSE_PARSE_MAX_BYTES:
        return None
    try:
        parsed = json.loads(body)
    except Exception:
        return None
    if isinstance(parsed, dict):
        return [str(k) for k in list(parsed.keys())[:RESPONSE_KEYS_MAX]]
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                return [f"[].{k}" for k in list(item.keys())[:RESPONSE_KEYS_MAX]]
        return ["[]"]
    return [f"<{type(parsed).__name__}>"]


# --------------------------------------------------------------------------- #
#  Middleware (spec §6.1, §6.4)
# --------------------------------------------------------------------------- #


class TelemetryMiddleware(BaseHTTPMiddleware):
    """Binds trace/session/user contextvars and emits the ``request`` event."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        excluded = is_excluded_path(path)

        incoming_trace = request.headers.get("x-trace-id")
        incoming_session = request.headers.get("x-session-id")
        trace_id = (incoming_trace or "").strip() or new_trace_id()
        session_id = (incoming_session or "").strip() or None

        state = _new_state(
            trace_id=trace_id,
            session_id=session_id,
            excluded=excluded,
            route=None,
        )
        token: Token[Any] = _state.set(state)

        started = time.perf_counter()

        try:
            try:
                raw_response = await call_next(request)
            except Exception as exc:  # unhandled — capture, then re-raise
                duration_ms = (time.perf_counter() - started) * 1000.0
                if not excluded:
                    route = _route_template(request, path)
                    state["route"] = route
                    capture_exception(
                        exc,
                        method=request.method,
                        route=route,
                        path=path,
                        handled=False,
                    )
                    self._emit_request(
                        request=request,
                        route=route,
                        path=path,
                        status=500,
                        duration_ms=duration_ms,
                        state=state,
                        body=b"",
                        content_type=None,
                        incoming_trace=incoming_trace,
                        exception=type(exc).__name__,
                    )
                raise

            content_type = raw_response.headers.get("content-type")

            chunks: list[bytes] = []
            async for chunk in raw_response.body_iterator:  # type: ignore[attr-defined]
                chunks.append(bytes(chunk))
            body = b"".join(chunks)

            headers = dict(raw_response.headers)
            headers.pop("content-length", None)
            response = Response(
                content=body,
                status_code=raw_response.status_code,
                headers=headers,
                media_type=raw_response.media_type,
            )
            response.headers["content-length"] = str(len(body))
            response.headers["X-Trace-Id"] = trace_id
            if raw_response.background is not None:
                response.background = raw_response.background

            duration_ms = (time.perf_counter() - started) * 1000.0

            if not excluded:
                route = _route_template(request, path)
                state["route"] = route
                self._emit_request(
                    request=request,
                    route=route,
                    path=path,
                    status=raw_response.status_code,
                    duration_ms=duration_ms,
                    state=state,
                    body=body,
                    content_type=content_type,
                    incoming_trace=incoming_trace,
                )
            return response
        finally:
            try:
                _state.reset(token)
            except Exception:
                pass

    @staticmethod
    def _emit_request(
        *,
        request: Request,
        route: str,
        path: str,
        status: int,
        duration_ms: float,
        state: dict[str, Any],
        body: bytes,
        content_type: str | None,
        incoming_trace: str | None,
        exception: str | None = None,
    ) -> None:
        attrs: dict[str, Any] = {
            "method": request.method,
            "route": route,
            "path": path,
            "status": status,
            "user_id": state.get("user_id"),
            "query": _truncate(request.url.query or "", 500) or None,
            "response_bytes": len(body),
            "content_type": content_type,
            "trace_id_source": "header" if incoming_trace else "minted",
        }
        if exception is not None:
            attrs["exception"] = exception
        keys = response_keys(body, content_type)
        if keys is not None:
            attrs["response_keys"] = keys
        level = "error" if status >= 500 else ("warn" if status >= 400 else "info")
        emit(
            "request",
            f"{request.method} {route}",
            level=level,
            duration_ms=duration_ms,
            **attrs,
        )


def _route_template(request: Request, fallback: str) -> str:
    """The matched route template (``/api/orders/{id}``), not the concrete path."""
    try:
        route = request.scope.get("route")
        if route is not None:
            template = getattr(route, "path_format", None) or getattr(route, "path", None)
            if template:
                return str(template)
        path_params = request.scope.get("path_params") or {}
        if path_params:
            template = fallback
            for key, value in path_params.items():
                if value is None:
                    continue
                template = template.replace(str(value), "{" + str(key) + "}")
            return template
    except Exception:
        pass
    return fallback


# --------------------------------------------------------------------------- #
#  SQLAlchemy listeners (spec §6.4)
# --------------------------------------------------------------------------- #

_listeners_installed: set[int] = set()


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
    try:
        conn.info.setdefault("_bugforge_sql_start", []).append(time.perf_counter())
    except Exception:
        pass


def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
    try:
        stack = conn.info.get("_bugforge_sql_start") or []
        started = stack.pop() if stack else None
        if not ENABLED or _state_read().get("excluded"):
            return
        duration_ms = (time.perf_counter() - started) * 1000.0 if started else None
        text = " ".join(str(statement).split())
        operation = (text.split(" ", 1)[0] or "SQL").upper()[:16]
        rowcount = None
        try:
            rowcount = int(cursor.rowcount)
        except Exception:
            rowcount = None
        emit(
            "sql",
            operation,
            level="debug",
            duration_ms=duration_ms,
            statement=_truncate(text, SQL_STATEMENT_MAX),
            params=redact_params(parameters),
            executemany=bool(executemany),
            rowcount=rowcount,
        )
    except Exception:
        return


def _handle_dbapi_error(context) -> None:  # noqa: ANN001
    """Capture DB-level failures (e.g. the BUG-001 CHECK violation) precisely."""
    try:
        exc = getattr(context, "original_exception", None) or getattr(
            context, "sqlalchemy_exception", None
        )
        if exc is None or _state_read().get("excluded"):
            return
        statement = getattr(context, "statement", None)
        capture_exception(
            exc,
            name=type(exc).__name__,
            layer="sql",
            statement=_truncate(" ".join(str(statement).split()), SQL_STATEMENT_MAX)
            if statement
            else None,
            params=redact_params(getattr(context, "parameters", None)),
        )
    except Exception:
        return


def install_sql_listeners(engine: Engine) -> None:
    """Attach the SQL event listeners to an engine (idempotent)."""
    key = id(engine)
    if key in _listeners_installed:
        return
    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    event.listen(engine, "after_cursor_execute", _after_cursor_execute)
    event.listen(engine, "handle_error", _handle_dbapi_error)
    _listeners_installed.add(key)


# --------------------------------------------------------------------------- #
#  install() — one call from main.py
# --------------------------------------------------------------------------- #


def install(app: FastAPI, engine: Engine | None = None) -> None:
    """Wire middleware + SQL listeners + exception capture in one call.

    ``telemetry.install(app, engine)`` is the only thing ``main.py`` needs.
    """
    app.add_middleware(TelemetryMiddleware)

    if engine is not None:
        install_sql_listeners(engine)

    def _telemetry_shutdown() -> None:  # pragma: no cover - lifecycle glue
        flush(timeout=1.5)

    # Registered directly on the router: ``@app.on_event`` is deprecated.
    app.router.on_shutdown.append(_telemetry_shutdown)

    _shipper.start()


def install_middleware(app: FastAPI) -> None:
    """Middleware only — for tests that do not want SQL listeners."""
    app.add_middleware(TelemetryMiddleware)


__all__ = [
    "ENABLED",
    "INGEST_URL",
    "TelemetryMiddleware",
    "bind",
    "business",
    "capture_exception",
    "current_route",
    "current_session_id",
    "current_trace_id",
    "current_user_id",
    "emit",
    "flush",
    "install",
    "install_middleware",
    "install_sql_listeners",
    "is_excluded_path",
    "new_trace_id",
    "redact_params",
    "response_keys",
    "set_user",
    "stats",
]
