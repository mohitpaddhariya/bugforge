"""Wire shapes for the collector.

Two families live here:

* **Ingest** (`EventIn`, `IngestResponse`) — deliberately *lenient*. Telemetry
  arrives from a wrapped browser ``fetch``, from ``sendBeacon`` on
  ``beforeunload``, and from a fire-and-forget background queue inside ``api``.
  All three can send truncated, half-serialised or plain wrong payloads. The
  contract is: coerce what we can, drop what we cannot, count the drops, and
  never fail the caller. Nothing in this module raises on bad input.

* **Query** (`TraceResponse`, `TraceSummary`, `SessionResponse`,
  `SearchResponse`, `BundleResponse`) — the agent-facing shapes. These are
  documentation as much as validation: the robot reads ``rendered`` first, then
  ``summary``, then drills into ``timeline``.

Both families set ``extra="allow"`` so a newer emitter adding a field never
costs us an event, and so query responses can carry extra diagnostics without a
schema change.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import EVENT_KINDS, EVENT_LEVELS, EVENT_SOURCES

# --------------------------------------------------------------------------- #
#  Column widths — mirrored from models.py so we truncate instead of erroring
# --------------------------------------------------------------------------- #

MAX_ID_LEN = 64
MAX_NAME_LEN = 255
MAX_ATTRS_BYTES = 64 * 1024
MAX_EVENTS_PER_BATCH = 5000

_UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(_UTC)


# --------------------------------------------------------------------------- #
#  Coercion helpers — none of these raise
# --------------------------------------------------------------------------- #


def coerce_str(value: Any, *, max_len: int) -> str | None:
    """Best-effort string, trimmed to ``max_len``. ``None`` for empty/absent."""
    if value is None:
        return None
    if isinstance(value, bool):
        value = "true" if value else "false"
    elif not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return None
    value = value.strip()
    if not value:
        return None
    return value[:max_len]


def coerce_int(value: Any) -> int | None:
    """Best-effort integer. Accepts ``"42"``, ``42.0``; rejects junk."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                parsed = float(text)
            except ValueError:
                return None
            if math.isnan(parsed) or math.isinf(parsed):
                return None
            return int(parsed)
    return None


