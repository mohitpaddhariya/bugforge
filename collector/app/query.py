"""The query API — everything the robot reads.

Four endpoints, in the order an investigation actually uses them:

``/telemetry/search``   which session is the ticket about?
``/telemetry/session``  what did that person do, interaction by interaction?
``/telemetry/trace``    what happened when they clicked the thing?
``/telemetry/bundle``   everything needed to form a hypothesis, in one call.

Two design commitments run through the whole module.

**The rendered timeline is the product.** The robot reads ``rendered`` before it
reads anything structured, so every response that contains events also contains
pre-formatted lines in a fixed-width, two-space-separated shape::

    t_9f3a  12:04:22.194  API   ERROR  IntegrityError  checkout.py:94
    └─ 8 ──┘└──── 14 ────┘└─ 6 ┘└ label ┘  └ detail ┘  └ location ┘

**Absence is evidence.** A click followed by no network request is the entire
diagnosis for a frontend-only bug, so the summariser reasons about what is
*missing* as much as what is present, and never requires an error to exist in
order to say something useful.

This module reads only ``telemetry.events``. It touches ``shop.users`` for one
optional convenience — resolving an email to a user id in ``/search`` — and
degrades to a text match when that read fails. It never calls ``api``: killing
``api`` must not stop the collector serving history.
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Text, and_, cast, func, or_, select, text as sql_text
from sqlalchemy.orm import Session

from .models import Event, get_db
from .schemas import (
    BundleResponse,
    ClickTarget,
    ErrorSummary,
    ImplicatedFile,
    RequestSummary,
    ResponseShape,
    SearchResponse,
    SessionMatch,
    SessionResponse,
    StackFrame,
    TimelineEvent,
    TraceResponse,
    TraceSummary,
    parse_when,
    utcnow,
)

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

#: Hard caps. A runaway ghost run must not turn one GET into a 200MB response.
MAX_TIMELINE_EVENTS = 2000
MAX_SESSION_EVENTS = 6000
MAX_SEARCH_SESSIONS = 100
DEFAULT_SEARCH_SESSIONS = 10
MAX_PRECEDING_ACTIONS = 40
MAX_PRECEDING_TRACES = 6

# Rendered-line column widths.
_W_TRACE = 8
_W_TIME = 14
_W_SOURCE = 6

_UTC = timezone.utc


# --------------------------------------------------------------------------- #
#  attrs plumbing
# --------------------------------------------------------------------------- #


def _attrs(event: Event) -> dict[str, Any]:
    value = event.attrs
    return value if isinstance(value, dict) else {}


def _pick(source: dict[str, Any], *keys: str) -> Any:
    """First present, non-empty value among ``keys``."""
    for key in keys:
        if key in source:
            value = source[key]
            if value is not None and value != "" and value != [] and value != {}:
                return value
    return None


def _dig(source: Any, *path: str) -> Any:
    cursor = source
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
    return cursor


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC).isoformat()


def _clock(value: datetime | None) -> str:
    """``12:04:22.194`` — the timeline's time column."""
    if value is None:
        return "--:--:--.---"
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    value = value.astimezone(_UTC)
    return f"{value:%H:%M:%S}.{value.microsecond // 1000:03d}"


def _ms_between(start: datetime | None, end: datetime | None) -> float:
    if not start or not end:
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=_UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=_UTC)
    return round((end - start).total_seconds() * 1000.0, 3)


def _truncate(value: Any, limit: int) -> str:
    text_value = " ".join(str(value).split())
    return text_value if len(text_value) <= limit else text_value[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
#  HTTP-ish accessors — emitters differ, we don't care
# --------------------------------------------------------------------------- #

_METHOD_IN_NAME = re.compile(
    r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)", re.IGNORECASE
)

_HTTP_KINDS = ("fetch", "request")


def _http_method(event: Event) -> str | None:
    attrs = _attrs(event)
    value = _pick(attrs, "method", "http_method", "verb")
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    match = _METHOD_IN_NAME.match(event.name or "")
    return match.group(1).upper() if match else None


def _http_url(event: Event) -> str | None:
    attrs = _attrs(event)
    value = _pick(attrs, "url", "path", "route", "target", "endpoint")
    if isinstance(value, str) and value.strip():
        return value.strip()
    match = _METHOD_IN_NAME.match(event.name or "")
    return match.group(2) if match else None


def _http_route(event: Event) -> str | None:
    attrs = _attrs(event)
    # A web `fetch` event stamps `route` with the PAGE route the call was made
    # from ("/checkout"), not the request's own path ("/api/checkout"). Reading
    # `route` first there renders three different calls as the same line and
    # hides which endpoint the browser actually hit. `path` is the request path.
    if (event.kind or "") == "fetch" or (event.source or "") == "web":
        keys = ("path", "path_template", "endpoint")
    else:
        keys = ("route", "path_template", "endpoint", "path")
    value = _pick(attrs, *keys)
    if isinstance(value, str) and value.strip():
        return value.strip()
    url = _http_url(event)
    if not url:
        return None
    without_origin = re.sub(r"^[a-zA-Z][\w+.-]*://[^/]+", "", url)
    return without_origin.split("?")[0] or url


def _http_status(event: Event) -> int | None:
    attrs = _attrs(event)
    value = _pick(attrs, "status", "status_code", "statusCode", "response_status", "http_status")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _duration(event: Event) -> float | None:
    if event.duration_ms is not None:
        return event.duration_ms
    attrs = _attrs(event)
    value = _pick(attrs, "duration_ms", "duration", "elapsed_ms", "ms")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _is_error(event: Event) -> bool:
    """Is this an *error event*, as opposed to a failed request?

    A ``fetch``/``request`` event carrying ``level=error`` is a failed HTTP
    call: it is already fully described by ``requests[].status``. Counting it
    here too would double-report every 500 and would fill the errors list with
    entries whose "exception" is really a URL. Failed requests are surfaced
    through ``requests``; this predicate is for exceptions and console errors.
    """
    kind = event.kind or ""
    if kind == "error":
        return True
    return (event.level or "").lower() == "error" and kind not in _HTTP_KINDS


def _exception_name(event: Event) -> str | None:
    attrs = _attrs(event)
    value = _pick(
        attrs,
        "exception",
        "exc_type",
        "exception_type",
        "type",
        "error_type",
        "class",
        "name",
    )
    if isinstance(value, dict):
        value = _pick(value, "type", "name", "class")
    if isinstance(value, str) and value.strip():
        return value.strip()[:120]
    if event.kind == "error" and event.name:
        return event.name[:120]
    return None


def _error_message(event: Event) -> str | None:
    attrs = _attrs(event)
    value = _pick(attrs, "message", "msg", "detail", "error_message", "text", "reason")
    if value is None:
        error = attrs.get("error")
        if isinstance(error, str):
            value = error
        elif isinstance(error, dict):
            value = _pick(error, "message", "msg", "detail")
    if isinstance(value, (int, float, bool)):
        value = str(value)
    return _truncate(value, 300) if isinstance(value, str) and value.strip() else None


# --------------------------------------------------------------------------- #
#  Stack frames
# --------------------------------------------------------------------------- #

_PY_FRAME_RE = re.compile(
    r'File "(?P<file>[^"\n]+)", line (?P<line>\d+)(?:, in (?P<func>[^\n]+))?'
)
_JS_FRAME_RE = re.compile(
    r"(?:at\s+)?(?:(?P<func>[\w$.<>\[\]]+)\s+\()?"
    r"(?P<file>(?:[a-zA-Z][\w+.-]*:\/\/[^\s()]+|[^\s():]+))"
    r":(?P<line>\d+)(?::(?P<col>\d+))?\)?"
)

_NON_APP_MARKERS = (
    "site-packages",
    "dist-packages",
    "node_modules",
    "/usr/lib/python",
    "/usr/local/lib/python",
    "<frozen",
    "<string>",
    "<anonymous>",
    "/next/dist/",
    "/.venv/",
    "internal/process",
)

_STACK_TEXT_KEYS = (
    "traceback",
    "stack",
    "stacktrace",
    "stack_trace",
    "trace",
    "tb",
)


