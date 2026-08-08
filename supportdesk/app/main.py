"""supportdesk — the tiny internal helpdesk for ShopForge.

Deliberately minimal and deliberately isolated:

  * no database connection
  * no calls to `api`
  * no calls to `collector`

Tickets live in `app/tickets.py` as plain Python data. That is the whole point:
the ticket system is what the robot reads FIRST, so it must stay up when the
store under test is on fire.

Serves both HTML (for humans) and JSON (for the robot) from the same data.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.tickets import get_ticket, list_tickets

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="ShopForge Supportdesk",
    description="Internal ticket queue. Read-only, no dependencies.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    "open": "Open",
    "pending": "Pending",
    "closed": "Closed",
}


def _pretty_ts(iso: str) -> str:
    """'2026-08-05T21:47:13.402Z' -> '2026-08-05 21:47 UTC'.

    Intentionally string-sliced rather than parsed: the stored value is always
    the canonical ISO8601-with-millis form, and a formatting helper must never
    be the reason this page fails to render.
    """
    try:
        date_part, _, time_part = iso.partition("T")
        return f"{date_part} {time_part[:5]} UTC"
    except Exception:  # pragma: no cover - defensive only
        return iso


def _decorate(ticket: Dict[str, Any]) -> Dict[str, Any]:
    """Add view-only fields. Never mutates the source ticket."""
    view = dict(ticket)
    view["status_label"] = _STATUS_LABELS.get(ticket["status"], ticket["status"].title())
    view["opened_at_pretty"] = _pretty_ts(ticket["opened_at"])
    view["paragraphs"] = [p for p in ticket["body"].split("\n\n") if p.strip()]
    return view


def _decorate_all(tickets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_decorate(t) for t in tickets]


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def page_index(request: Request) -> HTMLResponse:
    """Queue view: id, subject, customer, status, opened_at."""
    tickets = _decorate_all(list_tickets())
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"tickets": tickets, "open_count": sum(1 for t in tickets if t["status"] == "open")},
    )


@app.get("/ticket/{ticket_id}", response_class=HTMLResponse)
def page_ticket(request: Request, ticket_id: int) -> HTMLResponse:
    """Full detail for one ticket."""
    ticket = get_ticket(ticket_id)
    if ticket is None:
        return templates.TemplateResponse(
            request=request,
            name="not_found.html",
            context={"ticket_id": ticket_id},
            status_code=404,
        )
    return templates.TemplateResponse(
        request=request,
        name="ticket.html",
        context={"ticket": _decorate(ticket)},
    )


# ---------------------------------------------------------------------------
# JSON API — what the robot actually calls
# ---------------------------------------------------------------------------


@app.get("/api/tickets")
def api_tickets() -> JSONResponse:
    return JSONResponse({"tickets": list_tickets()})


@app.get("/api/tickets/{ticket_id}")
def api_ticket(ticket_id: int) -> JSONResponse:
    ticket = get_ticket(ticket_id)
    if ticket is None:
        return JSONResponse({"error": "ticket_not_found", "id": ticket_id}, status_code=404)
    return JSONResponse(ticket)


# ---------------------------------------------------------------------------
# Health — used by docker-compose healthcheck
# ---------------------------------------------------------------------------


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, "service": "supportdesk", "tickets": len(list_tickets())}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "3001")),
        reload=False,
    )
