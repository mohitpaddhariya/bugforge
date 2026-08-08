"""Runtime for generated reproduction scripts.

A repro script is a plain Python module with three things:

    VIEWPORT = (1440, 900)
    PERSONA  = "priya@example.com"

    SYMPTOM_CHECKS = [
        check("checkout_returns_500", lambda r: r.any_status(500)),
        check("button_stuck_loading", lambda r: r.attr("place-order", "data-loading") == "true"),
    ]

    async def run(ctx):
        await ctx.login()
        ...

The same script asserts "broken" before the fix and "fixed" after, which is what
makes verification mechanical rather than a judgement call.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


def _tid() -> str:
    return "t_" + secrets.token_hex(4)


def _sid() -> str:
    return "s_" + secrets.token_hex(4)


@dataclass
class Check:
    name: str
    fn: Callable[["Recording"], Any]


def check(name: str, fn: Callable[["Recording"], Any]) -> Check:
    """Declare a symptom check. Truthy result == the symptom is present."""
    return Check(name=name, fn=fn)


@dataclass
class Recording:
    """What the run observed. Symptom checks are written against this."""
    requests: list[dict[str, Any]] = field(default_factory=list)
    console: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    session_id: str = ""
    page: Any = None

    # -- convenience helpers so checks stay one-liners -------------------

    def any_status(self, status: int) -> bool:
        return any(r["status"] == status for r in self.requests)

    def requests_to(self, pattern: str) -> list[dict[str, Any]]:
        rx = re.compile(pattern)
        return [r for r in self.requests if rx.search(r["url"])]

    def no_request_to(self, pattern: str) -> bool:
        """The frontend-only-bug check: the click fired but nothing left the browser."""
        return not self.requests_to(pattern)

    def console_matching(self, pattern: str) -> list[dict[str, Any]]:
        rx = re.compile(pattern)
        return [c for c in self.console if rx.search(c.get("text", ""))]

    async def text(self, testid: str) -> str:
        loc = self.page.locator(f'[data-testid="{testid}"]')
        return (await loc.inner_text()) if await loc.count() else ""

    async def attr(self, testid: str, name: str) -> str | None:
        loc = self.page.locator(f'[data-testid="{testid}"]')
        return (await loc.get_attribute(name)) if await loc.count() else None

    async def visible(self, testid: str) -> bool:
        loc = self.page.locator(f'[data-testid="{testid}"]')
        return bool(await loc.count()) and await loc.first.is_visible()


class Ctx:
    """What a generated script drives. Deliberately small and stable — scripts
    written against this keep working as the store changes."""

    def __init__(self, page, context, rec: Recording, base_url: str,
                 api_url: str, persona: str, password: str = "password123",
                 playwright: Any = None):
        self._pw = playwright
        self.page = page
        self.context = context
        self.rec = rec
        self.base_url = base_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.persona = persona
        self.password = password

    def new_trace(self) -> str:
        t = _tid()
        self.rec.trace_ids.append(t)
        return t

    async def goto(self, path: str = "/"):
        await self.page.goto(f"{self.base_url}{path}", wait_until="domcontentloaded")

    async def click(self, testid: str, **kw):
        await self.page.locator(f'[data-testid="{testid}"]').first.click(**kw)

    async def fill(self, testid: str, value: str):
        await self.page.locator(f'[data-testid="{testid}"]').first.fill(value)

    async def wait(self, ms: int):
        await self.page.wait_for_timeout(ms)

    async def login(self, email: str | None = None, password: str | None = None):
        await self.goto("/login")
        await self.fill("login-email", email or self.persona)
        await self.fill("login-password", password or self.password)
        await self.click("login-submit")
        await self.page.wait_for_load_state("networkidle")

    async def api(self, method: str, path: str, *, json_body: Any = None,
                  trace_id: str | None = None) -> dict[str, Any]:
        """Raw API call sharing the browser's cookies. Use for concurrency —
        races cannot be hit through step-by-step UI actions."""
        headers = {"X-Trace-Id": trace_id or self.new_trace(),
                   "X-Session-Id": self.rec.session_id}
        if json_body is not None:
            # Without this the body arrives as text/plain, the server cannot
            # parse it, and every authenticated call afterwards is a 401.
            headers["content-type"] = "application/json"
        resp = await self.context.request.fetch(
            f"{self.api_url}{path}", method=method, headers=headers,
            data=json_body if json_body is None else json.dumps(json_body),
        )
        try:
            body = await resp.json()
        except Exception:  # noqa: BLE001
            body = await resp.text()
        out = {"method": method, "url": f"{self.api_url}{path}",
               "status": resp.status, "body": body}
        self.rec.requests.append(out)
        return out

    async def rival(self, email: str, password: str = "password123"):
        """An independent logged-in API session for a *second* shopper.

        Racing two requests from the same browser context would mean logging in
        as the rival and clobbering the first shopper's cookie. Concurrency
        between two real users needs two real sessions.

        Returns an async ``call(method, path, json_body=None)``.
        """
        rq = await self._pw.request.new_context(base_url=self.api_url)
        await rq.post("/api/auth/login",
                      data=json.dumps({"email": email, "password": password}),
                      headers={"content-type": "application/json"})
        # Warm the connection. A fresh context pays TCP setup on its first real
        # call, which is enough to lose every race it is supposed to contend.
        await rq.get("/api/me")

        async def call(method: str, path: str, json_body: Any = None):
            headers = {"X-Trace-Id": self.new_trace(),
                       "X-Session-Id": self.rec.session_id}
            if json_body is not None:
                headers["content-type"] = "application/json"
            resp = await rq.fetch(path, method=method, headers=headers,
                                  data=None if json_body is None else json.dumps(json_body))
            out = {"method": method, "url": f"{self.api_url}{path}",
                   "status": resp.status, "body": None}
            self.rec.requests.append(out)
            return out

        return call

    async def parallel(self, *coros):
        """Fire several things genuinely at once. Required for race conditions."""
        return await asyncio.gather(*coros, return_exceptions=True)


async def execute(module, *, label: str, out_dir: Path, base_url: str,
                  api_url: str, headed: bool = False,
                  viewport: tuple[int, int] | None = None,
                  persona: str | None = None) -> dict[str, Any]:
    """Run a repro module and return a verdict."""
    from playwright.async_api import async_playwright

    out_dir = Path(out_dir) / label
    # Start clean: Playwright names each video after the page, so re-runs
    # accumulate files and a later step cannot tell which one is current.
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    vw, vh = viewport or getattr(module, "VIEWPORT", (1440, 900))
    persona = persona or getattr(module, "PERSONA", "")
    checks: list[Check] = list(getattr(module, "SYMPTOM_CHECKS", []))
    rec = Recording(session_id=_sid())

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        context = await browser.new_context(
            viewport={"width": vw, "height": vh},
            record_video_dir=str(out_dir / "video"),
            record_har_path=str(out_dir / "network.har"),
        )
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)

        # every browser request carries our IDs, so the run is joinable to telemetry
        await context.set_extra_http_headers({"X-Session-Id": rec.session_id})

        page = await context.new_page()
        rec.page = page

        page.on("console", lambda m: rec.console.append({"type": m.type, "text": m.text}))
        page.on("pageerror", lambda e: rec.page_errors.append(str(e)))

        def _on_response(resp):
            hdrs = resp.request.headers
            tid = hdrs.get("x-trace-id")
            if tid and tid not in rec.trace_ids:
                rec.trace_ids.append(tid)
            rec.requests.append({"method": resp.request.method, "url": resp.url,
                                 "status": resp.status, "body": None})

        page.on("response", _on_response)

        ctx = Ctx(page, context, rec, base_url, api_url, persona, playwright=pw)

        error = None
        try:
            await module.run(ctx)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

        results = []
        for c in checks:
            try:
                val = c.fn(rec)
                if asyncio.iscoroutine(val):
                    val = await val
                results.append({"name": c.name, "present": bool(val)})
            except Exception as exc:  # noqa: BLE001
                results.append({"name": c.name, "present": False, "error": str(exc)})

        await context.tracing.stop(path=str(out_dir / "trace.zip"))
        (out_dir / "console.log").write_text(
            "\n".join(f"[{c['type']}] {c['text']}" for c in rec.console))
        (out_dir / "requests.json").write_text(json.dumps(rec.requests, indent=2))
        await context.close()
        await browser.close()

    videos = list((out_dir / "video").glob("*.webm"))
    return {
        "label": label,
        "symptom_detected": bool(results) and all(r["present"] for r in results),
        "checks": results,
        "trace_ids": rec.trace_ids,
        "session_id": rec.session_id,
        "script_error": error,
        "artifacts": {
            "video": str(videos[0]) if videos else None,
            "trace": str(out_dir / "trace.zip"),
            "har": str(out_dir / "network.har"),
            "console": str(out_dir / "console.log"),
            "requests": str(out_dir / "requests.json"),
        },
    }