def _strip_origin(path: str) -> str:
    without_scheme = re.sub(r"^[a-zA-Z][\w+.-]*:\/\/[^\/]+", "", path)
    without_scheme = re.sub(r"^webpack-internal:\/{2,}", "", without_scheme)
    without_scheme = re.sub(r"^\((?:rsc|ssr|app-pages-browser)\)\/", "", without_scheme)
    return without_scheme.split("?")[0].split("#")[0]


def _repo_path(raw: str, source: str) -> str:
    """Map a runtime path back onto a repo path where we can do so honestly.

    The compose file mounts ``./api/app`` at ``/srv/app`` and ``./web`` at
    ``/srv``. The answer sheets are written in repo terms
    (``api/app/routers/checkout.py``), so translating here is what lets a stack
    frame line up with a file the robot can open.
    """
    path = _strip_origin(str(raw).strip())
    path = re.sub(r"^\.\/", "", path)
    if not path:
        return str(raw)

    if path.startswith("/srv/"):
        rest = path[len("/srv/") :]
        prefix = "api" if source == "api" else "web"
        if rest.startswith("tests/") and source == "api":
            return f"api/{rest}"
        return f"{prefix}/{rest}"

    if source == "web" and path.startswith("/_next/"):
        return f"web/{path.lstrip('/')}"

    if source == "web" and re.match(r"^(app|lib|components|hooks|src)\/", path):
        return f"web/{path}"

    if source == "api" and re.match(r"^(app|tests)\/", path):
        return f"api/{path}"

    return path.lstrip("/") if path.startswith("/srv") else path


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_app_frame(raw_path: str) -> bool:
    lowered = raw_path.lower()
    return not any(marker in lowered for marker in _NON_APP_MARKERS)


def _frame_from_parts(
    raw_file: Any,
    raw_line: Any,
    function: Any,
    *,
    source: str,
    language: str,
    code: Any = None,
) -> dict[str, Any] | None:
    if raw_file is None:
        return None
    raw_file = str(raw_file).strip()
    if not raw_file:
        return None
    try:
        line = int(str(raw_line).strip()) if raw_line is not None else None
    except (TypeError, ValueError):
        line = None
    path = _repo_path(raw_file, source)
    return {
        "file": path,
        "raw_file": raw_file,
        "line": line,
        "function": str(function).strip()[:120] if function else None,
        "code": _truncate(code, 200) if code else None,
        "app": _is_app_frame(raw_file),
        "location": f"{_basename(path)}:{line}" if line is not None else _basename(path),
        "language": language,
    }


