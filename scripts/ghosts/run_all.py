#!/usr/bin/env python3
"""Ghost runs — the historical sessions the robot investigates (spec §8.2).

    docker compose exec -T api python /srv/scripts/ghosts/run_all.py

For the robot to "pull up the customer's session", that session has to already
exist in telemetry *before* the robot ever runs. We do not hand-write telemetry
rows: they would drift from what the app actually emits and the robot would
learn to trust fiction. Instead each ghost impersonates one ticket's customer
and **drives the real app**:

* it flips that ticket's bug flags through ``POST /api/debug/flags``;
* it mints a ``session_id`` and per-interaction ``trace_id``s in exactly the
  format ``web/lib/telemetry.ts`` uses (``s_``/``t_`` + 8 hex);
* it calls the real HTTP API with ``X-Trace-Id`` / ``X-Session-Id``, so every
  api-side ``request`` / ``sql`` / ``business`` / ``error`` event is genuinely
  produced by the code under test;
* it posts the matching **web-side** events (click, nav, fetch, console, error,
  vitals) straight to ``collector /ingest`` in the contract's Event shape,
  interleaved with the API calls. That stands in for the browser the customer
  actually used;
* it reproduces the human parts — the pause before clicking, the three retries,
  the reload, giving up and leaving.

This module is also the shared library every ``ticket_XXXX.py`` imports. It
deliberately imports **no** ticket module at import time (they import *it*);
``run_all()`` pulls them in lazily.

Assertions (spec §12, open question 2)
--------------------------------------
A ghost that silently stops reproducing poisons everything downstream, so every
ghost ends by querying telemetry back and asserting its symptom is really there
— including the negative symptoms (#1043 must produce *zero* api-side evidence,
#1046 must produce *zero* errors anywhere). Any failed check makes the ghost,
and ``run_all``, exit non-zero and say exactly which check failed.
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in ("/srv", os.path.dirname(os.path.dirname(_HERE))):
    if _candidate and _candidate not in sys.path and os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import flags as appflags  # noqa: E402

# --------------------------------------------------------------------------- #
#  Configuration
# --------------------------------------------------------------------------- #

#: Where the ghosts actually send their HTTP (container network).
API_URL = os.getenv("API_URL", "http://api:8000").rstrip("/")
COLLECTOR_URL = os.getenv("COLLECTOR_URL", "http://collector:8001").rstrip("/")

#: What a real browser would have had in its address bar / fetch URLs. Recorded
#: in the web-side fetch events so the telemetry reads like a browser's.
BROWSER_API_ORIGIN = os.getenv("NEXT_PUBLIC_API_URL", "http://localhost:8000").rstrip("/")
WEB_ORIGIN = os.getenv("WEB_ORIGIN", "http://localhost:3000").rstrip("/")

SEED_PASSWORD = "password123"

#: How long to wait for api-side telemetry to make it through the collector.
#: The api ships batches roughly every second; 30s is generous on purpose,
#: because a false "symptom missing" is far more expensive than a slow reset.
TELEMETRY_TIMEOUT = float(os.getenv("GHOST_TELEMETRY_TIMEOUT", "30"))

#: Ghost runs are slowed down to human speed. Set GHOST_SPEED=4 to run them at
#: 4x for a fast iteration loop (the telemetry is identical, just tighter).
SPEED = max(0.1, float(os.getenv("GHOST_SPEED", "1")))

DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
FIREFOX_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0"
)
WINDOWS_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)

#: The order ghost runs execute in. Chronological by ticket number, which also
#: happens to be the order the tickets were filed.
GHOST_MODULES: tuple[str, ...] = (
    "ticket_1042",
    "ticket_1043",
    "ticket_1044",
    "ticket_1045",
    "ticket_1046",
)


class GhostFailure(RuntimeError):
    """A ghost could not reproduce what it exists to reproduce."""


# --------------------------------------------------------------------------- #
#  Ids and timestamps — byte-identical in shape to web/lib/telemetry.ts
# --------------------------------------------------------------------------- #


def hex8() -> str:
    return f"{random.getrandbits(32):08x}"


def new_session_id() -> str:
    return "s_" + hex8()


def new_trace_id() -> str:
    return "t_" + hex8()


def iso_now() -> str:
    """``2026-08-08T12:04:22.118Z`` — UTC, milliseconds, exactly like the tracker."""
    ts = datetime.now(timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def human_pause(seconds: float) -> None:
    """Sleep like a person reading a page. Scaled by ``GHOST_SPEED``."""
    time.sleep(max(0.0, seconds) / SPEED)


# --------------------------------------------------------------------------- #
#  DOM description helpers — mirror Tracker.describeElement()
# --------------------------------------------------------------------------- #


def element(
    *,
    tag: str = "button",
    testid: str | None = None,
    text_: str = "",
    selector: str | None = None,
    classes: str | None = None,
    disabled: bool = False,
    rect: dict[str, int] | None = None,
    position: str = "static",
    z_index: str = "auto",
    pointer_events: str = "auto",
    opacity: str = "1",
    background: str = "rgba(0, 0, 0, 0)",
    role: str | None = None,
    type_: str | None = None,
    href: str | None = None,
) -> dict[str, Any]:
    """The element shape the browser tracker records for a click target."""
    return {
        "tag": tag,
        "testid": testid,
        "id": None,
        "classes": classes,
        "role": role,
        "type": type_,
        "href": href,
        "disabled": disabled,
        "text": text_,
        "selector": selector or (f'{tag}[data-testid="{testid}"]' if testid else tag),
        "rect": rect,
        "z_index": z_index,
        "position": position,
        "pointer_events": pointer_events,
        "opacity": opacity,
        "display": "block",
        "visibility": "visible",
        "background": background,
    }


# --------------------------------------------------------------------------- #
#  Checks
# --------------------------------------------------------------------------- #


@dataclass
class Check:
    """One asserted symptom. ``ok=False`` fails the whole ghost, loudly."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class GhostResult:
    ticket: int
    title: str
    persona: str
    session_id: str
    checks: list[Check] = field(default_factory=list)
    traces: list[str] = field(default_factory=list)
    event_count: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.checks) and all(c.ok for c in self.checks)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