def coerce_float(value: Any) -> float | None:
    """Best-effort float. NaN/Inf become ``None`` — JSONB cannot store them."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def coerce_ts(value: Any) -> datetime | None:
    """Parse an ISO8601 string (``Z`` suffix ok) or an epoch number.

    Epoch values are disambiguated by magnitude: > 1e11 is treated as
    milliseconds, which is what ``Date.now()`` in the browser produces.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=_UTC)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        if abs(number) > 1e11:  # milliseconds
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=_UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=_UTC)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=_UTC)
            except ValueError:
                continue
        # Bare epoch delivered as a string.
        numeric = coerce_float(text)
        if numeric is not None:
            return coerce_ts(numeric)
    return None


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Make a value storable in JSONB: no NaN, no sets, no arbitrary objects."""
    if depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            out[str(key)[:200]] = _json_safe(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth + 1) for item in list(value)[:500]]
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        return str(value)[:2000]
    except Exception:
        return "<unserialisable>"


def coerce_attrs(value: Any) -> dict[str, Any]:
    """Always a JSON object. Non-objects are wrapped rather than dropped."""
    if value is None:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        if text.startswith("{"):
            import json

            try:
                decoded = json.loads(text)
            except Exception:
                return {"raw": text[:2000]}
            return coerce_attrs(decoded)
        return {"raw": text[:2000]}
    if isinstance(value, dict):
        return _json_safe(value)  # type: ignore[return-value]
    if isinstance(value, (list, tuple)):
        return {"items": _json_safe(list(value))}
    return {"value": _json_safe(value)}


def _normalise_vocab(value: Any, allowed: tuple[str, ...], fallback: str) -> str:
    text = coerce_str(value, max_len=32)
    if not text:
        return fallback
    lowered = text.lower()
    return lowered if lowered in allowed else lowered[:32]


# --------------------------------------------------------------------------- #
#  Relative / absolute time parsing for the query API
# --------------------------------------------------------------------------- #

_RELATIVE_RE = re.compile(r"^(?P<sign>[-+]?)(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[a-z]+)$")

_UNITS: dict[str, float] = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}


def parse_when(value: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse ``since``/``until``.

    Accepts ISO8601 (``2026-08-01T12:00:00Z``), the word ``now``, and the
    relative forms tickets are actually written with: ``7d``, ``24h``, ``30m``,
    ``2w``. Relative values are interpreted as *ago* regardless of sign, which
    is what ``--since 7d`` means to a human. Unparseable input returns ``None``
    (i.e. "no bound") rather than raising — a bad filter must never cost the
    robot its search.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    reference = now or utcnow()
    lowered = text.lower()
    if lowered in ("now", "today"):
        return reference
    match = _RELATIVE_RE.match(lowered)
    if match:
        unit = _UNITS.get(match.group("unit"))
        if unit is not None:
            return reference - timedelta(seconds=float(match.group("num")) * unit)
    return coerce_ts(text)


# --------------------------------------------------------------------------- #
#  Ingest
# --------------------------------------------------------------------------- #


class EventIn(BaseModel):
    """One inbound telemetry event.

    Every field has a default and every validator is total: constructing this
    model from an arbitrary dict succeeds or the row is dropped and counted.
    Unknown fields are kept (``extra="allow"``) but only the declared ones are
    written to the table — the emitter is free to evolve ahead of us.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=False)

    ts: datetime | None = None
    trace_id: str | None = None
    session_id: str | None = None
    user_id: int | None = None
    source: str = "web"
    kind: str = "business"
    name: str = ""
    level: str = "info"
    duration_ms: float | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts", mode="before")
    @classmethod
    def _v_ts(cls, value: Any) -> datetime | None:
        return coerce_ts(value)

    @field_validator("trace_id", "session_id", mode="before")
    @classmethod
    def _v_ids(cls, value: Any) -> str | None:
        return coerce_str(value, max_len=MAX_ID_LEN)

    @field_validator("user_id", mode="before")
    @classmethod
    def _v_user(cls, value: Any) -> int | None:
        return coerce_int(value)

    @field_validator("source", mode="before")
    @classmethod
    def _v_source(cls, value: Any) -> str:
        return _normalise_vocab(value, EVENT_SOURCES, "web")

    @field_validator("kind", mode="before")
    @classmethod
    def _v_kind(cls, value: Any) -> str:
        return _normalise_vocab(value, EVENT_KINDS, "business")

    @field_validator("level", mode="before")
    @classmethod
    def _v_level(cls, value: Any) -> str:
        return _normalise_vocab(value, EVENT_LEVELS, "info")

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, value: Any) -> str:
        return coerce_str(value, max_len=MAX_NAME_LEN) or ""

    @field_validator("duration_ms", mode="before")
    @classmethod
    def _v_duration(cls, value: Any) -> float | None:
        return coerce_float(value)

    @field_validator("attrs", mode="before")
    @classmethod
    def _v_attrs(cls, value: Any) -> dict[str, Any]:
        return coerce_attrs(value)

    def to_row(self, *, received_at: datetime) -> dict[str, Any]:
        """Column dict for a bulk INSERT into ``telemetry.events``."""
        return {
            "ts": self.ts or received_at,
            "received_at": received_at,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "source": self.source,
            "kind": self.kind,
            "name": self.name,
            "level": self.level,
            "duration_ms": self.duration_ms,
            "attrs": self.attrs or {},
        }


class IngestRequest(BaseModel):
    """``{"events": [...]}``.

    Declared for documentation. The endpoint parses the raw body itself
    because ``navigator.sendBeacon`` sends ``text/plain`` and because a
    malformed body must still yield 202.
    """

    model_config = ConfigDict(extra="allow")

    events: list[dict[str, Any]] = Field(default_factory=list)


class IngestResponse(BaseModel):
    """Always returned with HTTP 202, success or not."""

    model_config = ConfigDict(extra="allow")

    status: str = "accepted"
    received: int = 0
    accepted: int = 0
    dropped: int = 0
    stored: bool = True
    errors: list[str] = Field(default_factory=list)
    received_at: str = ""


# --------------------------------------------------------------------------- #
#  Query — shared building blocks
# --------------------------------------------------------------------------- #