def _frames_from_text(blob: str, source: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    looks_python = 'File "' in blob or "Traceback (most recent call last)" in blob
    if looks_python:
        for match in _PY_FRAME_RE.finditer(blob):
            frame = _frame_from_parts(
                match.group("file"),
                match.group("line"),
                match.group("func"),
                source=source,
                language="python",
            )
            if frame:
                frames.append(frame)
        return frames

    for match in _JS_FRAME_RE.finditer(blob):
        candidate = match.group("file")
        line = match.group("line")
        # `file:line:col`. The URL alternative in the pattern may legitimately
        # contain colons ("webpack-internal:///./lib/api.ts"), so it swallows
        # the line number and leaves the *column* in the `line` group. Shift the
        # numbers back one place, otherwise the frame reports a path of
        # "web/lib/api.ts:88" at line 15 — a file no one can open.
        if match.group("col") is None:
            tail = re.search(r":(\d+)$", candidate)
            if tail:
                candidate, line = candidate[: tail.start()], tail.group(1)
        # Guard against matching "12:04:22" or "SAVE20:5" inside a message.
        if "/" not in candidate and "." not in candidate:
            continue
        frame = _frame_from_parts(
            candidate,
            line,
            match.group("func"),
            source=source,
            language="javascript",
        )
        if frame:
            frames.append(frame)
    return frames


def _frames_from_structured(items: Iterable[Any], source: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            frame = _frame_from_parts(
                _pick(item, "file", "filename", "path", "abs_path", "source"),
                _pick(item, "line", "lineno", "line_number", "lineNumber"),
                _pick(item, "function", "func", "name", "method"),
                source=source,
                language=str(_pick(item, "language", "lang") or "python"),
                code=_pick(item, "code", "context_line", "line_source", "src"),
            )
            if frame:
                frames.append(frame)
        elif isinstance(item, str):
            frames.extend(_frames_from_text(item, source))
    return frames


def extract_frames(event: Event) -> list[dict[str, Any]]:
    """Every stack frame carried by one event, outermost first.

    Looks in the places the three emitters actually put stacks: a structured
    ``frames`` list, a raw ``traceback``/``stack`` string, a nested
    ``error``/``exception`` object, or plain ``file``+``line`` attributes.
    """
    attrs = _attrs(event)
    source = event.source or "web"
    frames: list[dict[str, Any]] = []

    for key in ("frames", "stack_frames", "stackframes", "backtrace"):
        value = attrs.get(key)
        if isinstance(value, list):
            frames.extend(_frames_from_structured(value, source))

    for container_key in ("error", "exception", "exc", "detail"):
        container = attrs.get(container_key)
        if isinstance(container, dict):
            nested = _pick(container, "frames", "stack_frames")
            if isinstance(nested, list):
                frames.extend(_frames_from_structured(nested, source))
            for key in _STACK_TEXT_KEYS:
                blob = container.get(key)
                if isinstance(blob, str) and blob.strip():
                    frames.extend(_frames_from_text(blob, source))
                elif isinstance(blob, list):
                    frames.extend(_frames_from_structured(blob, source))

    for key in _STACK_TEXT_KEYS:
        blob = attrs.get(key)
        if isinstance(blob, str) and blob.strip():
            frames.extend(_frames_from_text(blob, source))
        elif isinstance(blob, list):
            frames.extend(_frames_from_structured(blob, source))

    # The emitter's own verdict on where this happened. ``api/app/telemetry.py``
    # already walks the traceback and stores the innermost *application* frame
    # in ``file``/``line``, so this is more trustworthy than anything reparsed
    # out of the blob — especially for a DBAPI error, whose traceback contains
    # nothing but driver internals, and for a chained traceback, where the
    # textually-last app frame is the middleware that re-raised, not the site.
    direct = _frame_from_parts(
        _pick(attrs, "file", "filename", "path", "source_file"),
        _pick(attrs, "line", "lineno", "line_number", "lineNumber"),
        _pick(attrs, "function", "func"),
        source=source,
        language="python" if source == "api" else "javascript",
    )
    if direct:
        frames.append(direct)

    # De-duplicate while preserving traceback order.
    seen: set[tuple[str, int | None, str | None]] = set()
    unique: list[dict[str, Any]] = []
    for frame in frames:
        key = (frame["file"], frame["line"], frame["function"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(frame)

    # Mark the frame to read first: the emitter's own file/line when it named an
    # application file, otherwise the innermost application frame in the stack.
    chosen: dict[str, Any] | None = None
    if direct and direct["app"] and direct["line"] is not None:
        chosen = next(
            (
                frame
                for frame in unique
                if frame["file"] == direct["file"] and frame["line"] == direct["line"]
            ),
            None,
        )
    if chosen is None:
        chosen = next(
            (frame for frame in reversed(unique) if frame["app"] and frame["line"] is not None),
            None,
        )
    for frame in unique:
        frame["innermost"] = frame is chosen

    return unique


def event_location(event: Event) -> str | None:
    """``checkout.py:94`` — the location column of a rendered error line."""
    frames = extract_frames(event)
    if not frames:
        return None
    for frame in frames:
        if frame["innermost"]:
            return frame["location"]
    for frame in reversed(frames):
        if frame["line"] is not None:
            return frame["location"]
    return frames[-1]["location"]


# --------------------------------------------------------------------------- #
#  Click targets
# --------------------------------------------------------------------------- #


def _element_label(element: dict[str, Any]) -> str | None:
    testid = _pick(element, "testid", "test_id", "data_testid", "dataTestid")
    if isinstance(testid, str) and testid.strip():
        return f"#{testid.strip()}"
    selector = _pick(element, "selector", "css", "path", "css_path")
    if isinstance(selector, str) and selector.strip():
        return _truncate(selector, 80)
    tag = _pick(element, "tag", "tagName", "node")
    klass = _pick(element, "class", "className", "classes")
    if isinstance(klass, list):
        klass = " ".join(str(part) for part in klass)
    if tag:
        return f"<{tag}{'.' + str(klass).split()[0] if klass else ''}>"
    return None


def click_target(event: Event) -> ClickTarget:
    """The intended element, the element actually hit, and whether they differ.

    The mismatch is the whole diagnosis for an invisible-overlay bug, so it is
    computed once here and reused by the renderer, the trace summary and the
    bundle's plain-English summary.
    """
    attrs = _attrs(event)
    intended = _as_dict(
        _pick(attrs, "intended", "intended_target", "intended_element", "target", "element", "expected")
    )
    actual = _as_dict(
        _pick(attrs, "actual", "hit", "hit_target", "hit_element", "actual_target", "received")
    )

    # `web/lib/telemetry.ts` reports an overlay-eaten click as: the layer in
    # `hit_element`, a null `intended_target` (no handler was ever meant to
    # run on the layer), and the control the user was aiming at in
    # `obscured_interactive_element`. Without this, the whole BUG-002 diagnosis
    # — "you clicked the overlay, not the button" — is invisible downstream.
    obscured = _as_dict(
        _pick(attrs, "obscured_interactive_element", "obscured_element", "covered_element")
    )
    blocked = _pick(attrs, "click_blocked_by_overlay", "blocked_by_overlay")
    if obscured and not intended:
        intended = obscured

    # Flat emitters put the intended element straight on attrs.
    if not intended:
        flat = {
            key: attrs[key]
            for key in ("testid", "test_id", "selector", "tag", "class", "text", "id")
            if key in attrs
        }
        intended = flat

    testid = _pick(intended, "testid", "test_id", "data_testid") or _pick(
        attrs, "testid", "test_id", "data_testid"
    )
    selector = _pick(intended, "selector", "css", "css_path") or _pick(attrs, "selector", "css")
    element_text = _pick(intended, "text", "label", "inner_text") or _pick(attrs, "text", "label")
    tag = _pick(intended, "tag", "tagName") or _pick(attrs, "tag", "tagName")

    listener_ran = _pick(attrs, "listener_ran", "listenerRan", "handler_ran")
    default_prevented = _pick(attrs, "default_prevented", "defaultPrevented", "prevented")

    mismatch = False
    if actual and intended:
        intended_label = _element_label(intended)
        actual_label = _element_label(actual)
        mismatch = bool(intended_label and actual_label and intended_label != actual_label)
    if blocked is True:
        mismatch = True

    return ClickTarget(
        blocked_by_overlay=blocked if isinstance(blocked, bool) else None,
        obscured_element=obscured or None,
        ts=_iso(event.ts),
        name=event.name or "",
        testid=str(testid) if testid else None,
        selector=str(selector) if selector else None,
        text=_truncate(element_text, 120) if element_text else None,
        tag=str(tag) if tag else None,
        intended=intended or None,
        actual=actual or None,
        listener_ran=listener_ran if isinstance(listener_ran, bool) else None,
        default_prevented=default_prevented if isinstance(default_prevented, bool) else None,
        hit_mismatch=mismatch,
        rendered=render_line(event),
    )


def _click_descriptor(event: Event) -> str:
    """Stable identity for a click, used to detect retries."""
    attrs = _attrs(event)
    intended = _as_dict(_pick(attrs, "intended", "target", "element")) or attrs
    label = _element_label(intended)
    if label:
        return label
    return event.name or "click"


# --------------------------------------------------------------------------- #
#  Rendering
# --------------------------------------------------------------------------- #


def _kv_from_attrs(attrs: dict[str, Any], *, limit: int = 4) -> str:
    """``code=SAVE20 uses=4/5`` — the scalar attrs of a business event."""
    skip = {
        "trace_id",
        "session_id",
        "user_id",
        "ts",
        "level",
        "source",
        "kind",
        "name",
        "traceback",
        "stack",
        "frames",
        "duration_ms",
    }
    parts: list[str] = []
    for key, value in attrs.items():
        if key in skip or key.startswith("_"):
            continue
        if isinstance(value, (dict, list)):
            continue
        if value is None:
            continue
        parts.append(f"{key}={_truncate(value, 60)}")
        if len(parts) >= limit:
            break
    return " ".join(parts)


def describe(event: Event) -> tuple[str, str, str | None]:
    """``(label, detail, location)`` — the three trailing columns of a line."""
    attrs = _attrs(event)
    kind = event.kind or "business"
    level = (event.level or "info").lower()

    # An api `request` event at error level is left to the HTTP branch below:
    # `POST /api/checkout → 500` carries more than a second copy of the
    # exception name, and the real `error` event is already on the timeline.
    if kind == "error" or (level == "error" and kind in ("console", "business")):
        if kind == "console":
            return f"console.{level}", f'"{_truncate(_error_message(event) or event.name, 120)}"', None
        exception = _exception_name(event) or event.name or "Error"
        message = _error_message(event)
        detail = exception if not message else f"{exception}  {_truncate(message, 140)}"
        return "ERROR", detail, event_location(event)

    if kind == "click":
        intended = _as_dict(
            _pick(attrs, "intended", "intended_target", "intended_element", "target", "element")
        ) or _as_dict(
            _pick(attrs, "obscured_interactive_element", "obscured_element", "covered_element")
        )
        target = _element_label(intended or attrs)
        detail = target or event.name or "?"
        actual = _as_dict(_pick(attrs, "actual", "hit", "hit_target", "hit_element"))
        if actual:
            actual_label = _element_label(actual)
            if actual_label and actual_label != detail:
                detail = f"{detail}  (hit {actual_label})"
        return "click", detail, None

    if kind == "nav":
        origin = _pick(attrs, "from", "from_route", "previous", "referrer")
        destination = _pick(attrs, "to", "to_route", "route", "url", "path") or event.name
        detail = f"{origin} → {destination}" if origin else str(destination or event.name)
        return "nav", _truncate(detail, 160), None

    if kind in _HTTP_KINDS:
        # web `fetch` renders as `POST /api/checkout → 500`; api `request`
        # renders as `request  POST /api/checkout → 500 user=2`, matching the
        # two sides of the target timeline in §6.5 of the spec.
        method = _http_method(event)
        url = _http_route(event) or _http_url(event) or event.name or "?"
        status_code = _http_status(event)
        duration = _duration(event)
        target = f"{method} {url}" if kind == "request" and method else url
        detail = f"{target} → {status_code if status_code is not None else 'pending'}"
        if duration is not None:
            detail += f" ({duration:.0f}ms)"
        if kind == "request" and event.user_id is not None:
            detail += f" user={event.user_id}"
        return ("request" if kind == "request" else (method or "HTTP")), detail, None

    if kind == "sql":
        statement = _pick(attrs, "statement", "sql", "query", "text") or event.name
        detail = _sql_summary(statement)
        duration = _duration(event)
        if duration is not None:
            detail += f"  ({duration:.0f}ms)"
        return "SQL", detail, None

    if kind == "console":
        return f"console.{level}", f'"{_truncate(_error_message(event) or event.name, 140)}"', None

    if kind == "vitals":
        return "vitals", _truncate(f"{event.name} {_kv_from_attrs(attrs)}".strip(), 160), None

    # business and anything unrecognised
    detail = _kv_from_attrs(attrs)
    return (event.name or kind), detail, None


_SQL_SHAPE_RE = re.compile(
    r"^\s*(?P<op>SELECT|INSERT INTO|UPDATE|DELETE FROM|WITH)\b"
    r"(?P<mid>.*?)(?:\bFROM\b|\bINTO\b)?\s*(?P<table>[\w.]+)?",
    re.IGNORECASE | re.DOTALL,
)


def _sql_summary(statement: str, width: int = 72) -> str:
    """One readable line per query.

    A raw ORM statement is 160+ characters of column aliases and dominates the
    timeline — on screen it buries the two lines that matter. Collapse it to the
    operation, the table, and (for writes) the SET clause, which is where the
    bug usually is.
    """
    if not statement:
        return ""
    flat = " ".join(str(statement).split())
    up = flat.upper()

    if up.startswith("UPDATE"):
        m = re.match(r"UPDATE\s+([\w.]+)\s+SET\s+(.+?)(?:\s+WHERE\b|$)", flat, re.I)
        if m:
            return _truncate(f"UPDATE {m.group(1)} SET {m.group(2)}", width)
    if up.startswith("INSERT"):
        m = re.match(r"INSERT\s+INTO\s+([\w.]+)", flat, re.I)
        if m:
            return f"INSERT INTO {m.group(1)}"
    if up.startswith("DELETE"):
        m = re.match(r"DELETE\s+FROM\s+([\w.]+)", flat, re.I)
        if m:
            return _truncate(f"DELETE FROM {m.group(1)}{_where_of(flat)}", width)
    if up.startswith("SELECT"):
        m = re.search(r"\bFROM\s+([\w.]+)", flat, re.I)
        table = m.group(1) if m else "?"
        lock = " FOR UPDATE" if "FOR UPDATE" in up else ""
        return _truncate(f"SELECT {table}{_where_of(flat)}{lock}", width)
    return _truncate(flat, width)


def _where_of(flat: str) -> str:
    m = re.search(r"\bWHERE\s+(.+?)(?:\s+(?:ORDER|GROUP|LIMIT|RETURNING|FOR)\b|$)",
                  flat, re.I)
    if not m:
        return ""
    cond = " ".join(m.group(1).split())
    return f" WHERE {cond}"


def render_line(event: Event, annotation: str | None = None) -> str:
    """One timeline line.

    ``t_9f3a  12:04:22.194  API   ERROR  IntegrityError  checkout.py:94``
    """
    trace = (event.trace_id or "-")[:_W_TRACE - 1]
    source = (event.source or "?").upper()
    label, detail, location = describe(event)

    parts = [label]
    if detail:
        parts.append(detail)
    if location:
        parts.append(location)

    line = f"{trace:<{_W_TRACE}}{_clock(event.ts):<{_W_TIME}}{source:<{_W_SOURCE}}" + "  ".join(
        parts
    )
    if annotation:
        line += f"  {annotation}"
    return line


def render_timeline(events: Sequence[Event]) -> list[str]:
    """Render a whole timeline, annotating repeated clicks as retries.

    The retry annotation is not decoration: three identical clicks after a
    failed request is how you know the UI gave the customer no feedback, which
    is a second and separate defect from whatever caused the failure.
    """
    seen_clicks: Counter[str] = Counter()
    lines: list[str] = []
    for event in events:
        annotation = None
        if (event.kind or "") == "click":
            descriptor = _click_descriptor(event)
            seen_clicks[descriptor] += 1
            if seen_clicks[descriptor] > 1:
                annotation = "← retried"
        lines.append(render_line(event, annotation))
    return lines


def to_timeline_event(event: Event, rendered: str | None = None) -> TimelineEvent:
    payload = event.as_dict()
    payload["rendered"] = rendered if rendered is not None else render_line(event)
    payload["location"] = event_location(event) if _is_error(event) else None
    return TimelineEvent.model_validate(payload)


# --------------------------------------------------------------------------- #
#  Response payload shapes  (the BUG-003 shortcut)
# --------------------------------------------------------------------------- #

_MONEY_FIELDS = {"total", "subtotal", "tax", "discount", "price", "amount", "unit_price"}

#: Business event names that describe a deliberate refusal rather than a fault.
_REJECTION_NAME_RE = re.compile(
    r"(rejected|declined|denied|refused|invalid|expired|not_eligible|unauthori[sz]ed)"
)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        if not value:
            return "array"
        inner = {_type_name(item) for item in value[:10]}
        return f"array<{inner.pop()}>" if len(inner) == 1 else "array"
    return type(value).__name__


def _shape_of(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _type_name(item) for key, item in list(value.items())[:60]}


def extract_response_shape(event: Event) -> tuple[list[str], dict[str, str], bool] | None:
    """Top-level keys (and their types where known) of a JSON response body.

    ``api`` records this explicitly (§6.4). We also derive it from a captured
    body if one is present, because a shape we computed ourselves is better
    than no shape at all when the client is the one reporting.
    """
    attrs = _attrs(event)

    for key in ("response_shape", "res_shape", "body_shape", "payload_shape"):
        value = attrs.get(key)
        if isinstance(value, dict) and value:
            if all(isinstance(item, str) for item in value.values()):
                return list(value.keys()), {str(k): str(v) for k, v in value.items()}, False
            return list(value.keys()), _shape_of(value), False
        if isinstance(value, list) and value:
            keys = [str(item) for item in value]
            return keys, {key_name: "unknown" for key_name in keys}, False

    for key in ("response_keys", "res_keys", "body_keys", "payload_keys", "keys"):
        value = attrs.get(key)
        if isinstance(value, list) and value:
            keys = [str(item) for item in value]
            return keys, {key_name: "unknown" for key_name in keys}, False

    for key in ("response", "response_body", "body", "payload", "json"):
        value = attrs.get(key)
        if isinstance(value, dict) and value:
            nested_shape = _pick(value, "shape", "response_shape")
            if isinstance(nested_shape, dict) and nested_shape:
                return (
                    list(nested_shape.keys()),
                    {str(k): str(v) for k, v in nested_shape.items()},
                    False,
                )
            nested_keys = _pick(value, "keys", "response_keys")
            if isinstance(nested_keys, list) and nested_keys:
                keys = [str(item) for item in nested_keys]
                return keys, {key_name: "unknown" for key_name in keys}, False
            nested_body = _pick(value, "body", "json", "data")
            if isinstance(nested_body, dict) and nested_body:
                return list(nested_body.keys()), _shape_of(nested_body), False
            return list(value.keys()), _shape_of(value), bool(value.get("_truncated"))

    return None


def collect_response_shapes(events: Sequence[Event]) -> list[ResponseShape]:
    shapes: list[ResponseShape] = []
    for event in events:
        if (event.kind or "") not in _HTTP_KINDS:
            continue
        extracted = extract_response_shape(event)
        if not extracted:
            continue
        keys, shape, truncated = extracted
        shapes.append(
            ResponseShape(
                ts=_iso(event.ts),
                source=event.source or "api",
                method=_http_method(event),
                url=_http_url(event),
                route=_http_route(event),
                status=_http_status(event),
                keys=[str(key) for key in keys][:60],
                shape=shape,
                truncated=truncated,
                event_id=event.id,
            )
        )
    return shapes


def _money_drift(shapes: Sequence[ResponseShape]) -> list[str]:
    """Spot a money field that lost its ``_cents`` suffix.

    Money is integer cents everywhere by contract. A response advertising
    ``total`` where the client reads ``total_cents`` is a contract drift, and
    it renders as ``$NaN`` rather than as an error — nothing else in telemetry
    will flag it.
    """
    signals: list[str] = []
    for shape in shapes:
        keys = set(shape.keys)
        for field in sorted(_MONEY_FIELDS):
            if field in keys and f"{field}_cents" not in keys:
                where = shape.route or shape.url or "the response"
                signals.append(
                    f"{where} returned `{field}` with no `{field}_cents` sibling — "
                    f"money is integer cents by contract, so a client reading "
                    f"`{field}_cents` gets undefined."
                )
    return signals


# --------------------------------------------------------------------------- #
#  Session metadata
# --------------------------------------------------------------------------- #

_META_KEYS = (
    "viewport",
    "viewport_w",
    "viewport_h",
    "viewport_width",
    "viewport_height",
    "width",
    "height",
    "user_agent",
    "userAgent",
    "ua",
    "locale",
    "language",
    "device_pixel_ratio",
    "dpr",
    "screen",
    "platform",
    "referrer",
)


def collect_session_meta(events: Sequence[Event]) -> dict[str, Any]:
    """Viewport, user agent, locale — lifted out of wherever they were stamped.

    Width-conditional bugs are invisible without this, so we scan every event
    rather than relying on a dedicated meta event existing.
    """
    meta: dict[str, Any] = {}
    for event in events:
        attrs = _attrs(event)
        for key in _META_KEYS:
            if key in meta:
                continue
            value = attrs.get(key)
            if value is None or value == "" or value == {}:
                continue
            if isinstance(value, (dict, list)):
                meta[key] = value
            else:
                meta[key] = value
        nested = _as_dict(_pick(attrs, "meta", "session_meta", "context"))
        for key, value in nested.items():
            if key in _META_KEYS and key not in meta and value not in (None, "", {}):
                meta[key] = value
    if "viewport" not in meta:
        width = meta.get("viewport_w") or meta.get("viewport_width") or meta.get("width")
        height = meta.get("viewport_h") or meta.get("viewport_height") or meta.get("height")
        if width and height:
            meta["viewport"] = [width, height]
    return meta


def _viewport_width(meta: dict[str, Any]) -> int | None:
    viewport = meta.get("viewport")
    if isinstance(viewport, (list, tuple)) and viewport:
        try:
            return int(viewport[0])
        except (TypeError, ValueError):
            return None
    if isinstance(viewport, dict):
        for key in ("w", "width"):
            if key in viewport:
                try:
                    return int(viewport[key])
                except (TypeError, ValueError):
                    return None
    if isinstance(viewport, str):
        match = re.match(r"^(\d+)\s*[x×]\s*(\d+)$", viewport.strip())
        if match:
            return int(match.group(1))
    for key in ("viewport_w", "viewport_width", "width"):
        value = meta.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


# --------------------------------------------------------------------------- #
#  Summaries
# --------------------------------------------------------------------------- #


def _counts(events: Sequence[Event], attribute: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for event in events:
        counter[str(getattr(event, attribute) or "unknown")] += 1
    return dict(counter.most_common())


def _requests_of(events: Sequence[Event], rendered: Sequence[str]) -> list[RequestSummary]:
    out: list[RequestSummary] = []
    for index, event in enumerate(events):
        if (event.kind or "") not in _HTTP_KINDS:
            continue
        status_code = _http_status(event)
        out.append(
            RequestSummary(
                ts=_iso(event.ts),
                source=event.source or "web",
                method=_http_method(event),
                url=_http_url(event),
                route=_http_route(event),
                status=status_code,
                duration_ms=_duration(event),
                ok=None if status_code is None else 200 <= status_code < 400,
                rendered=rendered[index] if index < len(rendered) else render_line(event),
            )
        )
    return out


def _errors_of(events: Sequence[Event], rendered: Sequence[str]) -> list[ErrorSummary]:
    out: list[ErrorSummary] = []
    for index, event in enumerate(events):
        if not _is_error(event):
            continue
        frames = extract_frames(event)
        chosen = None
        for frame in reversed(frames):
            if frame["app"] and frame["line"] is not None:
                chosen = frame
                break
        if chosen is None and frames:
            chosen = frames[-1]
        out.append(
            ErrorSummary(
                ts=_iso(event.ts),
                source=event.source or "api",
                kind=event.kind or "error",
                name=event.name or "",
                exception=_exception_name(event),
                message=_error_message(event),
                location=chosen["location"] if chosen else None,
                file=chosen["file"] if chosen else None,
                line=chosen["line"] if chosen else None,
                rendered=rendered[index] if index < len(rendered) else render_line(event),
            )
        )
    return out


def _trigger_of(events: Sequence[Event]) -> ClickTarget | None:
    """The interaction that opened the trace: a click if there is one, else a nav."""
    for event in events:
        if (event.kind or "") == "click":
            return click_target(event)
    for event in events:
        if (event.kind or "") == "nav":
            return click_target(event)
    return None


def _headline(
    events: Sequence[Event],
    trigger: ClickTarget | None,
    requests: Sequence[RequestSummary],
    errors: Sequence[ErrorSummary],
) -> str:
    parts: list[str] = []
    if trigger:
        if trigger.testid:
            label = f"#{trigger.testid}"
        else:
            label = trigger.selector or trigger.name or ""
        parts.append(f"click {label}".strip())
    if requests:
        failed = [item for item in requests if item.status is not None and item.status >= 400]
        if failed:
            first = failed[0]
            parts.append(f"{len(requests)} request(s) → {first.status} {first.route or first.url}")
        else:
            parts.append(f"{len(requests)} request(s) ok")
    elif trigger:
        parts.append("no request fired")
    if errors:
        first = errors[0]
        detail = first.exception or first.name or "error"
        parts.append(f"{detail}{' ' + first.location if first.location else ''}")
    if not parts:
        kinds = _counts(events, "kind")
        parts.append(", ".join(f"{count} {kind}" for kind, count in list(kinds.items())[:3]))
    return " → ".join(parts)


def summarise_trace(events: Sequence[Event]) -> TraceSummary:
    """The per-trace summary shape used by ``/session``, ``/search`` and ``/bundle``."""
    if not events:
        return TraceSummary()

    rendered = render_timeline(events)
    trigger = _trigger_of(events)
    requests = _requests_of(events, rendered)
    errors = _errors_of(events, rendered)
    first, last = events[0], events[-1]

    return TraceSummary(
        trace_id=first.trace_id,
        session_id=next((event.session_id for event in events if event.session_id), None),
        user_id=next((event.user_id for event in events if event.user_id is not None), None),
        started_at=_iso(first.ts),
        ended_at=_iso(last.ts),
        duration_ms=_ms_between(first.ts, last.ts),
        event_count=len(events),
        counts_by_kind=_counts(events, "kind"),
        counts_by_source=_counts(events, "source"),
        counts_by_level=_counts(events, "level"),
        error_count=sum(1 for event in events if _is_error(event)),
        trigger=trigger,
        requests=requests,
        errors=errors,
        headline=_headline(events, trigger, requests, errors),
        rendered=rendered,
    )


def _group_by_trace(events: Sequence[Event]) -> "OrderedDict[str, list[Event]]":
    grouped: OrderedDict[str, list[Event]] = OrderedDict()
    for event in events:
        key = event.trace_id or "(no-trace)"
        grouped.setdefault(key, []).append(event)
    return grouped


# --------------------------------------------------------------------------- #
#  Plain-English summary — the sentence the robot reads before anything else
# --------------------------------------------------------------------------- #


def build_summary(
    trace_id: str,
    events: Sequence[Event],
    *,
    trigger: ClickTarget | None,
    requests: Sequence[RequestSummary],
    errors: Sequence[ErrorSummary],
    frames: Sequence[dict[str, Any]],
    shapes: Sequence[ResponseShape],
    meta: dict[str, Any],
) -> tuple[str, list[str], str]:
    """Return ``(summary, signals, verdict)``.

    Entirely rule-based. Every sentence is derived from an observation that is
    also exposed structurally elsewhere in the bundle, so the robot can always
    check the prose against the data.
    """
    if not events:
        return (
            f"No telemetry was recorded for trace {trace_id}.",
            [],
            "no-data",
        )

    sentences: list[str] = []
    signals: list[str] = []

    api_events = [event for event in events if (event.source or "") == "api"]
    web_events = [event for event in events if (event.source or "") == "web"]
    http_events = [event for event in events if (event.kind or "") in _HTTP_KINDS]
    failed = [item for item in requests if item.status is not None and item.status >= 400]
    server_failed = [item for item in failed if item.status is not None and item.status >= 500]
    console_errors = [
        event
        for event in events
        if (event.kind or "") == "console" and (event.level or "") == "error"
    ]
    clicks = [event for event in events if (event.kind or "") == "click"]

    # 1. What the user did.
    if trigger:
        label = (
            f"#{trigger.testid}"
            if trigger.testid
            else (trigger.selector or trigger.text or trigger.name or "an element")
        )
        started = trigger.ts.split("T")[-1][:12] if trigger.ts else "?"
        sentences.append(f"The user clicked {label} at {started}.")

    # 2. Did the click reach the network at all?
    if trigger and not http_events:
        signals.append("click-produced-no-request")
        sentences.append(
            "No network request followed that interaction — nothing reached the API, "
            "so the defect is entirely client-side."
        )
        if trigger.hit_mismatch and trigger.actual:
            actual_label = _element_label(trigger.actual) or "another element"
            extra = []
            for key in ("zIndex", "z_index", "position", "class", "className"):
                if key in trigger.actual:
                    extra.append(f"{key}={trigger.actual[key]}")
            suffix = f" ({', '.join(extra)})" if extra else ""
            signals.append("click-hit-wrong-element")
            sentences.append(
                f"The click landed on {actual_label}{suffix}, not on the intended element — "
                "something is overlaying the control."
            )
        if trigger.listener_ran is False:
            signals.append("no-listener-ran")
            sentences.append("No click handler ran for that element.")
        if trigger.default_prevented is True:
            signals.append("default-prevented")
            sentences.append("The event's default action was prevented.")
        width = _viewport_width(meta)
        if width is not None:
            signals.append(f"viewport-width-{width}")
            sentences.append(
                f"The session's viewport was {width}px wide — reproduce at that width, "
                "this class of failure is usually width-conditional."
            )

    # 2b. An overlay-eaten click is worth reporting even when the trace does
    # contain requests: the interaction window (5s) also catches the page loads
    # that happen around the dead click, and those are not evidence of anything.
    if (
        trigger
        and trigger.hit_mismatch
        and trigger.actual
        and "click-hit-wrong-element" not in signals
    ):
        actual_label = _element_label(trigger.actual) or "another element"
        intended_label = _element_label(trigger.intended or {}) or "the intended element"
        extra = [
            f"{key}={trigger.actual[key]}"
            for key in ("zIndex", "z_index", "position", "class", "classes", "className")
            if key in trigger.actual
        ]
        suffix = f" ({', '.join(extra)})" if extra else ""
        signals.append("click-hit-wrong-element")
        sentences.append(
            f"The click landed on {actual_label}{suffix}, not on {intended_label} — "
            "something is overlaying the control."
        )
        width = _viewport_width(meta)
        if width is not None and f"viewport-width-{width}" not in signals:
            signals.append(f"viewport-width-{width}")
            sentences.append(
                f"The session's viewport was {width}px wide — reproduce at that width, "
                "this class of failure is usually width-conditional."
            )

    # 3. HTTP failures.
    for item in failed[:3]:
        signals.append(f"http-{item.status}")
        duration = f" after {item.duration_ms:.0f}ms" if item.duration_ms is not None else ""
        sentences.append(
            f"{item.method or 'HTTP'} {item.route or item.url} returned {item.status}{duration}."
        )

    # 4. Backend exception with a file and a line.
    for item in errors[:3]:
        if item.source != "api":
            continue
        signals.append(f"exception-{item.exception or item.name}")
        where = f" at {item.location}" if item.location else ""
        file_hint = f" ({item.file})" if item.file and item.location else ""
        message = f' — "{item.message}"' if item.message else ""
        sentences.append(
            f"The API raised {item.exception or item.name}{where}{file_hint}{message}."
        )

    # 5. The state of the world immediately before the failure.
    first_error_ts = next(
        (event.ts for event in events if _is_error(event)),
        None,
    )
    if first_error_ts is not None:
        business_before = [
            event
            for event in events
            if (event.kind or "") == "business" and event.ts and event.ts <= first_error_ts
        ]
        if business_before:
            last_business = business_before[-1]
            detail = _kv_from_attrs(_attrs(last_business))
            signals.append(f"business-{last_business.name}")
            sentences.append(
                f"The last business event before the failure was "
                f"{last_business.name}{' (' + detail + ')' if detail else ''} — that is the "
                "precondition to reproduce."
            )
        sql_before = [
            event
            for event in events
            if (event.kind or "") == "sql" and event.ts and event.ts <= first_error_ts
        ]
        if len(sql_before) >= 2:
            statement = _pick(_attrs(sql_before[-1]), "statement", "sql", "query") or sql_before[
                -1
            ].name
            sentences.append(
                f"The statement immediately before it was: {_truncate(statement, 120)}."
            )

    # 6. Did the user get told?
    if clicks:
        descriptors = Counter(_click_descriptor(event) for event in clicks)
        repeated = [(label, count) for label, count in descriptors.items() if count > 1]
        if repeated:
            label, count = max(repeated, key=lambda pair: pair[1])
            span = _ms_between(clicks[0].ts, clicks[-1].ts) / 1000.0
            signals.append("user-retried")
            sentences.append(
                f"The user clicked {label} {count} times over {span:.1f}s, which means the UI "
                "gave no visible feedback on failure — a second, separate defect from whatever "
                "caused the failure itself."
            )

    # 7. What the browser said.
    for event in console_errors[:2]:
        message = _error_message(event) or event.name
        signals.append("console-error")
        sentences.append(f'The browser logged console.error: "{_truncate(message, 140)}".')

    # 8. Client-side exceptions. Console errors are already covered above.
    for item in errors[:3]:
        if item.source == "api" or item.kind == "console":
            continue
        where = f" at {item.location}" if item.location else ""
        quoted = ' — "{}"'.format(item.message) if item.message else ""
        signals.append(f"web-exception-{item.exception or item.name}")
        sentences.append(f"The browser threw {item.exception or item.name}{where}{quoted}.")

    # 9. Contract drift in the response payload.
    for drift in _money_drift(shapes)[:2]:
        signals.append("money-field-drift")
        sentences.append(drift)

    # 9b. A refusal is not a failure.
    #
    # The single most valuable thing this summary can say is "nothing is
    # broken here". A 4xx with no exception anywhere and an explicit business
    # rejection event is the application declining on purpose. Calling that a
    # defect is how an agent ends up shipping a patch for correct behaviour.
    client_failed = [
        item for item in failed if item.status is not None and 400 <= item.status < 500
    ]
    rejections = [
        event
        for event in events
        if (event.kind or "") == "business"
        and (
            _REJECTION_NAME_RE.search((event.name or "").lower())
            or _pick(_attrs(event), "reason", "rejection_reason", "error_code", "code")
            is not None
        )
    ]
    rejected_by_design = bool(client_failed) and not server_failed and not errors and bool(
        rejections
    )
    if rejected_by_design:
        event = rejections[-1]
        detail = _kv_from_attrs(_attrs(event))
        signals.append("rejected-by-design")
        sentences.append(
            f"The API rejected this deliberately: business event {event.name}"
            f"{' (' + detail + ')' if detail else ''}. A 4xx paired with an explicit "
            "rejection event and no exception anywhere is the application refusing on "
            "purpose, not failing — confirm what the customer expected before treating "
            "this as a defect."
        )

    # 10. Verdict.
    if server_failed or any(item.source == "api" for item in errors):
        verdict = "backend-error"
    elif rejected_by_design:
        verdict = "rejected-by-design"
    elif failed:
        verdict = "http-error"
    elif trigger and not http_events:
        verdict = "frontend-only"
    elif "click-hit-wrong-element" in signals and not api_events:
        # The click never reached its control. Whatever requests the interaction
        # window also swept up (a page load either side of the dead click) are
        # not evidence that anything worked.
        verdict = "frontend-only"
    elif "money-field-drift" in signals:
        # Ordered ahead of client-error on purpose: the client-side console
        # error IS the drift ("$NaN"), and "contract-drift" is the verdict that
        # sends the reader to compare both sides instead of only the browser.
        verdict = "contract-drift"
    elif errors or console_errors:
        verdict = "client-error"
    else:
        verdict = "clean"

    if verdict == "clean":
        sentences.append(
            f"No errors were recorded in this trace: {len(requests)} request(s), all "
            f"{'successful' if requests else 'absent'}, "
            f"{len(api_events)} api event(s) and {len(web_events)} web event(s). "
            "Behaviour here looks correct — check the ticket's expectation, not the code."
        )

    if not sentences:
        sentences.append(
            f"Trace {trace_id} contains {len(events)} events across "
            f"{len(set(event.source for event in events))} source(s) with nothing anomalous."
        )

    # Where to read first.
    #
    # Two corrections over "just take the last app frame". A trace usually
    # carries several error events — the original exception, the router's
    # re-raise, the middleware's re-raise, and the browser's own failure. The
    # *first* one is the site; the later ones are the propagation path. And when
    # the verdict is backend-error, the answer is in api code: the web frame is
    # the browser correctly reporting a 500 it did nothing to cause.
    app_frames = [frame for frame in frames if frame.get("app") and frame.get("line") is not None]
    if app_frames:
        preferred = app_frames
        if verdict == "backend-error":
            api_frames = [frame for frame in app_frames if (frame.get("source") or "") == "api"]
            if api_frames:
                preferred = api_frames
        innermost = next(
            (frame for frame in preferred if frame.get("innermost")), preferred[0]
        )
        sentences.append(
            f"Start reading at {innermost['file']}:{innermost['line']}"
            f"{' in ' + innermost['function'] if innermost.get('function') else ''} — but note "
            "the innermost frame is where it surfaced, not necessarily where it went wrong."
        )

    return " ".join(sentences), signals, verdict


def _hints(events: Sequence[Event], verdict: str) -> list[str]:
    hints: list[str] = []
    if verdict == "no-data":
        hints.append("No events for this trace id. Try /telemetry/search to find the session.")
        return hints
    if verdict == "frontend-only":
        hints.append(
            "Backend investigation will find nothing. Read the click event's attrs "
            "(intended vs actual) and reproduce at the session's viewport width."
        )
    if verdict == "backend-error":
        hints.append(
            "Walk backwards from the exception through the sql and business events until "
            "you find the decision that made the failing line's precondition false."
        )
    if verdict == "contract-drift":
        hints.append(
            "Neither side is wrong alone. Compare the response_shapes here against what "
            "the client reads."
        )
    if verdict == "rejected-by-design":
        hints.append(
            "The app refused on purpose. Reproduce and check the UI actually showed the "
            "rejection message; if it did, this is working as intended and needs no patch."
        )
    if verdict == "clean":
        hints.append(
            "Nothing failed in this trace. Confirm the customer's expectation before "
            "assuming a defect exists."
        )
    if not any((event.source or "") == "api" for event in events):
        hints.append("This trace has no api-side events at all.")
    if not any((event.source or "") == "web" for event in events):
        hints.append("This trace has no web-side events at all (api-only or a ghost run).")
    return hints


# --------------------------------------------------------------------------- #
#  Data access
# --------------------------------------------------------------------------- #


def _fetch_trace(db: Session, trace_id: str, limit: int = MAX_TIMELINE_EVENTS) -> list[Event]:
    statement = (
        select(Event)
        .where(Event.trace_id == trace_id)
        .order_by(Event.ts.asc(), Event.id.asc())
        .limit(limit + 1)
    )
    return list(db.execute(statement).scalars().all())


def _fetch_session(db: Session, session_id: str, limit: int = MAX_SESSION_EVENTS) -> list[Event]:
    statement = (
        select(Event)
        .where(Event.session_id == session_id)
        .order_by(Event.ts.asc(), Event.id.asc())
        .limit(limit + 1)
    )
    return list(db.execute(statement).scalars().all())


def _resolve_user(db: Session, value: str) -> tuple[list[int], str | None]:
    """``user`` accepts an id or an email.

    The email lookup reads ``shop.users`` directly — same Postgres instance, no
    dependency on the ``api`` process being alive. If that read fails for any
    reason (schema dropped mid-reset, permissions, api rewrote the table) we
    fall back to matching the email inside event attrs, so search still works.
    """
    value = value.strip()
    if not value:
        return [], None

    if value.isdigit():
        return [int(value)], None

    email = value.lower()
    try:
        rows = db.execute(
            sql_text("SELECT id FROM shop.users WHERE lower(email) = :email"),
            {"email": email},
        ).all()
        ids = [int(row[0]) for row in rows]
        if ids:
            return ids, None
        return [], f"no user in shop.users with email {value}; falling back to attrs text match"
    except Exception as exc:  # noqa: BLE001 - shop schema is not ours to depend on
        db.rollback()
        return [], f"could not read shop.users ({str(exc)[:120]}); using attrs text match"


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #


@router.get("/trace/{trace_id}", response_model=TraceResponse, summary="Merged web+api timeline")
def get_trace(trace_id: str, db: Session = Depends(get_db)) -> TraceResponse:
    """One user interaction and everything it caused, strictly time-ordered.

    Read ``rendered`` first; ``timeline`` carries the same events with their
    full attrs when you need to drill in.
    """
    events = _fetch_trace(db, trace_id)
    if not events:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "trace_not_found",
                "trace_id": trace_id,
                "hint": "Use /telemetry/search to locate the session, then /telemetry/session "
                "to list its traces.",
            },
        )

    truncated = len(events) > MAX_TIMELINE_EVENTS
    events = events[:MAX_TIMELINE_EVENTS]
    rendered = render_timeline(events)
    first, last = events[0], events[-1]

    return TraceResponse(
        trace_id=trace_id,
        session_id=next((event.session_id for event in events if event.session_id), None),
        user_id=next((event.user_id for event in events if event.user_id is not None), None),
        started_at=_iso(first.ts),
        ended_at=_iso(last.ts),
        duration_ms=_ms_between(first.ts, last.ts),
        event_count=len(events),
        counts_by_kind=_counts(events, "kind"),
        counts_by_source=_counts(events, "source"),
        error_count=sum(1 for event in events if _is_error(event)),
        rendered=rendered,
        timeline=[
            to_timeline_event(event, rendered[index]) for index, event in enumerate(events)
        ],
        truncated=truncated,
    )


@router.get(
    "/session/{session_id}", response_model=SessionResponse, summary="Every trace in a session"
)
def get_session(
    session_id: str,
    include_rendered: bool = Query(True, description="Include the full merged rendering"),
    db: Session = Depends(get_db),
) -> SessionResponse:
    """Every interaction in the session, summarised, oldest first.

    Look for the trace where behaviour diverges from intent — usually one with
    an error, or one where the user repeated themselves.
    """
    events = _fetch_session(db, session_id)
    if not events:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "session_not_found",
                "session_id": session_id,
                "hint": "Use /telemetry/search?user=<email>&since=7d to find real session ids.",
            },
        )

    truncated = len(events) > MAX_SESSION_EVENTS
    events = events[:MAX_SESSION_EVENTS]
    grouped = _group_by_trace(events)
    traces = [summarise_trace(group) for group in grouped.values()]
    first, last = events[0], events[-1]

    return SessionResponse(
        session_id=session_id,
        user_id=next((event.user_id for event in events if event.user_id is not None), None),
        started_at=_iso(first.ts),
        ended_at=_iso(last.ts),
        duration_ms=_ms_between(first.ts, last.ts),
        event_count=len(events),
        trace_count=len(traces),
        error_count=sum(1 for event in events if _is_error(event)),
        counts_by_kind=_counts(events, "kind"),
        meta=collect_session_meta(events),
        traces=traces,
        rendered=render_timeline(events) if include_rendered else [],
        truncated=truncated,
    )


@router.get("/search", response_model=SearchResponse, summary="Find the session behind a ticket")
def search(
    user: str | None = Query(None, description="Email or numeric user id"),
    since: str | None = Query(None, description="ISO8601 or relative: 7d, 24h, 30m"),
    until: str | None = Query(None, description="ISO8601 or relative"),
    level: str | None = Query(None, description="Comma-separated: debug,info,warn,error"),
    kind: str | None = Query(None, description="Comma-separated event kinds"),
    name: str | None = Query(None, description="Substring match on event name"),
    text: str | None = Query(None, description="Substring match across name and attrs"),
    source: str | None = Query(None, description="web or api"),
    trace_id: str | None = Query(None),
    session_id: str | None = Query(None),
    limit: int = Query(DEFAULT_SEARCH_SESSIONS, ge=1, le=MAX_SEARCH_SESSIONS),
    db: Session = Depends(get_db),
) -> SearchResponse:
    """Find the session a ticket is talking about.

    Every filter is optional and every filter is AND-ed. Sessions come back
    ranked by recency, each carrying the same summary shape ``/session``
    returns, so one search is usually enough to pick the right one.
    """
    conditions: list[Any] = []
    hints: list[str] = []
    resolved_user_ids: list[int] = []

    if user:
        resolved_user_ids, note = _resolve_user(db, user)
        if note:
            hints.append(note)
        if resolved_user_ids:
            conditions.append(Event.user_id.in_(resolved_user_ids))
        else:
            # Fall back to finding the email anywhere in the event payloads.
            conditions.append(cast(Event.attrs, Text).ilike(f"%{user.strip()}%"))

    since_dt = parse_when(since)
    if since:
        if since_dt is None:
            hints.append(f"could not parse since={since!r}; ignored")
        else:
            conditions.append(Event.ts >= since_dt)

    until_dt = parse_when(until)
    if until:
        if until_dt is None:
            hints.append(f"could not parse until={until!r}; ignored")
        else:
            conditions.append(Event.ts <= until_dt)

    if level:
        levels = [item.strip().lower() for item in level.split(",") if item.strip()]
        if levels:
            conditions.append(Event.level.in_(levels))

    if kind:
        kinds = [item.strip().lower() for item in kind.split(",") if item.strip()]
        if kinds:
            conditions.append(Event.kind.in_(kinds))

    if source:
        sources = [item.strip().lower() for item in source.split(",") if item.strip()]
        if sources:
            conditions.append(Event.source.in_(sources))

    if name:
        conditions.append(Event.name.ilike(f"%{name.strip()}%"))

    if text:
        pattern = f"%{text.strip()}%"
        conditions.append(
            or_(Event.name.ilike(pattern), cast(Event.attrs, Text).ilike(pattern))
        )

    if trace_id:
        conditions.append(Event.trace_id == trace_id)

    if session_id:
        conditions.append(Event.session_id == session_id)

    where = and_(*conditions) if conditions else sql_text("true")

    total_matches = int(
        db.execute(select(func.count()).select_from(Event).where(where)).scalar_one_or_none() or 0
    )

    session_rows = db.execute(
        select(
            Event.session_id,
            func.max(Event.ts).label("last_ts"),
            func.count().label("matches"),
        )
        .where(where)
        .where(Event.session_id.is_not(None))
        .group_by(Event.session_id)
        .order_by(func.max(Event.ts).desc())
        .limit(limit)
    ).all()

    matches: list[SessionMatch] = []
    for row in session_rows:
        sid = row[0]
        last_ts = row[1]
        match_count = int(row[2])

        session_events = _fetch_session(db, sid)
        if not session_events:
            continue
        session_events = session_events[:MAX_SESSION_EVENTS]
        grouped = _group_by_trace(session_events)
        traces = [summarise_trace(group) for group in grouped.values()]

        matched_events = list(
            db.execute(
                select(Event)
                .where(where)
                .where(Event.session_id == sid)
                .order_by(Event.ts.asc(), Event.id.asc())
                .limit(25)
            )
            .scalars()
            .all()
        )

        first, last = session_events[0], session_events[-1]
        matches.append(
            SessionMatch(
                session_id=sid,
                user_id=next(
                    (event.user_id for event in session_events if event.user_id is not None), None
                ),
                started_at=_iso(first.ts),
                ended_at=_iso(last.ts),
                last_event_at=_iso(last_ts),
                duration_ms=_ms_between(first.ts, last.ts),
                event_count=len(session_events),
                trace_count=len(traces),
                error_count=sum(1 for event in session_events if _is_error(event)),
                match_count=match_count,
                counts_by_kind=_counts(session_events, "kind"),
                meta=collect_session_meta(session_events),
                traces=traces,
                matched=[to_timeline_event(event) for event in matched_events],
                rendered=render_timeline(session_events),
            )
        )

    orphan_events = list(
        db.execute(
            select(Event)
            .where(where)
            .where(Event.session_id.is_(None))
            .order_by(Event.ts.desc(), Event.id.desc())
            .limit(25)
        )
        .scalars()
        .all()
    )

    if total_matches == 0:
        hints.append(
            "No events matched. Widen in this order: drop --level (frontend-only bugs "
            "produce no errors at all), widen --since, then search by --text or --kind click."
        )

    return SearchResponse(
        query={
            "user": user,
            "since": _iso(since_dt),
            "until": _iso(until_dt),
            "level": level,
            "kind": kind,
            "name": name,
            "text": text,
            "source": source,
            "trace_id": trace_id,
            "session_id": session_id,
            "limit": limit,
        },
        resolved_user_ids=resolved_user_ids,
        total_matches=total_matches,
        session_count=len(matches),
        sessions=matches,
        unsessioned=[to_timeline_event(event) for event in orphan_events],
        hints=hints,
    )


def _app_frames_first(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Application frames only, when there are any.

    A DBAPI traceback is ~20 library frames around the 2 that belong to the
    codebase. Returning all of them buries the answer, and a library frame has
    never once been the thing to change.
    """
    app = [f for f in frames if f.get("app")]
    return app if app else frames[:8]


@router.get(
    "/bundle/{trace_id}", response_model=BundleResponse, summary="Everything, in one call"
)
def get_bundle(
    trace_id: str,
    preceding: int = Query(
        MAX_PRECEDING_ACTIONS, ge=0, le=200, description="How many prior session events to include"
    ),
    db: Session = Depends(get_db),
) -> BundleResponse:
    """The robot's front door.

    Timeline, stack frames with file:line, the source files those frames
    implicate, response payload key shapes, what the user did earlier in the
    same session, and a plain-English account of what appears to have gone
    wrong — in a single request, so forming a hypothesis costs one round trip.
    """
    events = _fetch_trace(db, trace_id)
    if not events:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "trace_not_found",
                "trace_id": trace_id,
                "hint": "Use /telemetry/search?user=<email>&since=7d to find the session first.",
            },
        )

    events = events[:MAX_TIMELINE_EVENTS]
    rendered = render_timeline(events)
    first, last = events[0], events[-1]

    session_id = next((event.session_id for event in events if event.session_id), None)
    user_id = next((event.user_id for event in events if event.user_id is not None), None)

    trigger = _trigger_of(events)
    requests = _requests_of(events, rendered)
    errors = _errors_of(events, rendered)
    shapes = collect_response_shapes(events)

    # Stack frames, tagged with the event they came from.
    all_frames: list[dict[str, Any]] = []
    for event in events:
        if not _is_error(event):
            continue
        for frame in extract_frames(event):
            enriched = dict(frame)
            enriched["event_id"] = event.id
            enriched["source"] = event.source
            enriched["exception"] = _exception_name(event)
            all_frames.append(enriched)

    # Implicated source files — application frames only, deepest usage first.
    implicated: "OrderedDict[str, ImplicatedFile]" = OrderedDict()
    for frame in all_frames:
        if not frame.get("app"):
            continue
        path = frame["file"]
        entry = implicated.get(path)
        if entry is None:
            entry = ImplicatedFile(path=path, language=frame.get("language", "python"))
            implicated[path] = entry
        entry.frames += 1
        if frame.get("line") is not None and frame["line"] not in entry.lines:
            entry.lines.append(int(frame["line"]))
        function = frame.get("function")
        if function and function not in entry.functions:
            entry.functions.append(function)
    for entry in implicated.values():
        entry.lines.sort()

    # Preceding actions in the same session.
    preceding_events: list[Event] = []
    preceding_traces: list[TraceSummary] = []
    session_meta: dict[str, Any] = {}
    if session_id:
        session_events = _fetch_session(db, session_id)[:MAX_SESSION_EVENTS]
        session_meta = collect_session_meta(session_events)
        earlier = [
            event
            for event in session_events
            if event.trace_id != trace_id and event.ts and first.ts and event.ts < first.ts
        ]
        preceding_events = earlier[-preceding:] if preceding else []
        grouped = _group_by_trace(earlier)
        for group in list(grouped.values())[-MAX_PRECEDING_TRACES:]:
            preceding_traces.append(summarise_trace(group))
    else:
        session_meta = collect_session_meta(events)

    summary, signals, verdict = build_summary(
        trace_id,
        events,
        trigger=trigger,
        requests=requests,
        errors=errors,
        frames=all_frames,
        shapes=shapes,
        meta=session_meta or collect_session_meta(events),
    )

    return BundleResponse(
        trace_id=trace_id,
        session_id=session_id,
        user_id=user_id,
        generated_at=_iso(utcnow()) or "",
        started_at=_iso(first.ts),
        ended_at=_iso(last.ts),
        duration_ms=_ms_between(first.ts, last.ts),
        summary=summary,
        headline=_headline(events, trigger, requests, errors),
        verdict=verdict,
        rendered=rendered,
        timeline=[to_timeline_event(event, rendered[i]) for i, event in enumerate(events)],
        stack_frames=[StackFrame.model_validate(frame)
                      for frame in _app_frames_first(all_frames)],
        implicated_files=list(implicated.values()),
        response_shapes=shapes,
        preceding_actions=[to_timeline_event(event) for event in preceding_events],
        preceding_traces=preceding_traces,
        requests=requests,
        errors=errors,
        sql=[
            to_timeline_event(event, rendered[i])
            for i, event in enumerate(events)
            if (event.kind or "") == "sql"
        ],
        business=[
            to_timeline_event(event, rendered[i])
            for i, event in enumerate(events)
            if (event.kind or "") == "business"
        ],
        trigger=trigger,
        counts_by_kind=_counts(events, "kind"),
        counts_by_source=_counts(events, "source"),
        error_count=sum(1 for event in events if _is_error(event)),
        session_meta=session_meta,
        signals=signals,
        hints=_hints(events, verdict),
    )


__all__ = [
    "build_summary",
    "click_target",
    "collect_response_shapes",
    "collect_session_meta",
    "describe",
    "extract_frames",
    "render_line",
    "render_timeline",
    "router",
    "summarise_trace",
]
