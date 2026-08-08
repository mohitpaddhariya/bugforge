"""Harness control plane: bug switches and deterministic reset.

These routes are **not customer-facing** and are excluded from telemetry (see
spec §5), so the robot's own setup calls never pollute the timeline it is about
to read. Nothing in this module calls ``emit``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import text

from app import db as dbmod
from app import flags
from app.routers.cart import clear_all_applied_codes
from app.schemas import ApiError, FlagUpdate, ResetRequest

router = APIRouter(tags=["debug"])

#: The seed script lives in the repo and is bind-mounted into the container.
SEED_SCRIPT = Path(os.getenv("SEED_SCRIPT", "/srv/scripts/seed.py"))
SEED_TIMEOUT_SECONDS = int(os.getenv("SEED_TIMEOUT_SECONDS", "180"))


def _tail(value: str, limit: int = 2000) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else "..." + value[-limit:]


@router.get("/debug/flags")
def read_flags() -> dict:
    """Every known switch, its current value and what it breaks."""
    return {
        "flags": flags.all_flags(),
        "descriptions": flags.describe(),
    }


@router.post("/debug/flags")
def write_flag(payload: FlagUpdate) -> dict:
    """Flip one switch. Takes effect within the flag cache TTL (~2s)."""
    key = payload.key.strip()
    if not key:
        raise ApiError(400, "invalid_flag_key", "A flag key is required.")

    flags.set_flag(key, payload.enabled)
    current = flags.refresh()
    return {
        "key": key,
        "enabled": bool(current.get(key, False)),
        "known": key in flags.ALL_FLAG_KEYS,
        "flags": current,
    }


@router.post("/debug/reset")
def reset(payload: ResetRequest | None = None) -> dict:
    """Return the store to a known state.

    Drops and recreates every ``shop`` table, re-runs ``scripts/seed.py``, and
    puts the bug switches back where the caller asked. Telemetry is left alone
    unless ``telemetry: true`` is passed, and even then only the rows are
    removed — the collector keeps its table and stays up.

    Ghost runs are *not* triggered from here: they are driven by
    ``make reset`` / ``scripts/ghosts`` so a reset from the API never blocks on
    a headless browser.
    """
    payload = payload or ResetRequest()
    result: dict = {"ok": True}

    clear_all_applied_codes()

    if payload.drop:
        # Return pooled connections before DDL so DROP TABLE is not blocked by
        # an idle-in-transaction session from an earlier request.
        dbmod.engine.dispose()
        dbmod.create_all()  # make sure the schemas exist before dropping tables
        dbmod.drop_all()
        dbmod.create_all()
        result["schema"] = "recreated"

    if payload.telemetry:
        try:
            with dbmod.engine.begin() as conn:
                conn.execute(
                    text(
                        f'TRUNCATE TABLE "{dbmod.TELEMETRY_SCHEMA}".events RESTART IDENTITY'
                    )
                )
            result["telemetry"] = "truncated"
        except Exception as exc:  # the collector owns that table; never fatal
            result["telemetry"] = f"skipped: {type(exc).__name__}"

    if payload.seed:
        if SEED_SCRIPT.exists():
            proc = subprocess.run(
                [sys.executable, str(SEED_SCRIPT)],
                capture_output=True,
                text=True,
                timeout=SEED_TIMEOUT_SECONDS,
                cwd=str(SEED_SCRIPT.parent),
            )
            result["seed"] = {
                "ran": True,
                "returncode": proc.returncode,
                "stdout": _tail(proc.stdout),
                "stderr": _tail(proc.stderr),
            }
            result["ok"] = proc.returncode == 0
        else:
            result["seed"] = {"ran": False, "reason": f"{SEED_SCRIPT} not found"}

    flags.ensure_defaults()
    if payload.flags is None:
        result["flags"] = flags.reset_flags()
    else:
        result["flags"] = flags.set_flags(payload.flags)

    return result


__all__ = ["router"]
