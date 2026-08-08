"""Runtime bug switches, backed by ``shop.feature_flags``.

Every planted bug toggles at runtime with no rebuild — flip a row in the DB
(``POST /api/debug/flags``) and the next request picks it up within the cache
TTL (~2s).

Usage in application code::

    from app import flags

    if flags.is_enabled(flags.BUG_COUPON_TOCTOU):
        ...buggy read-modify-write...
    else:
        ...SELECT ... FOR UPDATE...

The cache exists so hot paths (checkout, order detail) do not issue a SELECT
per branch. It is deliberately short: a flag flip is visible within 2 seconds,
which is fast enough for a scenario switch and slow enough to be free.
"""

from __future__ import annotations

import os
import threading
import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import SessionLocal
from app.models import FeatureFlag

# --------------------------------------------------------------------------- #
#  Flag keys — one constant per switch named in the spec (§7)
# --------------------------------------------------------------------------- #

#: BUG-001 — checkout reads coupons.uses, computes uses+1, writes it back with
#: no SELECT ... FOR UPDATE. Concurrent checkouts violate CHECK (uses <= max_uses).
BUG_COUPON_TOCTOU = "BUG_COUPON_TOCTOU"

#: BUG-001 (secondary) — the web checkout page swallows a non-2xx response and
#: leaves the Place Order button spinning forever.
BUG_CHECKOUT_SWALLOWS_ERROR = "BUG_CHECKOUT_SWALLOWS_ERROR"

#: BUG-002 — the promo banner's dismiss layer is fixed, transparent and, below
#: 768px, covers the Place Order button. The click never reaches the button.
BUG_PROMO_OVERLAY = "BUG_PROMO_OVERLAY"

#: BUG-003 — the API renames total_cents -> total in the order response while
#: the web app still reads total_cents, rendering $NaN.
BUG_TOTAL_FIELD_RENAME = "BUG_TOTAL_FIELD_RENAME"

#: BUG-004 — GET /api/orders/{id} looks up by primary key with no
#: `AND user_id = current_user`, leaking other customers' orders.
BUG_ORDER_IDOR = "BUG_ORDER_IDOR"


#: Every known flag -> human description. This is the authoritative registry;
#: ``ensure_defaults()`` materialises exactly these rows, all disabled.
FLAG_DESCRIPTIONS: dict[str, str] = {
    BUG_COUPON_TOCTOU: (
        "BUG-001: checkout increments coupons.uses without row-level locking "
        "(no SELECT ... FOR UPDATE), so concurrent checkouts race and the "
        "second UPDATE violates CHECK (uses <= max_uses)."
    ),
    BUG_CHECKOUT_SWALLOWS_ERROR: (
        "BUG-001 secondary: the checkout page ignores non-2xx responses and "
        "never clears the Place Order loading state — the button spins forever."
    ),
    BUG_PROMO_OVERLAY: (
        "BUG-002: the promo banner dismiss layer is position:fixed, transparent "
        "and high z-index; below 768px it covers the Place Order button so the "
        "click lands on the overlay and no request is ever made."
    ),
    BUG_TOTAL_FIELD_RENAME: (
        "BUG-003: the order response serialises the total as `total` instead of "
        "`total_cents`; the web app reads `total_cents` and renders $NaN."
    ),
    BUG_ORDER_IDOR: (
        "BUG-004: GET /api/orders/{id} fetches by primary key only, omitting the "
        "owner check, so any authenticated user can read any order."
    ),
}

#: Stable, sorted list of every flag key.
ALL_FLAG_KEYS: tuple[str, ...] = tuple(sorted(FLAG_DESCRIPTIONS))


# --------------------------------------------------------------------------- #
#  Cache
# --------------------------------------------------------------------------- #

#: Seconds a snapshot of the flag table is considered fresh.
CACHE_TTL_SECONDS: float = float(os.getenv("FLAGS_CACHE_TTL", "2.0"))

_lock = threading.RLock()
_cache: dict[str, bool] = {}
_cache_expires_at: float = 0.0