# --------------------------------------------------------------------------- #
#  Feature flags — HTTP control plane first, direct DB write as a fallback
# --------------------------------------------------------------------------- #


def set_flag(key: str, enabled: bool) -> bool:
    """Flip one switch. ``/api/debug/*`` is excluded from telemetry by design."""
    try:
        response = httpx.post(
            f"{API_URL}/api/debug/flags",
            json={"key": key, "enabled": bool(enabled)},
            timeout=10.0,
        )
        if response.status_code < 400:
            return bool(response.json().get("enabled"))
    except Exception:  # noqa: BLE001 - api may be mid-reload; fall through
        pass
    # The ghosts run inside the api container, so the database is right there.
    # The running server picks the change up within the flag cache TTL (~2s).
    appflags.set_flag(key, enabled)
    time.sleep(2.5)
    return bool(enabled)


def apply_flags(values: dict[str, bool]) -> None:
    for key, enabled in values.items():
        set_flag(key, enabled)
    # Give the server's flag cache a beat to expire even on the HTTP path.
    time.sleep(0.3)


def reset_all_flags() -> None:
    """Every known bug switch off. Run between ghosts so they never bleed."""
    for key in appflags.ALL_FLAG_KEYS:
        set_flag(key, False)
    time.sleep(0.3)


# --------------------------------------------------------------------------- #
#  Seed-state helpers
# --------------------------------------------------------------------------- #


def coupon_state(code: str) -> dict[str, Any] | None:
    with dbmod.engine.begin() as conn:
        row = conn.execute(
            text(
                f'SELECT code, uses, max_uses, expires_at FROM "{dbmod.SHOP_SCHEMA}".coupons '
                "WHERE code = :code"
            ),
            {"code": code},
        ).mappings().first()
    return dict(row) if row else None


def reprime_coupon(code: str) -> None:
    """Put a coupon back one redemption from its limit.

    ``SAVE20`` is seeded at 4 of 5 precisely so the BUG-001 race is reachable on
    the next checkout. Ghost 1042 burns that headroom by design, so it restores
    it afterwards — the robot must find the store in the same primed state the
    seed left it in.
    """
    with dbmod.engine.begin() as conn:
        conn.execute(
            text(
                f'UPDATE "{dbmod.SHOP_SCHEMA}".coupons '
                "SET uses = GREATEST(0, max_uses - 1) WHERE code = :code"
            ),
            {"code": code},
        )


def user_id_for(email: str) -> int:
    with dbmod.engine.begin() as conn:
        row = conn.execute(
            text(f'SELECT id FROM "{dbmod.SHOP_SCHEMA}".users WHERE email = :email'),
            {"email": email},
        ).first()
    if row is None:
        raise GhostFailure(f"seed is missing user {email} — run scripts/seed.py first")
    return int(row[0])


