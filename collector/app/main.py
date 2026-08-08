"""Collector — the observability plane.

This service exists precisely so that breaking the app cannot blind the robot
investigating it. Two consequences show up as code here:

* **No dependency on ``api``.** Nothing in this process imports from, calls, or
  waits on the API service. Past telemetry stays queryable with ``api`` dead.
* **Startup never hard-fails.** If Postgres is not up yet, the app still binds
  its port and answers ``/health`` with ``database: down`` while retrying the
  schema creation in the background. A collector that refuses to boot is a
  collector that takes the investigation down with it.

Routes:

===============================  =============================================
``POST /ingest``                 the only write path; always 202
``GET  /ingest/stats``           how much telemetry exists
``GET  /telemetry/trace/{id}``   merged web+api timeline for one interaction
``GET  /telemetry/session/{id}`` every trace in a browser session
``GET  /telemetry/search``       find the session behind a ticket
``GET  /telemetry/bundle/{id}``  everything needed to form a hypothesis
``GET  /health``                 liveness + storage state
===============================  =============================================
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from .ingest import router as ingest_router
from .models import DATABASE_URL, TELEMETRY_SCHEMA, Event, SessionLocal, create_all, ping
from .query import router as telemetry_router
from .schemas import HealthResponse, utcnow

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
)
log = logging.getLogger("collector")

#: Set once the events table is known to exist.
_SCHEMA_READY = False

_SCHEMA_RETRY_DELAY_SECONDS = 2.0
_SCHEMA_MAX_ATTEMPTS = 60


async def _ensure_schema_forever() -> None:
    """Create the telemetry schema, retrying until Postgres accepts us.

    Runs as a background task so a slow database delays storage, not the port
    binding. Ingest tolerates the gap: it reports ``stored: false`` rather than
    erroring.
    """
    global _SCHEMA_READY
    for attempt in range(1, _SCHEMA_MAX_ATTEMPTS + 1):
        try:
            await asyncio.to_thread(create_all)
            _SCHEMA_READY = True
            log.info("telemetry schema %r ready (attempt %d)", TELEMETRY_SCHEMA, attempt)
            return
        except Exception as exc:  # noqa: BLE001 - db not up yet is the normal case
            log.warning(
                "telemetry schema not ready (attempt %d/%d): %s",
                attempt,
                _SCHEMA_MAX_ATTEMPTS,
                exc,
            )
            await asyncio.sleep(_SCHEMA_RETRY_DELAY_SECONDS)
    log.error("giving up creating the telemetry schema; queries will fail until it exists")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("collector starting; database=%s schema=%s", DATABASE_URL, TELEMETRY_SCHEMA)
    task = asyncio.create_task(_ensure_schema_forever())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        log.info("collector stopped")


app = FastAPI(
    title="bugforge collector",
    version="1.0.0",
    summary="Telemetry ingest and the agent-facing query API",
    description=__doc__,
    lifespan=lifespan,
)

# The browser tracker posts here cross-origin from http://localhost:3000, and
# sendBeacon cannot negotiate. This service is a sandbox observability plane —
# it holds no secrets and reads no cookies — so it accepts any origin.
app.add_middleware(
    CORSMiddleware,
    # `allow_origin_regex` rather than `allow_origins=["*"]` on purpose.
    # `navigator.sendBeacon` — how the web tracker flushes on `beforeunload`,
    # spec §6.3 — always sends its request in credentials mode "include", and a
    # browser rejects a credentialed response whose `Access-Control-Allow-Origin`
    # is the literal `*`. The regex form makes Starlette echo the caller's origin
    # instead, so the final batch of a session is not silently dropped.
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Trace-Id"],
    max_age=86400,
)

app.include_router(ingest_router)
app.include_router(telemetry_router)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Structured failures, never an HTML error page.

    The robot parses these responses. A stack-trace HTML page from the
    observability service would be a uniquely unhelpful thing to hand it.
    """
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": "collector_internal_error",
            "path": request.url.path,
            "detail": str(exc)[:300],
            "hint": "Telemetry storage is unaffected; retry the query.",
        },
    )


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    """Liveness plus what the storage layer actually knows.

    Deliberately reports ``ok`` while the database is down: the process is
    healthy and still accepting ingest attempts. ``database`` tells you the
    truth separately.
    """
    database_up = ping()
    events: int | None = None
    latest: Any = None

    if database_up and _SCHEMA_READY:
        db = SessionLocal()
        try:
            events = int(
                db.execute(select(func.count()).select_from(Event)).scalar_one_or_none() or 0
            )
            latest = db.execute(select(func.max(Event.ts))).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001
            log.warning("health count failed: %s", exc)
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    return HealthResponse(
        status="ok",
        service="collector",
        database="up" if database_up else "down",
        schema_ready=_SCHEMA_READY,
        events=events,
        latest_event_at=latest.isoformat() if latest else None,
        now=utcnow().isoformat(),
        depends_on_api=False,
    )


@app.get("/", tags=["meta"])
def index() -> dict[str, Any]:
    """Route map. The front door for anything pointed at this service."""
    return {
        "service": "collector",
        "version": "1.0.0",
        "role": "telemetry ingest + agent-facing query API",
        "ingest": {
            "POST /ingest": 'body {"events": [Event, ...]}; always 202, bad rows dropped '
            "and counted",
            "GET /ingest/stats": "event / trace / session counters",
        },
        "query": {
            "GET /telemetry/search": "?user=&since=&until=&level=&kind=&name=&text=&limit= "
            "— find the session behind a ticket; user accepts email or id",
            "GET /telemetry/session/{session_id}": "every trace in the session, summarised",
            "GET /telemetry/trace/{trace_id}": "merged, time-ordered web+api timeline",
            "GET /telemetry/bundle/{trace_id}": "the front door: timeline, stack frames, "
            "implicated files, response shapes, preceding actions, plain-English summary",
        },
        "meta": {"GET /health": "liveness + storage state", "GET /docs": "OpenAPI UI"},
        "notes": [
            "Read the `rendered` array before anything else.",
            "This service never calls api; telemetry stays queryable when api is down.",
        ],
    }


__all__ = ["app"]
