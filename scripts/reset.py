#!/usr/bin/env python3
"""One-command deterministic reset (spec §8.3, data half).

    docker compose exec -T api python /srv/scripts/reset.py

Does, in order:

1. drop and recreate the ``shop`` **and** ``telemetry`` schemas;
2. recreate every ``shop`` table, plus ``telemetry.events``;
3. run ``scripts/seed.py``;
4. run every ghost script, which drives the real app and leaves authentic
   historical telemetry behind;
5. leave every bug flag **off**.

Idempotent: run it as many times as you like, the end state is identical (the
seed checksum is printed so you can prove it).

Why this file recreates ``telemetry.events`` itself
---------------------------------------------------
The collector owns that table and creates it at start-up. But dropping the
schema out from under a *running* collector would leave it pointing at nothing
until someone restarted it, and the whole point of a separate collector is that
it stays up. So the DDL below mirrors ``collector/app/models.py`` exactly and is
written with ``IF NOT EXISTS`` — whoever gets there first wins, and the shapes
agree. ``collector/app/models.py`` remains the authority; if a column is added
there, add it here too.

The code half of the reset (``git reset --hard`` to the scenario baseline) is
deliberately not here: this script runs inside a container that has no business
touching the host's git tree. The Makefile owns that.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_GHOSTS = os.path.join(_HERE, "ghosts")
for _candidate in ("/srv", os.path.dirname(_HERE)):
    if _candidate and _candidate not in sys.path and os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)
for _candidate in (_HERE, _GHOSTS):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from sqlalchemy import text  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import flags  # noqa: E402

import seed as seed_module  # noqa: E402

# --------------------------------------------------------------------------- #
#  telemetry.events — mirrors collector/app/models.py
# --------------------------------------------------------------------------- #

_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS {schema}.events (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ      NOT NULL,
    received_at  TIMESTAMPTZ      NOT NULL DEFAULT now(),
    trace_id     VARCHAR(64),
    session_id   VARCHAR(64),
    user_id      INTEGER,
    source       VARCHAR(16)      NOT NULL DEFAULT 'web',
    kind         VARCHAR(32)      NOT NULL DEFAULT 'business',
    name         VARCHAR(255)     NOT NULL DEFAULT '',
    level        VARCHAR(16)      NOT NULL DEFAULT 'info',
    duration_ms  DOUBLE PRECISION,
    attrs        JSONB            NOT NULL DEFAULT '{{}}'::jsonb
)
"""

_EVENTS_INDEXES = (
    ("ix_events_trace_id", "(trace_id)"),
    ("ix_events_session_id", "(session_id)"),
    ("ix_events_user_id", "(user_id)"),
    ("ix_events_ts", "(ts)"),
    ("ix_events_trace_id_ts", "(trace_id, ts)"),
    ("ix_events_session_id_ts", "(session_id, ts)"),
    ("ix_events_kind_ts", "(kind, ts)"),
    ("ix_events_level_ts", "(level, ts)"),
    ("ix_events_name", "(name)"),
    ("ix_events_source_ts", "(source, ts)"),
)


def create_telemetry_tables() -> None:
    schema = dbmod.TELEMETRY_SCHEMA
    with dbmod.engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        conn.execute(text(_EVENTS_DDL.format(schema=f'"{schema}"')))
        for name, columns in _EVENTS_INDEXES:
            conn.execute(
                text(f'CREATE INDEX IF NOT EXISTS {name} ON "{schema}".events {columns}')
            )


# --------------------------------------------------------------------------- #
#  Waiting
# --------------------------------------------------------------------------- #


def wait_for_db(timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dbmod.ping():
            return
        time.sleep(1.0)
    raise SystemExit("reset: database never became reachable")


def wait_for_api(timeout: float = 60.0) -> bool:
    """The ghosts drive the running API over HTTP, so it has to be answering."""
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with the api image
        return False

    base = os.getenv("API_URL", "http://api:8000").rstrip("/")
    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base}/api/products", timeout=3.0)
            if response.status_code < 500:
                return True
            last = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        time.sleep(1.0)
    print(f"reset: api not reachable at {base} ({last})", file=sys.stderr)
    return False


# --------------------------------------------------------------------------- #
#  Steps
# --------------------------------------------------------------------------- #


def recreate_schemas() -> None:
    dbmod.engine.dispose()
    dbmod.drop_schemas()
    dbmod.ensure_schemas()
    dbmod.create_all()
    create_telemetry_tables()


def run_seed() -> dict[str, Any]:
    return seed_module.seed()


def run_ghosts() -> tuple[bool, list[Any]]:
    import run_all  # scripts/ghosts/run_all.py

    return run_all.run_all()


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic BugForge data reset")
    parser.add_argument("--no-seed", action="store_true", help="skip the base seed")
    parser.add_argument("--no-ghosts", action="store_true", help="skip the ghost runs")
    parser.add_argument(
        "--keep-telemetry",
        action="store_true",
        help="keep existing telemetry.events rows instead of dropping the schema",
    )
    args = parser.parse_args(argv)

    print("==> waiting for postgres")
    wait_for_db()

    print("==> dropping + recreating schemas")
    if args.keep_telemetry:
        dbmod.engine.dispose()
        dbmod.drop_schemas(dbmod.SHOP_SCHEMA)
        dbmod.ensure_schemas()
        dbmod.create_all()
        create_telemetry_tables()
    else:
        recreate_schemas()
    print(f"    shop + telemetry ready (telemetry kept: {args.keep_telemetry})")

    if not args.no_seed:
        print("==> seeding")
        result = run_seed()
        print(
            f"    {result['users']} users, {result['products']} products, "
            f"{result['coupons']} coupons, {result['orders']} orders"
        )
        print(f"    checksum {result['checksum']}")

    ok = True
    if not args.no_ghosts:
        print("==> ghost runs")
        if not wait_for_api():
            print("reset: FAILED — the api must be up for ghost runs", file=sys.stderr)
            return 1
        ok, _results = run_ghosts()

    # Whatever the ghosts turned on, the harness hands back a clean switchboard.
    flags.ensure_defaults()
    flags.reset_flags()
    print("==> all bug flags off")

    print("==> reset complete" if ok else "==> reset complete WITH GHOST FAILURES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