def order_owned_by_someone_else(email: str) -> dict[str, Any]:
    """An order id that does *not* belong to ``email`` — the BUG-004 target.

    Prefers the jacket order, because that is the one ticket #1045 describes.
    """
    with dbmod.engine.begin() as conn:
        row = conn.execute(
            text(
                f"""
                SELECT o.id AS order_id, o.user_id, u.email, i.name_snapshot
                FROM "{dbmod.SHOP_SCHEMA}".orders o
                JOIN "{dbmod.SHOP_SCHEMA}".users u ON u.id = o.user_id
                JOIN "{dbmod.SHOP_SCHEMA}".order_items i ON i.order_id = o.id
                WHERE u.email <> :email AND i.name_snapshot ILIKE '%jacket%'
                ORDER BY o.id
                LIMIT 1
                """
            ),
            {"email": email},
        ).mappings().first()
        if row is None:
            row = conn.execute(
                text(
                    f"""
                    SELECT o.id AS order_id, o.user_id, u.email, i.name_snapshot
                    FROM "{dbmod.SHOP_SCHEMA}".orders o
                    JOIN "{dbmod.SHOP_SCHEMA}".users u ON u.id = o.user_id
                    JOIN "{dbmod.SHOP_SCHEMA}".order_items i ON i.order_id = o.id
                    WHERE u.email <> :email
                    ORDER BY o.id
                    LIMIT 1
                    """
                ),
                {"email": email},
            ).mappings().first()
    if row is None:
        raise GhostFailure("seed has no order belonging to another user")
    return dict(row)


# --------------------------------------------------------------------------- #
#  The ghost browser
# --------------------------------------------------------------------------- #


