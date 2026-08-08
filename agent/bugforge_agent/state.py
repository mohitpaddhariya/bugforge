"""Run state on disk, not in context.

Harnesses differ in context size and some compact or restart mid-run. A run is a
directory so any harness can resume one it did not start.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PHASES = [
    "intake",
    "investigated",
    "reproduced",
    "decided",
    "test_written",
    "fixed",
    "verified",
    "pr_opened",
    "escalated",
    "closed_no_change",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class State:
    def __init__(self, run_dir: Path, ticket: str):
        self.path = run_dir / "state.json"
        self.ticket = str(ticket)
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except json.JSONDecodeError:
                pass
        return {
            "ticket": self.ticket,
            "phase": "intake",
            "created_at": _now(),
            "updated_at": _now(),
            "hypothesis": None,
            "repro_attempts": [],
            "fix_attempts": [],
            "decisions": [],
            "artifacts": {},
        }

    def save(self) -> None:
        self.data["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    # --- mutations -------------------------------------------------------

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def phase(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase!r}; expected one of {PHASES}")
        self.data["phase"] = phase
        self.data.setdefault("phase_history", []).append({"phase": phase, "at": _now()})
        self.save()

    def note(self, kind: str, text: str, **extra: Any) -> None:
        self.data["decisions"].append({"at": _now(), "kind": kind, "text": text, **extra})
        self.save()

    def record_repro(self, attempt: dict[str, Any]) -> int:
        attempt = {"at": _now(), **attempt}
        self.data["repro_attempts"].append(attempt)
        self.save()
        return len(self.data["repro_attempts"])

    def record_fix(self, attempt: dict[str, Any]) -> int:
        self.data["fix_attempts"].append({"at": _now(), **attempt})
        self.save()
        return len(self.data["fix_attempts"])

    def artifact(self, name: str, path: str | Path) -> None:
        self.data["artifacts"][name] = str(path)
        self.save()

    # --- budgets ---------------------------------------------------------

    MAX_REPRO_ATTEMPTS = 3
    MAX_FIX_ATTEMPTS = 2

    @property
    def repro_budget_left(self) -> int:
        return self.MAX_REPRO_ATTEMPTS - len(self.data["repro_attempts"])

    @property
    def fix_budget_left(self) -> int:
        return self.MAX_FIX_ATTEMPTS - len(self.data["fix_attempts"])
