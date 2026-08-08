from __future__ import annotations

from typing import Any

import httpx

from . import register

TIMEOUT = 30.0


class TelemetryAdapter:
    """Contract: search, session, trace, bundle.

    `bundle` is the only hard requirement — the investigation step depends on it.
    If a backend has no native equivalent, compose one; the skill only cares about
    the shape:

        {trace_id, summary, rendered[], events[], stack_frames[],
         implicated_files[], response_shapes{}, preceding_actions[]}
    """

    def __init__(self, cfg):
        self.cfg = cfg

    def search(self, **kw) -> dict[str, Any]:
        raise NotImplementedError

    def session(self, session_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def trace(self, trace_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def bundle(self, trace_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def health(self) -> tuple[bool, str]:
        try:
            r = httpx.get(f"{self.cfg.url}/health", timeout=10)
            return r.status_code < 500, f"http {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


@register("telemetry", "bugforge")
class BugforgeTelemetry(TelemetryAdapter):
    def _get(self, path: str, params: dict | None = None):
        r = httpx.get(f"{self.cfg.url}{path}", params=params or {}, timeout=TIMEOUT)
        if r.status_code == 404:
            raise SystemExit(f"not found: {path}")
        r.raise_for_status()
        return r.json()

    def search(self, user=None, since=None, until=None, level=None,
               kind=None, name=None, text=None, limit=20):
        params = {k: v for k, v in {
            "user": user, "since": since, "until": until, "level": level,
            "kind": kind, "name": name, "text": text, "limit": limit,
        }.items() if v is not None}
        return self._get("/telemetry/search", params)

    def session(self, session_id):
        return self._get(f"/telemetry/session/{session_id}")

    def trace(self, trace_id):
        return self._get(f"/telemetry/trace/{trace_id}")

    def bundle(self, trace_id):
        return self._get(f"/telemetry/bundle/{trace_id}")


@register("telemetry", "sentry")
class SentryTelemetry(TelemetryAdapter):
    """Backend-only telemetry.

    Degrades in one specific way, documented in adapters.md: pure-frontend bugs
    (a click that fires no request) leave no trace here, so those tickets become
    reproduce-first instead of investigate-first.
    """

    def _hdr(self):
        return {"Authorization": f"Bearer {self.cfg.opts['token']}"}

    def _base(self):
        org = self.cfg.opts["org"]
        return f"{self.cfg.url or 'https://sentry.io'}/api/0/organizations/{org}"

    def search(self, user=None, since=None, text=None, limit=20, **_):
        q = []
        if user:
            q.append(f"user.email:{user}")
        if text:
            q.append(text)
        r = httpx.get(f"{self._base()}/issues/",
                      params={"query": " ".join(q), "limit": limit,
                              "statsPeriod": (since or "7d")},
                      headers=self._hdr(), timeout=TIMEOUT)
        r.raise_for_status()
        return {"sessions": [
            {"session_id": i["id"], "summary": i["title"],
             "error_count": int(i.get("count", 0)), "last_seen": i.get("lastSeen")}
            for i in r.json()
        ]}

    def session(self, session_id):
        r = httpx.get(f"{self.cfg.url or 'https://sentry.io'}/api/0/issues/{session_id}/events/latest/",
                      headers=self._hdr(), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def trace(self, trace_id):
        return self.session(trace_id)

    def bundle(self, trace_id):
        ev = self.session(trace_id)
        frames = []
        for entry in ev.get("entries", []):
            if entry.get("type") == "exception":
                for val in entry["data"].get("values", []):
                    for f in (val.get("stacktrace") or {}).get("frames", []):
                        if f.get("inApp"):
                            frames.append({"file": f.get("filename"),
                                           "line": f.get("lineNo"),
                                           "function": f.get("function")})
        return {
            "trace_id": trace_id,
            "summary": ev.get("title", ""),
            "rendered": [ev.get("title", "")],
            "events": [],
            "stack_frames": frames,
            "implicated_files": sorted({f["file"] for f in frames if f.get("file")}),
            "response_shapes": {},
            "preceding_actions": ev.get("breadcrumbs", {}).get("values", []),
            "_degraded": "backend-only telemetry: frontend-only bugs are invisible here",
        }

    def health(self):
        try:
            self.search(limit=1)
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