class TimelineEvent(BaseModel):
    """One row of a timeline: the stored event plus what we derived from it."""

    model_config = ConfigDict(extra="allow")

    id: int | None = None
    ts: str | None = None
    received_at: str | None = None
    trace_id: str | None = None
    session_id: str | None = None
    user_id: int | None = None
    source: str = "web"
    kind: str = "business"
    name: str = ""
    level: str = "info"
    duration_ms: float | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
    #: The pre-formatted line for this event — same string as the matching
    #: entry in the parent's ``rendered`` array.
    rendered: str = ""
    #: ``checkout.py:94`` when the event carries a stack frame.
    location: str | None = None


class ClickTarget(BaseModel):
    """The triggering click of a trace, with intended vs actually-hit element."""

    model_config = ConfigDict(extra="allow")

    ts: str | None = None
    name: str = ""
    testid: str | None = None
    selector: str | None = None
    text: str | None = None
    tag: str | None = None
    intended: dict[str, Any] | None = None
    actual: dict[str, Any] | None = None
    listener_ran: bool | None = None
    default_prevented: bool | None = None
    hit_mismatch: bool = False
    rendered: str = ""


class RequestSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str | None = None
    source: str = "web"
    method: str | None = None
    url: str | None = None
    route: str | None = None
    status: int | None = None
    duration_ms: float | None = None
    ok: bool | None = None
    rendered: str = ""


class ErrorSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str | None = None
    source: str = "api"
    #: ``error`` for a thrown exception, ``console`` for a console.error.
    kind: str = "error"
    name: str = ""
    exception: str | None = None
    message: str | None = None
    location: str | None = None
    file: str | None = None
    line: int | None = None
    rendered: str = ""


class StackFrame(BaseModel):
    model_config = ConfigDict(extra="allow")

    file: str
    line: int | None = None
    function: str | None = None
    code: str | None = None
    #: False for stdlib / site-packages / node_modules frames.
    app: bool = True
    #: ``checkout.py:94``
    location: str = ""
    language: str = "python"
    event_id: int | None = None
    #: "api" or "web" — which side of the app raised the exception this frame
    #: came from. A backend-error verdict should be read in api frames.
    source: str | None = None
    exception: str | None = None
    #: Innermost application frame of its traceback — the one to read first.
    innermost: bool = False


class ImplicatedFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str
    lines: list[int] = Field(default_factory=list)
    frames: int = 0
    functions: list[str] = Field(default_factory=list)
    language: str = "python"
    reason: str = "appears in a stack frame"


class ResponseShape(BaseModel):
    """Top-level key shape of a JSON response body. The BUG-003 shortcut."""

    model_config = ConfigDict(extra="allow")

    ts: str | None = None
    source: str = "api"
    method: str | None = None
    url: str | None = None
    route: str | None = None
    status: int | None = None
    keys: list[str] = Field(default_factory=list)
    shape: dict[str, str] = Field(default_factory=dict)
    truncated: bool = False
    event_id: int | None = None


class TraceSummary(BaseModel):
    """The per-trace summary shared by ``/session`` and ``/search``."""

    model_config = ConfigDict(extra="allow")

    trace_id: str | None = None
    session_id: str | None = None
    user_id: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: float = 0.0
    event_count: int = 0
    counts_by_kind: dict[str, int] = Field(default_factory=dict)
    counts_by_source: dict[str, int] = Field(default_factory=dict)
    counts_by_level: dict[str, int] = Field(default_factory=dict)
    error_count: int = 0
    #: The click/nav that opened the interaction window.
    trigger: ClickTarget | None = None
    #: Every HTTP call the trigger caused, web-side and api-side.
    requests: list[RequestSummary] = Field(default_factory=list)
    errors: list[ErrorSummary] = Field(default_factory=list)
    #: One-line verdict, e.g. ``"click → 1 request → 500 IntegrityError"``.
    headline: str = ""
    rendered: list[str] = Field(default_factory=list)


class TraceResponse(BaseModel):
    """``GET /telemetry/trace/{trace_id}``"""

    model_config = ConfigDict(extra="allow")

    trace_id: str
    session_id: str | None = None
    user_id: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: float = 0.0
    event_count: int = 0
    counts_by_kind: dict[str, int] = Field(default_factory=dict)
    counts_by_source: dict[str, int] = Field(default_factory=dict)
    error_count: int = 0
    #: Read this first.
    rendered: list[str] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    truncated: bool = False