class Ghost:
    """One impersonated customer session.

    Web-side events are buffered and posted to ``collector /ingest`` in batches,
    exactly like the browser tracker's 2s flush. API calls go to the real
    service with the same trace/session headers the browser would have sent.
    """

    def __init__(
        self,
        *,
        ticket: int,
        title: str,
        email: str,
        viewport: tuple[int, int],
        user_agent: str = DESKTOP_UA,
        locale: str = "en-US",
        device_pixel_ratio: float = 2.0,
        route: str = "/",
    ) -> None:
        self.ticket = ticket
        self.title = title
        self.email = email
        self.viewport_w, self.viewport_h = viewport
        self.user_agent = user_agent
        self.locale = locale
        self.device_pixel_ratio = device_pixel_ratio

        self.session_id = new_session_id()
        self.user_id: int | None = None
        self.route = route
        self.trace: str = new_trace_id()
        self.traces: list[str] = [self.trace]

        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._posted = 0

        self.client = httpx.Client(
            base_url=API_URL,
            timeout=30.0,
            headers={"User-Agent": self.user_agent, "Origin": WEB_ORIGIN},
        )

    # -- lifecycle -------------------------------------------------------- #

    def close(self) -> None:
        self.flush()
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "Ghost":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- trace management ------------------------------------------------- #

    def open_interaction(self, reason: str = "click") -> str:
        """A trace id is per user *intent*, not per request (spec §6.1)."""
        self.trace = new_trace_id()
        self.traces.append(self.trace)
        return self.trace

    # -- web event emission ----------------------------------------------- #

    def record(
        self,
        kind: str,
        name: str,
        attrs: dict[str, Any] | None = None,
        *,
        level: str = "info",
        duration_ms: float | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "ts": iso_now(),
            "trace_id": trace_id if trace_id is not None else self.trace,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "source": "web",
            "kind": kind,
            "name": name,
            "level": level,
            "duration_ms": duration_ms,
            "attrs": attrs or {},
        }
        with self._lock:
            self._buffer.append(event)
            should_flush = len(self._buffer) >= 40
        if should_flush:
            self.flush()
        return event

    def flush(self) -> None:
        with self._lock:
            batch, self._buffer = self._buffer, []
        if not batch:
            return
        try:
            httpx.post(f"{COLLECTOR_URL}/ingest", json={"events": batch}, timeout=10.0)
            self._posted += len(batch)
        except Exception as exc:  # noqa: BLE001
            raise GhostFailure(f"could not post web telemetry to the collector: {exc}") from exc

    # -- page level ------------------------------------------------------- #

    def open_page(self, route: str, *, referrer: str = "") -> None:
        """A fresh document load: session meta, vitals, nav — same as start()."""
        self.open_interaction("page_load")
        self.route = route
        self.record(
            "vitals",
            "session_start",
            {
                "viewport_w": self.viewport_w,
                "viewport_h": self.viewport_h,
                "screen_w": self.viewport_w,
                "screen_h": self.viewport_h,
                "device_pixel_ratio": self.device_pixel_ratio,
                "user_agent": self.user_agent,
                "locale": self.locale,
                "languages": [self.locale],
                "timezone": "UTC",
                "url": f"{WEB_ORIGIN}{route}",
                "collector": f"{COLLECTOR_URL}/ingest",
            },
        )
        self.record(
            "nav",
            "page_load",
            {"from": None, "to": route, "referrer": referrer},
        )
        self.record(
            "vitals",
            "page_load_timing",
            {
                "route": route,
                "first_paint_ms": 412.0,
                "first_contentful_paint_ms": 448.0,
                "viewport_w": self.viewport_w,
                "viewport_h": self.viewport_h,
                "ttfb_ms": 96.0,
                "dom_interactive_ms": 640.0,
                "dom_content_loaded_ms": 702.0,
                "load_event_ms": 861.0,
                "nav_type": "navigate",
            },
            duration_ms=861.0,
        )

    def navigate(self, to: str, *, via: str = "pushState", title: str = "") -> None:
        """Client-side route change, the way the Next.js router does it."""
        from_route, self.route = self.route, to
        self.record(
            "nav",
            "route_change",
            {"from": from_route, "to": to, "via": via, "title": title},
        )

    def reload(self, route: str | None = None) -> None:
        """A full document reload — new interaction, fresh vitals."""
        target = route or self.route
        self.open_interaction("reload")
        self.record(
            "nav",
            "page_load",
            {"from": target, "to": target, "referrer": f"{WEB_ORIGIN}{target}", "reload": True},
        )
        self.record(
            "vitals",
            "page_load_timing",
            {
                "route": target,
                "first_paint_ms": 388.0,
                "first_contentful_paint_ms": 401.0,
                "viewport_w": self.viewport_w,
                "viewport_h": self.viewport_h,
                "nav_type": "reload",
            },
            duration_ms=744.0,
        )

    # -- interactions ------------------------------------------------------ #

    def click(
        self,
        testid: str,
        *,
        text_: str = "",
        tag: str = "button",
        at: tuple[int, int] | None = None,
        rect: dict[str, int] | None = None,
        disabled: bool = False,
        listener_ran: bool = True,
        new_interaction: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """A click that reached its intended target."""
        if new_interaction:
            self.open_interaction("click")
        hit = element(
            tag=tag, testid=testid, text_=text_, rect=rect, disabled=disabled, z_index="auto"
        )
        x, y = at or self._default_point(rect)
        attrs: dict[str, Any] = {
            "selector": hit["selector"],
            "testid": testid,
            "text": text_,
            "tag": tag,
            "hit_element": hit,
            "intended_target": None,
            "hit_is_intended_target": True,
            "element_stack_at_point": [hit],
            "obscured_interactive_element": None,
            "click_blocked_by_overlay": False,
            "listeners_on_path": 1 if listener_ran else 0,
            "listeners_on_hit_element": 1 if listener_ran else 0,
            "client_x": x,
            "client_y": y,
            "page_x": x,
            "page_y": y,
            "button": 0,
            "trusted": True,
            "viewport_w": self.viewport_w,
            "viewport_h": self.viewport_h,
            "route": self.route,
            "propagation_reached_document": True,
            "propagation_stopped": False,
            "default_prevented": False,
            "listener_ran": listener_ran,
        }
        if disabled:
            # A disabled control still gets recorded: it is the whole reason the
            # customer's three retries are visible with no request behind them.
            attrs["listener_ran"] = False
            attrs["disabled"] = True
        if extra:
            attrs.update(extra)
        self.record("click", testid, attrs, level="info")
        return self.trace

    def blocked_click(
        self,
        *,
        intended_testid: str,
        overlay: dict[str, Any],
        intended_rect: dict[str, int],
        at: tuple[int, int] | None = None,
        intended_text: str = "",
    ) -> str:
        """A click that landed on an invisible overlay instead of the button.

        This is the BUG-002 diagnosis in one event: the click happened, its real
        hit target was the overlay, no listener ran, and no fetch follows.
        """
        self.open_interaction("click")
        x, y = at or self._default_point(intended_rect)
        intended = element(
            tag="button",
            testid=intended_testid,
            text_=intended_text,
            rect=intended_rect,
            z_index="30",
            position="static",
        )
        attrs = {
            "selector": overlay["selector"],
            "testid": overlay.get("testid"),
            "text": overlay.get("text", ""),
            "tag": overlay["tag"],
            "hit_element": overlay,
            "intended_target": None,
            "hit_is_intended_target": True,
            "element_stack_at_point": [overlay, intended],
            "obscured_interactive_element": intended,
            "click_blocked_by_overlay": True,
            "listeners_on_path": 0,
            "listeners_on_hit_element": 0,
            "client_x": x,
            "client_y": y,
            "page_x": x,
            "page_y": y,
            "button": 0,
            "trusted": True,
            "viewport_w": self.viewport_w,
            "viewport_h": self.viewport_h,
            "route": self.route,
            "propagation_reached_document": True,
            "propagation_stopped": False,
            "default_prevented": False,
            "listener_ran": False,
        }
        # level=warn: the tracker flags a click it knows was swallowed.
        self.record("click", overlay.get("testid") or "unknown", attrs, level="warn")
        return self.trace

    def submit(self, testid: str, fields: Sequence[str], *, new_interaction: bool = True) -> str:
        if new_interaction:
            self.open_interaction("submit")
        info = element(tag="form", testid=testid)
        self.record(
            "click",
            testid,
            {
                "submit": True,
                "form": info,
                "fields": list(fields),
                "method": "post",
                "action": f"{WEB_ORIGIN}{self.route}",
                "route": self.route,
            },
        )
        return self.trace

    def console_error(self, message: str, *, stack: str | None = None) -> None:
        self.record(
            "console",
            "console.error",
            {
                "message": message,
                "arg_count": 1,
                "route": self.route,
                "stack": stack,
            },
            level="error",
        )

    def console_warn(self, message: str) -> None:
        self.record(
            "console",
            "console.warn",
            {"message": message, "arg_count": 1, "route": self.route},
            level="warn",
        )

    def unhandled_rejection(self, message: str, *, error_type: str = "ApiError") -> None:
        self.record(
            "error",
            "unhandledrejection",
            {
                "message": message,
                "stack": (
                    f"{error_type}: {message}\n"
                    "    at request (webpack-internal:///./lib/api.ts:88:15)\n"
                    "    at async placeOrder (webpack-internal:///./app/checkout/page.tsx:99:21)"
                ),
                "error_type": error_type,
                "route": self.route,
            },
            level="error",
        )

    def ui_message(self, testid: str, message: str, **extra: Any) -> None:
        """Something the customer was actually shown.

        Emitted as a web ``business`` event (the same channel the app's own
        ``track()`` calls use). BUG-005 hinges on being able to prove the expiry
        message really did render.
        """
        attrs = {"testid": testid, "message": message, "route": self.route}
        attrs.update(extra)
        self.record("business", "ui_message_shown", attrs)

    def _default_point(self, rect: dict[str, int] | None) -> tuple[int, int]:
        if not rect:
            return (self.viewport_w // 2, self.viewport_h // 2)
        return (rect["x"] + rect["w"] // 2, rect["y"] + rect["h"] // 2)

    # -- HTTP -------------------------------------------------------------- #

    def api(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        trace_id: str | None = None,
        expect: int | Iterable[int] | None = None,
        record_fetch: bool = True,
    ) -> httpx.Response:
        """Call the real API, stamped like the browser, and record the fetch.

        Returns the response even when it is an error — the ghost decides what
        that means. ``expect`` fails loudly when the API answers something the
        scenario cannot be built on.
        """
        trace = trace_id or self.trace
        headers = {"X-Trace-Id": trace, "X-Session-Id": self.session_id}
        url = f"{BROWSER_API_ORIGIN}{path}"
        body_bytes = len(json.dumps(json_body).encode()) if json_body is not None else 0

        started = time.perf_counter()
        error: Exception | None = None
        response: httpx.Response | None = None
        try:
            response = self.client.request(method, path, json=json_body, headers=headers)
        except Exception as exc:  # noqa: BLE001
            error = exc
        duration_ms = (time.perf_counter() - started) * 1000.0

        if record_fetch:
            status = response.status_code if response is not None else None
            attrs: dict[str, Any] = {
                "method": method.upper(),
                "url": url,
                "path": path.split("?")[0],
                "status": status,
                "ok": bool(response is not None and 200 <= status < 300),  # type: ignore[operator]
                "status_text": "" if response is None else response.reason_phrase,
                "request_bytes": body_bytes,
                "response_bytes": len(response.content) if response is not None else None,
                "trace_id_sent": trace,
                "session_id_sent": self.session_id,
                "content_type": None
                if response is None
                else response.headers.get("content-type"),
                "route": self.route,
            }
            if error is not None:
                attrs["error"] = str(error)[:300]
            level = (
                "error"
                if error is not None or (response is not None and response.status_code >= 500)
                else "warn"
                if response is not None and response.status_code >= 400
                else "info"
            )
            self.record(
                "fetch",
                f"{method.upper()} {path.split('?')[0]}",
                attrs,
                level=level,
                duration_ms=round(duration_ms, 3),
                trace_id=trace,
            )

        if error is not None:
            raise GhostFailure(f"{method} {path} never completed: {error}") from error

        assert response is not None
        if expect is not None:
            allowed = {expect} if isinstance(expect, int) else set(expect)
            if response.status_code not in allowed:
                raise GhostFailure(
                    f"{method} {path} answered {response.status_code}, expected {sorted(allowed)}: "
                    f"{response.text[:300]}"
                )
        return response

    # -- composite flows ---------------------------------------------------- #

    def login(self, password: str = SEED_PASSWORD) -> dict[str, Any]:
        """Open the login page, type, submit — with the events that produces."""
        self.open_page("/login", referrer=f"{WEB_ORIGIN}/")
        human_pause(1.1)
        self.submit("login-form", ["email", "password"])
        human_pause(0.15)
        response = self.api(
            "POST",
            "/api/auth/login",
            json_body={"email": self.email, "password": password},
            expect=200,
        )
        user = response.json()["user"]
        self.user_id = int(user["id"])
        human_pause(0.3)
        self.navigate("/", via="pushState", title="ShopForge")
        return user

    def browse_to_product(self, product_id: int) -> dict[str, Any]:
        self.open_interaction("click")
        self.api("GET", "/api/products", expect=200)
        human_pause(0.9)
        self.click(f"product-card-{product_id}", text_="View", tag="a")
        self.navigate(f"/product/{product_id}", title="Product")
        response = self.api("GET", f"/api/products/{product_id}", expect=200)
        human_pause(1.2)
        return response.json()

    def add_to_cart(self, product_id: int, qty: int = 1) -> dict[str, Any]:
        self.click("add-to-cart", text_="Add to cart")
        human_pause(0.12)
        response = self.api(
            "POST",
            "/api/cart/items",
            json_body={"product_id": product_id, "qty": qty},
            expect=201,
        )
        return response.json()

    def go_to_cart(self) -> dict[str, Any]:
        self.click("nav-cart", text_="Cart", tag="a")
        self.navigate("/cart", title="Your cart")
        response = self.api("GET", "/api/cart", expect=200)
        human_pause(1.4)
        return response.json()

    def go_to_checkout(self) -> dict[str, Any]:
        self.click("checkout-link", text_="Checkout", tag="a")
        self.navigate("/checkout", title="Checkout")
        response = self.api("GET", "/api/cart", expect=200)
        human_pause(1.6)
        return response.json()

    def apply_coupon(self, code: str) -> httpx.Response:
        self.click("coupon-input", text_="", tag="input", listener_ran=False)
        human_pause(0.8)
        self.click("coupon-apply", text_="Apply")
        human_pause(0.1)
        return self.api("POST", "/api/cart/coupon", json_body={"code": code})


# --------------------------------------------------------------------------- #
#  Reading telemetry back  (spec §12 open question 2 — assert, never assume)
# --------------------------------------------------------------------------- #

_QUERY_NOTE_PRINTED = False


#: The keys every stored event carries. Summary objects in the query API
#: (``TraceSummary``, ``ErrorSummary``, ``RequestSummary``) also have ``kind``
#: and ``name``, so a looser test silently harvests summaries as if they were
#: events and every downstream check then counts the wrong things.
_EVENT_KEYS = ("ts", "source", "kind", "name", "attrs")


def _harvest_events(payload: Any) -> list[dict[str, Any]]:
    """Pull stored-event dicts out of a collector response.

    Only ``timeline`` / ``events`` rows survive: a node must carry every key in
    :data:`_EVENT_KEYS` with the right shape.
    """
    found: list[dict[str, Any]] = []

    def looks_like_event(node: dict[str, Any]) -> bool:
        return (
            all(key in node for key in _EVENT_KEYS)
            and isinstance(node.get("kind"), str)
            and isinstance(node.get("source"), str)
            and isinstance(node.get("attrs"), dict)
        )

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if looks_like_event(node):
                found.append(node)
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _get_json(path: str) -> Any | None:
    try:
        response = httpx.get(f"{COLLECTOR_URL}{path}", timeout=10.0)
    except Exception:  # noqa: BLE001
        return None
    if response.status_code >= 400:
        return None
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return None


def _events_from_collector(session_id: str) -> list[dict[str, Any]] | None:
    """Read the session back through the collector's own query API.

    ``/telemetry/session`` returns per-trace *summaries*, not events, so the
    full rows are fetched trace by trace from ``/telemetry/trace``. If that walk
    does not account for every event the session claims to have, we return
    ``None`` and let the caller read ``telemetry.events`` directly rather than
    verify a symptom against a partial picture.
    """
    summary = _get_json(f"/telemetry/session/{session_id}")
    if not isinstance(summary, dict):
        return None

    expected = int(summary.get("event_count") or 0)
    trace_ids = [
        str(trace.get("trace_id"))
        for trace in (summary.get("traces") or [])
        if isinstance(trace, dict) and trace.get("trace_id")
    ]
    if not trace_ids:
        return None

    by_id: dict[Any, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for index, trace_id in enumerate(trace_ids):
        payload = _get_json(f"/telemetry/trace/{trace_id}")
        if payload is None:
            return None
        for event in _harvest_events(payload):
            if event.get("session_id") not in (None, session_id):
                continue
            key = event.get("id")
            if key is None:
                key = (index, event.get("ts"), event.get("kind"), event.get("name"))
            if key in by_id:
                continue
            by_id[key] = event
            ordered.append(event)

    if not ordered or (expected and len(ordered) < expected):
        return None
    return ordered


def _events_from_db(session_id: str) -> list[dict[str, Any]]:
    """Fallback: read ``telemetry.events`` directly.

    The collector is the intended reader, but its query API is a separate
    deliverable; a ghost must be able to verify itself either way. The rows are
    the same rows.
    """
    with dbmod.engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT ts, trace_id, session_id, user_id, source, kind, name,
                           level, duration_ms, attrs
                    FROM "{dbmod.TELEMETRY_SCHEMA}".events
                    WHERE session_id = :sid
                    ORDER BY ts, id
                    """
                ),
                {"sid": session_id},
            )
            .mappings()
            .all()
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        ts = item.get("ts")
        item["ts"] = ts.isoformat() if hasattr(ts, "isoformat") else ts
        item["attrs"] = item.get("attrs") or {}
        out.append(item)
    return out


def session_events(session_id: str) -> list[dict[str, Any]]:
    """Every event recorded for one browser session, web and api, time-ordered."""
    global _QUERY_NOTE_PRINTED
    events = _events_from_collector(session_id)
    if events is None:
        if not _QUERY_NOTE_PRINTED:
            print(
                "    (collector query API not answering — verifying against "
                "telemetry.events directly)"
            )
            _QUERY_NOTE_PRINTED = True
        events = _events_from_db(session_id)
    events.sort(key=lambda e: str(e.get("ts") or ""))
    return events


def wait_for_events(
    session_id: str,
    want: Callable[[list[dict[str, Any]]], bool],
    *,
    timeout: float = TELEMETRY_TIMEOUT,
    label: str = "telemetry",
) -> list[dict[str, Any]]:
    """Poll until ``want`` is satisfied, then return the events.

    Returns whatever it has when the timeout expires; the caller's checks decide
    whether that is a failure. Nothing here raises — a missing symptom must be
    reported as a failed *check*, with the evidence, not as a stack trace.
    """
    deadline = time.monotonic() + timeout
    events: list[dict[str, Any]] = []
    while True:
        events = session_events(session_id)
        if want(events):
            return events
        if time.monotonic() >= deadline:
            return events
        time.sleep(1.5)


# -- event predicates -------------------------------------------------------- #


def select_events(
    events: Iterable[dict[str, Any]],
    *,
    source: str | None = None,
    kind: str | None = None,
    name: str | None = None,
    name_contains: str | None = None,
    level: str | None = None,
    where: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    out = []
    for event in events:
        if source is not None and event.get("source") != source:
            continue
        if kind is not None and event.get("kind") != kind:
            continue
        if name is not None and event.get("name") != name:
            continue
        if name_contains is not None and name_contains not in str(event.get("name") or ""):
            continue
        if level is not None and event.get("level") != level:
            continue
        if where is not None and not where(event):
            continue
        out.append(event)
    return out


def attrs_of(event: dict[str, Any]) -> dict[str, Any]:
    value = event.get("attrs")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:  # noqa: BLE001
            return {}
    return value if isinstance(value, dict) else {}


def requests_to(events: Iterable[dict[str, Any]], route_fragment: str) -> list[dict[str, Any]]:
    """api-side ``request`` events whose route or path contains a fragment."""
    return select_events(
        events,
        source="api",
        kind="request",
        where=lambda e: route_fragment in str(attrs_of(e).get("route") or "")
        or route_fragment in str(attrs_of(e).get("path") or ""),
    )


def summarise(events: Sequence[dict[str, Any]], limit: int = 4) -> str:
    """Compact evidence string for a check's detail line."""
    parts = []
    for event in events[:limit]:
        parts.append(f"{event.get('source')}/{event.get('kind')} {event.get('name')}")
    if len(events) > limit:
        parts.append(f"(+{len(events) - limit} more)")
    return "; ".join(parts) or "nothing"


# --------------------------------------------------------------------------- #
#  Reporting
# --------------------------------------------------------------------------- #


def report(result: GhostResult) -> None:
    status = "OK" if result.ok else "FAILED"
    print(f"  ticket #{result.ticket} [{status}] {result.title}")
    print(f"    persona {result.persona}  session {result.session_id}  events {result.event_count}")
    if result.error:
        print(f"    ERROR: {result.error}")
    for check in result.checks:
        mark = "ok  " if check.ok else "FAIL"
        print(f"    [{mark}] {check.name}" + (f" — {check.detail}" if check.detail else ""))


def print_summary(results: Sequence[GhostResult]) -> None:
    print("")
    print("  ghost run summary")
    print("  " + "-" * 78)
    print(f"  {'ticket':<8}{'persona':<22}{'session':<14}{'checks':<10}{'status'}")
    print("  " + "-" * 78)
    for result in results:
        passed = sum(1 for c in result.checks if c.ok)
        print(
            f"  {result.ticket:<8}{result.persona:<22}{result.session_id:<14}"
            f"{f'{passed}/{len(result.checks)}':<10}{'confirmed' if result.ok else 'NOT CONFIRMED'}"
        )
    print("  " + "-" * 78)
    failures = [r for r in results if not r.ok]
    if failures:
        print("")
        print("  UNCONFIRMED SYMPTOMS — the seeded telemetry is not what the tickets describe:")
        for result in failures:
            if result.error:
                print(f"    #{result.ticket}: {result.error}")
            for check in result.failed:
                print(f"    #{result.ticket}: {check.name} — {check.detail}")
        print("")
        print("  Fix the ghost or the app before trusting anything downstream.")
    else:
        print("  every ticket's symptom was reproduced and confirmed in telemetry")


# --------------------------------------------------------------------------- #
#  Runner
# --------------------------------------------------------------------------- #


def run_module_object(module: Any) -> GhostResult:
    """Run one already-imported ghost module, turning any blow-up into a result.

    A ghost that crashes and a ghost that fails a check are the same thing to
    the caller: the seeded telemetry cannot be trusted.
    """
    try:
        return module.run()
    except GhostFailure as exc:
        return GhostResult(
            ticket=int(getattr(module, "TICKET", 0)),
            title=str(getattr(module, "TITLE", module_name)),
            persona=str(getattr(module, "PERSONA", "?")),
            session_id="-",
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return GhostResult(
            ticket=int(getattr(module, "TICKET", 0)),
            title=str(getattr(module, "TITLE", module_name)),
            persona=str(getattr(module, "PERSONA", "?")),
            session_id="-",
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}",
        )


def run_module(module_name: str) -> GhostResult:
    import importlib

    module = importlib.import_module(module_name)
    reset_all_flags()
    return run_module_object(module)


def run_all() -> tuple[bool, list[GhostResult]]:
    """Every ghost, in order, with the flags reset between them."""
    results: list[GhostResult] = []
    for module_name in GHOST_MODULES:
        result = run_module(module_name)
        results.append(result)
        report(result)
    reset_all_flags()
    print_summary(results)
    return all(r.ok for r in results), results


def cli(module: Any) -> int:
    """``python ticket_1042.py`` — run one ghost on its own."""
    reset_all_flags()
    result = run_module_object(module)
    report(result)
    reset_all_flags()
    if not result.ok:
        print(
            f"ghost #{result.ticket} did NOT reproduce its symptom — see the failed checks above",
            file=sys.stderr,
        )
    return 0 if result.ok else 1


def main() -> int:
    ok, _results = run_all()
    return 0 if ok else 1


if __name__ == "__main__":
    # Re-enter through the real module. Run directly, this file would be
    # ``__main__`` while the ticket scripts import ``run_all`` — two copies of
    # GhostFailure, and an except clause that silently stops matching.
    import run_all as _self

    raise SystemExit(_self.main())