def _load_from_db() -> dict[str, bool]:
    """Read every flag row. Never raises — a broken DB means 'all bugs off'."""
    values = {key: False for key in ALL_FLAG_KEYS}
    try:
        with SessionLocal() as db:
            for row in db.execute(select(FeatureFlag)).scalars():
                values[row.key] = bool(row.enabled)
    except Exception:
        # The flag table may not exist yet (pre-seed, mid-reset). Treat every
        # switch as off rather than taking the whole API down.
        return values
    return values


def _snapshot(force: bool = False) -> dict[str, bool]:
    """Return the cached flag map, refreshing it if the TTL has elapsed."""
    global _cache, _cache_expires_at
    now = time.monotonic()
    with _lock:
        if force or now >= _cache_expires_at or not _cache:
            _cache = _load_from_db()
            _cache_expires_at = time.monotonic() + CACHE_TTL_SECONDS
        return dict(_cache)


def invalidate() -> None:
    """Drop the cache so the next read hits the database."""
    global _cache_expires_at
    with _lock:
        _cache_expires_at = 0.0


def refresh() -> dict[str, bool]:
    """Force an immediate reload and return the fresh map."""
    return _snapshot(force=True)


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #


def is_enabled(key: str) -> bool:
    """True when the given bug switch is on. Cached for ~2s. Never raises."""
    return bool(_snapshot().get(key, False))


def all_flags() -> dict[str, bool]:
    """Every known flag -> enabled. Unknown/missing rows read as ``False``."""
    return _snapshot()


def set_flag(key: str, enabled: bool) -> bool:
    """Upsert a flag row and invalidate the cache. Returns the stored value.

    Unknown keys are allowed (they simply get an empty description), so future
    scenarios can add switches without touching this module.
    """
    enabled = bool(enabled)
    description = FLAG_DESCRIPTIONS.get(key, "")
    with SessionLocal() as db:
        stmt = (
            pg_insert(FeatureFlag)
            .values(key=key, enabled=enabled, description=description)
            .on_conflict_do_update(
                index_elements=[FeatureFlag.key],
                set_={"enabled": enabled},
            )
        )
        db.execute(stmt)
        db.commit()
    invalidate()
    return enabled


def set_flags(values: dict[str, bool]) -> dict[str, bool]:
    """Apply several flags at once (used when activating a scenario manifest)."""
    for key, enabled in values.items():
        set_flag(key, enabled)
    return all_flags()


def ensure_defaults(enabled: bool = False) -> dict[str, bool]:
    """Materialise a row for every known flag. Existing rows keep their value.

    Called by the seed script and by API start-up so ``GET /api/debug/flags``
    always lists the full switchboard.
    """
    with SessionLocal() as db:
        for key, description in FLAG_DESCRIPTIONS.items():
            stmt = (
                pg_insert(FeatureFlag)
                .values(key=key, enabled=enabled, description=description)
                .on_conflict_do_update(
                    index_elements=[FeatureFlag.key],
                    set_={"description": description},
                )
            )
            db.execute(stmt)
        db.commit()
    invalidate()
    return all_flags()


def reset_flags() -> dict[str, bool]:
    """Turn every known bug switch off."""
    return set_flags({key: False for key in ALL_FLAG_KEYS})


def describe() -> list[dict[str, object]]:
    """Switchboard rendering: ``[{key, enabled, description}, ...]``, sorted."""
    current = all_flags()
    return [
        {
            "key": key,
            "enabled": bool(current.get(key, False)),
            "description": FLAG_DESCRIPTIONS.get(key, ""),
        }
        for key in ALL_FLAG_KEYS
    ]


__all__ = [
    "ALL_FLAG_KEYS",
    "BUG_CHECKOUT_SWALLOWS_ERROR",
    "BUG_COUPON_TOCTOU",
    "BUG_ORDER_IDOR",
    "BUG_PROMO_OVERLAY",
    "BUG_TOTAL_FIELD_RENAME",
    "CACHE_TTL_SECONDS",
    "FLAG_DESCRIPTIONS",
    "all_flags",
    "describe",
    "ensure_defaults",
    "invalidate",
    "is_enabled",
    "refresh",
    "reset_flags",
    "set_flag",
    "set_flags",
]
