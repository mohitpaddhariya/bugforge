"""``POST /ingest`` — the only way telemetry is ever written.

Three very different clients post here:

1. the browser tracker, batching every 2s over ``fetch``;
2. the same tracker on ``beforeunload`` via ``navigator.sendBeacon``, which
   sends ``text/plain`` (or a ``Blob`` with whatever type it likes) and gives
   the page no chance to retry;
3. ``api``, from a fire-and-forget background queue.

None of them can be allowed to fail. A 500 here would either lose the tail of a
customer session (case 2 has no retry at all) or, worse, propagate back into a
request path we are supposed to be observing rather than affecting. So this
module is written to one rule:

    **Always 202. Drop bad rows, count them, keep the good ones.**

Every layer is defensive — body decoding, JSON parsing, per-event coercion, and
the INSERT itself, which falls back from one executemany to per-row inserts so
a single poisoned row cannot take a batch with it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from .models import Event, SessionLocal
from .schemas import (
    MAX_ATTRS_BYTES,
    MAX_EVENTS_PER_BATCH,
    EventIn,
    IngestResponse,
    utcnow,
)

log = logging.getLogger("collector.ingest")

router = APIRouter(tags=["ingest"])

#: How many parse failures we echo back. Enough to debug an emitter, small
#: enough that a broken client cannot make us build a huge response.
MAX_REPORTED_ERRORS = 10

#: Rows per INSERT statement.
INSERT_CHUNK = 500


# --------------------------------------------------------------------------- #
#  Body decoding
# --------------------------------------------------------------------------- #


def _decode_body(raw: bytes) -> tuple[Any, str | None]:
    """Turn a raw request body into a Python object.

    Tolerates: UTF-8 with a BOM, latin-1 fallback, trailing junk, and
    newline-delimited JSON (which is what a naive beacon implementation
    produces when it concatenates batches). Returns ``(value, error)``.
    """
    if not raw:
        return None, "empty body"

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except Exception:
            return None, "body is not decodable text"

    text = text.strip()
    if not text:
        return None, "empty body"

    try:
        return json.loads(text), None
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same to us
        first_error = str(exc)

    # Newline-delimited JSON: salvage whatever lines parse.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        salvaged: list[Any] = []
        for line in lines:
            try:
                salvaged.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        if salvaged:
            return salvaged, None

    return None, f"invalid JSON: {first_error[:200]}"


def _extract_events(payload: Any) -> tuple[list[Any], str | None]:
    """Find the event list in whatever shape arrived.

    Accepts ``{"events": [...]}`` (the contract), a bare list, a single event
    object, and the common near-misses ``{"batch": [...]}`` / ``{"data": [...]}``.
    """
    if payload is None:
        return [], "no payload"

    if isinstance(payload, list):
        return payload, None

    if isinstance(payload, dict):
        for key in ("events", "batch", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value, None
            if isinstance(value, dict):
                return [value], None
        # A single event posted bare.
        if any(key in payload for key in ("kind", "name", "source", "ts", "trace_id")):
            return [payload], None
        return [], "payload has no 'events' array"

    return [], f"payload is {type(payload).__name__}, expected object or array"


# --------------------------------------------------------------------------- #
#  Per-event normalisation
# --------------------------------------------------------------------------- #


def normalise_event(raw: Any) -> tuple[EventIn | None, str | None]:
    """Coerce one raw event. Returns ``(event, None)`` or ``(None, reason)``."""
    if not isinstance(raw, dict):
        if isinstance(raw, str):
            # Some emitters double-encode individual events.
            try:
                decoded = json.loads(raw)
            except Exception:  # noqa: BLE001
                return None, "event is a non-JSON string"
            if isinstance(decoded, dict):
                raw = decoded
            else:
                return None, f"event decoded to {type(decoded).__name__}"
        else:
            return None, f"event is {type(raw).__name__}, expected object"

    try:
        event = EventIn.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - validators are total, but be safe
        return None, f"event rejected: {str(exc)[:200]}"

    # Oversized attrs blobs (a stringified DOM, a whole HTML error page) are
    # trimmed rather than dropped: the metadata is still worth keeping.
    try:
        encoded = json.dumps(event.attrs, default=str)
        if len(encoded) > MAX_ATTRS_BYTES:
            event.attrs = {
                "_truncated": True,
                "_original_bytes": len(encoded),
                "preview": encoded[:4000],
            }
    except Exception:  # noqa: BLE001
        event.attrs = {"_unserialisable": True}

    return event, None


# --------------------------------------------------------------------------- #
#  Persistence
# --------------------------------------------------------------------------- #


def _insert_rows(db: Session, rows: list[dict[str, Any]]) -> tuple[int, int, list[str]]:
    """Bulk insert with per-row salvage. Returns ``(stored, failed, errors)``."""
    stored = 0
    failed = 0
    errors: list[str] = []

    for start in range(0, len(rows), INSERT_CHUNK):
        chunk = rows[start : start + INSERT_CHUNK]
        try:
            db.execute(insert(Event), chunk)
            db.commit()
            stored += len(chunk)
            continue
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            log.warning("bulk insert of %d events failed, retrying row-by-row: %s", len(chunk), exc)

        # Salvage: one bad row must not cost us the batch.
        for row in chunk:
            try:
                db.execute(insert(Event), [row])
                db.commit()
                stored += 1
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                failed += 1
                if len(errors) < MAX_REPORTED_ERRORS:
                    errors.append(f"insert failed: {str(exc)[:160]}")

    return stored, failed, errors


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #


@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED, summary="Ingest telemetry events")
async def ingest(request: Request) -> JSONResponse:
    """Accept a telemetry batch. Always 202, whatever arrives.

    The body is read raw rather than through a Pydantic body model on purpose:
    ``sendBeacon`` does not let the browser choose a JSON content type, and a
    422 from FastAPI's own validation would be an unretryable data loss.
    """
    received_at = utcnow()
    errors: list[str] = []

    try:
        raw = await request.body()
    except Exception as exc:  # noqa: BLE001 - client hung up mid-body
        log.warning("could not read ingest body: %s", exc)
        return _respond(received_at, 0, 0, 0, False, [f"unreadable body: {str(exc)[:160]}"])

    payload, decode_error = _decode_body(raw)
    if decode_error:
        errors.append(decode_error)

    raw_events, extract_error = _extract_events(payload)
    if extract_error:
        errors.append(extract_error)

    received = len(raw_events)
    overflow = 0
    if received > MAX_EVENTS_PER_BATCH:
        overflow = received - MAX_EVENTS_PER_BATCH
        raw_events = raw_events[:MAX_EVENTS_PER_BATCH]
        errors.append(f"batch truncated: {overflow} events beyond {MAX_EVENTS_PER_BATCH} discarded")

    rows: list[dict[str, Any]] = []
    dropped = overflow
    for raw_event in raw_events:
        event, reason = normalise_event(raw_event)
        if event is None:
            dropped += 1
            if reason and len(errors) < MAX_REPORTED_ERRORS:
                errors.append(reason)
            continue
        rows.append(event.to_row(received_at=received_at))

    if not rows:
        return _respond(received_at, received, 0, dropped, True, errors)

    db = SessionLocal()
    try:
        stored, failed, insert_errors = _insert_rows(db, rows)
    except Exception as exc:  # noqa: BLE001 - db unreachable, pool exhausted, ...
        log.error("ingest persistence failed entirely: %s", exc)
        return _respond(
            received_at,
            received,
            0,
            dropped + len(rows),
            False,
            errors + [f"database unavailable: {str(exc)[:160]}"],
        )
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    errors.extend(insert_errors)
    # ``stored`` means "everything we tried to persist landed". A partial write
    # reports false so the emitter can tell durable loss from a rejected row.
    return _respond(received_at, received, stored, dropped + failed, stored == len(rows), errors)


def _respond(
    received_at: Any,
    received: int,
    accepted: int,
    dropped: int,
    stored: bool,
    errors: list[str],
) -> JSONResponse:
    body = IngestResponse(
        status="accepted",
        received=received,
        accepted=accepted,
        dropped=dropped,
        stored=stored,
        errors=errors[:MAX_REPORTED_ERRORS],
        received_at=received_at.isoformat(),
    )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body.model_dump())


@router.get("/ingest/stats", tags=["ingest"], summary="Ingest counters")
def ingest_stats() -> dict[str, Any]:
    """How much telemetry exists. Used by ``make reset`` and by the robot to
    confirm ghost runs actually produced data."""
    db = SessionLocal()
    try:
        total = db.execute(select(func.count()).select_from(Event)).scalar_one_or_none() or 0
        latest = db.execute(select(func.max(Event.ts))).scalar_one_or_none()
        earliest = db.execute(select(func.min(Event.ts))).scalar_one_or_none()
        traces = (
            db.execute(select(func.count(func.distinct(Event.trace_id)))).scalar_one_or_none() or 0
        )
        sessions = (
            db.execute(select(func.count(func.distinct(Event.session_id)))).scalar_one_or_none()
            or 0
        )
        by_kind_rows = db.execute(
            select(Event.kind, func.count()).group_by(Event.kind).order_by(func.count().desc())
        ).all()
        by_source_rows = db.execute(select(Event.source, func.count()).group_by(Event.source)).all()
        errors = (
            db.execute(
                select(func.count()).select_from(Event).where(Event.level == "error")
            ).scalar_one_or_none()
            or 0
        )
        return {
            "events": int(total),
            "traces": int(traces),
            "sessions": int(sessions),
            "errors": int(errors),
            "earliest_event_at": earliest.isoformat() if earliest else None,
            "latest_event_at": latest.isoformat() if latest else None,
            "by_kind": {kind: int(count) for kind, count in by_kind_rows},
            "by_source": {source: int(count) for source, count in by_source_rows},
        }
    except Exception as exc:  # noqa: BLE001
        return {"events": None, "error": str(exc)[:200]}
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["normalise_event", "router"]
