from __future__ import annotations

from typing import Any

import httpx

from . import register

TIMEOUT = 20.0


class TicketAdapter:
    """Contract: list() -> [ticket], get(id) -> ticket.

    ticket = {id, subject, body, customer_email, customer_name, status,
              opened_at, browser, device}
    """

    def __init__(self, cfg):
        self.cfg = cfg

    def list(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get(self, ticket_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def health(self) -> tuple[bool, str]:
        try:
            self.list()
            return True, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


@register("tickets", "supportdesk")
class SupportdeskTickets(TicketAdapter):
    def list(self):
        r = httpx.get(f"{self.cfg.url}/api/tickets", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data.get("tickets", data) if isinstance(data, dict) else data

    def get(self, ticket_id):
        r = httpx.get(f"{self.cfg.url}/api/tickets/{ticket_id}", timeout=TIMEOUT)
        if r.status_code == 404:
            raise SystemExit(f"ticket {ticket_id} not found")
        r.raise_for_status()
        return r.json()


@register("tickets", "github-issues")
class GithubIssues(TicketAdapter):
    """cfg.opts: repo ("owner/name"), token"""

    def _hdr(self):
        tok = self.cfg.opts.get("token", "")
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def _norm(self, i: dict) -> dict:
        return {
            "id": str(i["number"]),
            "subject": i["title"],
            "body": i.get("body") or "",
            "customer_email": "",
            "customer_name": (i.get("user") or {}).get("login", ""),
            "status": i.get("state", "open"),
            "opened_at": i.get("created_at", ""),
            "browser": "",
            "device": "",
        }

    def list(self):
        repo = self.cfg.opts["repo"]
        r = httpx.get(f"https://api.github.com/repos/{repo}/issues",
                      headers=self._hdr(), timeout=TIMEOUT)
        r.raise_for_status()
        return [self._norm(i) for i in r.json() if "pull_request" not in i]

    def get(self, ticket_id):
        repo = self.cfg.opts["repo"]
        r = httpx.get(f"https://api.github.com/repos/{repo}/issues/{ticket_id}",
                      headers=self._hdr(), timeout=TIMEOUT)
        r.raise_for_status()
        return self._norm(r.json())


@register("tickets", "file")
class FileTickets(TicketAdapter):
    """Offline fallback. cfg.opts: path to a YAML/JSON list of tickets."""

    def _all(self):
        import json
        from pathlib import Path

        import yaml

        p = Path(self.cfg.opts["path"])
        text = p.read_text()
        return yaml.safe_load(text) if p.suffix in (".yaml", ".yml") else json.loads(text)

    def list(self):
        return self._all()

    def get(self, ticket_id):
        for t in self._all():
            if str(t["id"]) == str(ticket_id):
                return t
        raise SystemExit(f"ticket {ticket_id} not found")
