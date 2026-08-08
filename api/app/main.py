"""ShopForge API — the store backend. Under test.

Wiring, in the order it matters:

1. ``telemetry.install(app, engine)`` — request middleware, SQL listeners and
   the trace-id contextvar. Innermost, so it sees the real handler outcome.
2. A catch-all middleware that turns an unhandled exception into a JSON 500.
   It sits *outside* telemetry (so the exception is recorded first) and
   *inside* CORS (so the browser actually receives the 500 instead of a
   opaque network error).
3. CORS for ``http://localhost:3000`` with credentials, since the session
   cookie is httpOnly and the web app calls the API cross-origin.

Every router is mounted under ``/api``.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Starlette's class, not FastAPI's subclass: registering the base covers both
# `ApiError` and the framework's own 404 / 405, so every error body has the
# same shape.
from starlette.exceptions import HTTPException

from app import flags
from app.db import DATABASE_URL, create_all, engine, ping
from app.routers import auth as auth_router
from app.routers import cart as cart_router
from app.routers import catalog as catalog_router
from app.routers import checkout as checkout_router
from app.routers import debug as debug_router
from app.routers import orders as orders_router
from app.telemetry import current_trace_id, install

log = logging.getLogger("shopforge.api")

WEB_ORIGIN = os.getenv("WEB_ORIGIN", "http://localhost:3000")
ALLOWED_ORIGINS = [o.strip() for o in WEB_ORIGIN.split(",") if o.strip()]

app = FastAPI(
    title="ShopForge API",
    version="1.0.0",
    description="The store backend the robot practices on.",
)


# --------------------------------------------------------------------------- #
#  Start-up
# --------------------------------------------------------------------------- #


@app.on_event("startup")
def on_startup() -> None:
    """Best-effort schema + flag bootstrap.

    ``make reset`` owns the real lifecycle; this only exists so a bare
    ``docker compose up`` on an empty database still serves requests instead of
    500-ing on every query. Both steps are non-fatal by design.
    """
    try:
        create_all()
    except Exception as exc:  # pragma: no cover - startup race with `make reset`
        log.warning("create_all() skipped: %s", exc)
    try:
        flags.ensure_defaults()
    except Exception as exc:  # pragma: no cover
        log.warning("flags.ensure_defaults() skipped: %s", exc)


# --------------------------------------------------------------------------- #
#  Telemetry (innermost)
# --------------------------------------------------------------------------- #

install(app, engine)


# --------------------------------------------------------------------------- #
#  Errors
# --------------------------------------------------------------------------- #


@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Render errors as ``{"error": <code>, "message": ..., "detail": ...}``.

    ``ApiError`` already carries a dict detail; a bare ``HTTPException`` raised
    anywhere else is normalised into the same shape so the web app only ever
    has to look at one key.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        body = dict(detail)
        body.setdefault("error", "error")
        body.setdefault("message", body.get("error"))
        body.setdefault("detail", body.get("message"))
    else:
        text = str(detail)
        body = {"error": text, "message": text, "detail": text}

    trace_id = current_trace_id()
    if trace_id:
        body.setdefault("trace_id", trace_id)

    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=getattr(exc, "headers", None),
    )


@app.middleware("http")
async def unhandled_error_middleware(request: Request, call_next):
    """Last line of defence: a JSON 500 that still carries CORS headers.

    Telemetry has already recorded the exception and its traceback by the time
    it reaches here (that middleware is installed further in), so this only
    decides what the customer's browser sees: a real ``500`` with a readable
    body rather than a dropped connection.
    """
    try:
        return await call_next(request)
    except Exception as exc:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        body = {
            "error": "internal_error",
            "message": "Something went wrong on our end.",
            "detail": "Something went wrong on our end.",
            "type": type(exc).__name__,
        }
        trace_id = current_trace_id()
        if trace_id:
            body["trace_id"] = trace_id
        return JSONResponse(status_code=500, content=body)


# --------------------------------------------------------------------------- #
#  CORS (outermost)
# --------------------------------------------------------------------------- #

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Trace-Id", "X-Session-Id"],
    max_age=600,
)


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #

app.include_router(auth_router.router, prefix="/api")
app.include_router(catalog_router.router, prefix="/api")
app.include_router(cart_router.router, prefix="/api")
app.include_router(checkout_router.router, prefix="/api")
app.include_router(orders_router.router, prefix="/api")
app.include_router(debug_router.router, prefix="/api")


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "service": "api", "db": ping()}


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "shopforge-api",
        "database": DATABASE_URL.rsplit("@", 1)[-1],
        "docs": "/docs",
        "routes": "/api",
    }


__all__ = ["app"]