class SessionResponse(BaseModel):
    """``GET /telemetry/session/{session_id}``"""

    model_config = ConfigDict(extra="allow")

    session_id: str
    user_id: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: float = 0.0
    event_count: int = 0
    trace_count: int = 0
    error_count: int = 0
    counts_by_kind: dict[str, int] = Field(default_factory=dict)
    #: Viewport / user agent / locale, lifted out of session-meta events.
    meta: dict[str, Any] = Field(default_factory=dict)
    traces: list[TraceSummary] = Field(default_factory=list)
    rendered: list[str] = Field(default_factory=list)
    truncated: bool = False


class SessionMatch(BaseModel):
    """A session hit from ``/telemetry/search``."""

    model_config = ConfigDict(extra="allow")

    session_id: str | None = None
    user_id: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    last_event_at: str | None = None
    duration_ms: float = 0.0
    event_count: int = 0
    trace_count: int = 0
    error_count: int = 0
    match_count: int = 0
    counts_by_kind: dict[str, int] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)
    #: Same shape as ``/telemetry/session`` returns.
    traces: list[TraceSummary] = Field(default_factory=list)
    #: The events that actually matched the filters.
    matched: list[TimelineEvent] = Field(default_factory=list)
    rendered: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    """``GET /telemetry/search``"""

    model_config = ConfigDict(extra="allow")

    query: dict[str, Any] = Field(default_factory=dict)
    resolved_user_ids: list[int] = Field(default_factory=list)
    total_matches: int = 0
    session_count: int = 0
    sessions: list[SessionMatch] = Field(default_factory=list)
    #: Orphan matches (events with no session_id) so nothing is invisible.
    unsessioned: list[TimelineEvent] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)


class BundleResponse(BaseModel):
    """``GET /telemetry/bundle/{trace_id}`` — the robot's front door."""

    model_config = ConfigDict(extra="allow")

    trace_id: str
    session_id: str | None = None
    user_id: int | None = None
    generated_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: float = 0.0
    #: Plain English: what appears to have gone wrong.
    summary: str = ""
    headline: str = ""
    verdict: str = "unknown"
    rendered: list[str] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    stack_frames: list[StackFrame] = Field(default_factory=list)
    implicated_files: list[ImplicatedFile] = Field(default_factory=list)
    response_shapes: list[ResponseShape] = Field(default_factory=list)
    preceding_actions: list[TimelineEvent] = Field(default_factory=list)
    preceding_traces: list[TraceSummary] = Field(default_factory=list)
    requests: list[RequestSummary] = Field(default_factory=list)
    errors: list[ErrorSummary] = Field(default_factory=list)
    sql: list[TimelineEvent] = Field(default_factory=list)
    business: list[TimelineEvent] = Field(default_factory=list)
    trigger: ClickTarget | None = None
    counts_by_kind: dict[str, int] = Field(default_factory=dict)
    counts_by_source: dict[str, int] = Field(default_factory=dict)
    error_count: int = 0
    session_meta: dict[str, Any] = Field(default_factory=dict)
    #: Machine-readable observations behind ``summary``.
    signals: list[str] = Field(default_factory=list)
    #: What to try next when the timeline is thin.
    hints: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = "ok"
    service: str = "collector"
    database: str = "up"
    schema_ready: bool = True
    events: int | None = None
    latest_event_at: str | None = None
    now: str = ""
    #: Always false: the collector never calls api.
    depends_on_api: bool = False


__all__ = [
    "MAX_ATTRS_BYTES",
    "MAX_EVENTS_PER_BATCH",
    "MAX_ID_LEN",
    "MAX_NAME_LEN",
    "BundleResponse",
    "ClickTarget",
    "ErrorSummary",
    "EventIn",
    "HealthResponse",
    "ImplicatedFile",
    "IngestRequest",
    "IngestResponse",
    "RequestSummary",
    "ResponseShape",
    "SearchResponse",
    "SessionMatch",
    "SessionResponse",
    "StackFrame",
    "TimelineEvent",
    "TraceResponse",
    "TraceSummary",
    "coerce_attrs",
    "coerce_float",
    "coerce_int",
    "coerce_str",
    "coerce_ts",
    "parse_when",
    "utcnow",
]
