#!/usr/bin/env python3
"""Deterministic base seed for ShopForge (spec §8.1).

Run inside the ``api`` container, where ``PYTHONPATH=/srv`` makes ``app.*``
importable::

    docker compose exec -T api python /srv/scripts/seed.py

Everything here is fixed. No randomness, no ``now()``, no faker. Two runs of
this script against an empty schema produce byte-identical rows, which is what
makes ``make reset`` verifiable — the script prints a checksum of everything it
wrote (password hashes excluded, since bcrypt salts them).

What gets written
-----------------
* **12 products** over 3 categories, 1200–24900 cents.
* **5 users**, all with password ``password123``.
* **3 coupons**. ``SAVE20`` is seeded at ``uses=4`` of ``max_uses=5`` — one
  redemption from the edge — so the BUG-001 race is reachable on the very next
  checkout instead of needing five warm-up runs. Those four uses are not
  invented: four of the historical orders below actually carry ``SAVE20``.
* **11 historical orders** spread over the three weeks before the tickets were
  written. Two of them are load-bearing:

  - ``arjun`` owns the **Aurora Wireless Headphones** order. That is the order
    ``priya`` can read through BUG-004 (``GET /api/orders/{id}`` with no owner
    check).
  - ``priya`` owns the **Moss Green Field Jacket** order. That is the order
    ``arjun`` stumbles into in ticket #1045 ("it showed a jacket I never
    bought"), and it is what ghost run 1045 reads.

* **Every feature flag row**, all disabled. The switchboard is always complete
  so ``GET /api/debug/flags`` lists the full set before any scenario is chosen.

Money is integer cents throughout, and order totals are computed with the same
arithmetic ``api/app/routers/cart.py`` uses (percent discount floor-divided by
100, tax at 800 bps of the discounted subtotal, floor-divided), so a seeded
order and a freshly placed one are indistinguishable.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

# Make ``app.*`` importable when this file is run by path from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in ("/srv", os.path.dirname(_HERE)):
    if _candidate and _candidate not in sys.path and os.path.isdir(os.path.join(_candidate, "app")):
        sys.path.insert(0, _candidate)

from sqlalchemy import select, text  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import flags  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.models import (  # noqa: E402
    Coupon,
    Order,
    OrderItem,
    Product,
    User,
)

# --------------------------------------------------------------------------- #
#  Money — mirrors app/routers/cart.py so seeded orders match placed orders
# --------------------------------------------------------------------------- #

#: Sales tax in basis points (800 = 8.00%), applied to the discounted subtotal.
TAX_BPS = 800

#: Every seeded user shares this password (spec §8.1).
SEED_PASSWORD = "password123"


def _utc(value: str) -> datetime:
    """``"2026-07-12T10:22:04"`` -> aware UTC datetime."""
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def discount_for(kind: str | None, value: int, subtotal_cents: int) -> int:
    if not kind or subtotal_cents <= 0:
        return 0
    raw = (subtotal_cents * int(value)) // 100 if kind == "percent" else int(value)
    return max(0, min(raw, subtotal_cents))


def tax_for(taxable_cents: int) -> int:
    return max(0, (taxable_cents * TAX_BPS) // 10_000)


# --------------------------------------------------------------------------- #
#  Products — 12, three categories, 1200..24900 cents
# --------------------------------------------------------------------------- #

PRODUCTS: list[dict[str, Any]] = [
    # ── audio ────────────────────────────────────────────────────────────── #
    {
        "id": 1,
        "sku": "SF-AUD-001",
        "name": "Aurora Wireless Headphones",
        "description": "Over-ear, 40h battery, adaptive noise cancelling. The one everybody orders.",
        "price_cents": 18900,
        "category": "audio",
        "image_url": "/images/aurora-headphones.svg",
        "stock": 42,
    },
    {
        "id": 2,
        "sku": "SF-AUD-002",
        "name": "Pebble Bluetooth Speaker",
        "description": "Palm-sized, splash resistant, 12h of playback.",
        "price_cents": 6400,
        "category": "audio",
        "image_url": "/images/pebble-speaker.svg",
        "stock": 88,
    },
    {
        "id": 3,
        "sku": "SF-AUD-003",
        "name": "Trailhead Earbuds",
        "description": "Sweat-proof buds with a magnetic charging case.",
        "price_cents": 9900,
        "category": "audio",
        "image_url": "/images/trailhead-earbuds.svg",
        "stock": 60,
    },
    {
        "id": 4,
        "sku": "SF-AUD-004",
        "name": "Lapis Studio Monitors",
        "description": "Near-field reference pair. Flat, honest, unforgiving.",
        "price_cents": 24900,
        "category": "audio",
        "image_url": "/images/lapis-monitors.svg",
        "stock": 12,
    },
    # ── apparel ──────────────────────────────────────────────────────────── #
    {
        "id": 5,
        "sku": "SF-APP-001",
        "name": "Moss Green Field Jacket",
        "description": "Waxed cotton, four pockets, cut for layering.",
        "price_cents": 15900,
        "category": "apparel",
        "image_url": "/images/field-jacket.svg",
        "stock": 25,
    },
    {
        "id": 6,
        "sku": "SF-APP-002",
        "name": "Everyday Merino Tee",
        "description": "Lightweight merino that survives a week of travel.",
        "price_cents": 4200,
        "category": "apparel",
        "image_url": "/images/merino-tee.svg",
        "stock": 140,
    },
    {
        "id": 7,
        "sku": "SF-APP-003",
        "name": "Canvas Weekender Bag",
        "description": "Waxed canvas, leather handles, fits a two-night trip.",
        "price_cents": 12400,
        "category": "apparel",
        "image_url": "/images/weekender-bag.svg",
        "stock": 30,
    },
    {
        "id": 8,
        "sku": "SF-APP-004",
        "name": "Ribbed Wool Beanie",
        "description": "Lambswool, folded brim, one size.",
        "price_cents": 1900,
        "category": "apparel",
        "image_url": "/images/wool-beanie.svg",
        "stock": 200,
    },
    # ── home ─────────────────────────────────────────────────────────────── #
    {
        "id": 9,
        "sku": "SF-HOM-001",
        "name": "Cedar & Smoke Candle",
        "description": "Soy wax, cotton wick, 50 hours.",
        "price_cents": 2400,
        "category": "home",
        "image_url": "/images/cedar-candle.svg",
        "stock": 150,
    },
    {
        "id": 10,
        "sku": "SF-HOM-002",
        "name": "Stoneware Mug Set",
        "description": "Four mugs, reactive glaze, no two identical.",
        "price_cents": 3600,
        "category": "home",
        "image_url": "/images/mug-set.svg",
        "stock": 75,
    },
    {
        "id": 11,
        "sku": "SF-HOM-003",
        "name": "Washed Linen Throw",
        "description": "Stonewashed European flax, 130x180cm.",
        "price_cents": 8800,
        "category": "home",
        "image_url": "/images/linen-throw.svg",
        "stock": 48,
    },
    {
        "id": 12,
        "sku": "SF-HOM-004",
        "name": "Folding Desk Lamp",
        "description": "Matte aluminium, three brightness steps, USB-C.",
        "price_cents": 1200,
        "category": "home",
        "image_url": "/images/desk-lamp.svg",
        "stock": 110,
    },
]

#: Handy aliases used by the order fixtures below and by the ghost runs.
P_HEADPHONES = 1
P_SPEAKER = 2
P_EARBUDS = 3
P_MONITORS = 4
P_JACKET = 5
P_TEE = 6
P_BAG = 7
P_BEANIE = 8
P_CANDLE = 9
P_MUGS = 10
P_THROW = 11
P_LAMP = 12


# --------------------------------------------------------------------------- #
#  Users — password123 for every one of them
# --------------------------------------------------------------------------- #

USERS: list[dict[str, Any]] = [
    {
        "id": 1,
        "email": "priya@example.com",
        "name": "Priya Nair",
        "locale": "en-IN",
        "created_at": "2025-11-04T09:12:00",
    },
    {
        "id": 2,
        "email": "arjun@example.com",
        "name": "Arjun Mehta",
        "locale": "en-IN",
        "created_at": "2025-12-18T17:40:00",
    },
    {
        "id": 3,
        "email": "mei@example.com",
        "name": "Mei Tanaka",
        "locale": "en-GB",
        "created_at": "2026-01-27T11:05:00",
    },
    {
        "id": 4,
        "email": "rahul@example.com",
        "name": "Rahul Verma",
        "locale": "en-IN",
        "created_at": "2025-09-30T08:22:00",
    },
    {
        "id": 5,
        "email": "sofia@example.com",
        "name": "Sofia Ramos",
        "locale": "es-ES",
        "created_at": "2026-03-14T20:01:00",
    },
]

U_PRIYA = 1
U_ARJUN = 2
U_MEI = 3
U_RAHUL = 4
U_SOFIA = 5


# --------------------------------------------------------------------------- #
#  Coupons
# --------------------------------------------------------------------------- #

COUPONS: list[dict[str, Any]] = [
    {
        # Primed one redemption from the edge. See module docstring.
        "id": 1,
        "code": "SAVE20",
        "kind": "percent",
        "value": 20,
        "max_uses": 5,
        "uses": 4,
        "min_subtotal_cents": 0,
        "expires_at": "2026-12-31T23:59:59",
        "active": True,
    },
    {
        "id": 2,
        "code": "WELCOME10",
        "kind": "fixed",
        "value": 1000,
        "max_uses": 100,
        "uses": 1,
        "min_subtotal_cents": 2000,
        "expires_at": "2026-12-31T23:59:59",
        "active": True,
    },
    {
        # BUG-005: genuinely expired. The API is right to reject it. The seven
        # uses are from before it lapsed — it was a real, working code once,
        # which is why rahul believes it should still work.
        "id": 3,
        "code": "EXPIRED15",
        "kind": "percent",
        "value": 15,
        "max_uses": 50,
        "uses": 7,
        "min_subtotal_cents": 0,
        "expires_at": "2026-06-30T23:59:59",
        "active": True,
    },
]

_COUPON_BY_CODE = {c["code"]: c for c in COUPONS}


# --------------------------------------------------------------------------- #
#  Historical orders
# --------------------------------------------------------------------------- #
#
# ``items`` are ``(product_id, qty)``. Totals are computed, never hardcoded.
# ``SAVE20`` appears on exactly four of these, which is where its uses=4 comes
# from — the seeded counter is a consequence of the history, not a magic number.

ORDERS: list[dict[str, Any]] = [
    {
        "id": 1,
        "user_id": U_ARJUN,
        "created_at": "2026-07-09T20:14:55",
        "status": "delivered",
        "coupon_code": "SAVE20",
        # ── BUG-004 target: the order priya can read that isn't hers ──────── #
        "items": [(P_HEADPHONES, 1)],
    },
    {
        "id": 2,
        "user_id": U_PRIYA,
        "created_at": "2026-07-12T10:22:04",
        "status": "delivered",
        "coupon_code": "WELCOME10",
        "items": [(P_EARBUDS, 1)],
    },
    {
        "id": 3,
        "user_id": U_MEI,
        "created_at": "2026-07-15T07:58:19",
        "status": "delivered",
        "coupon_code": "SAVE20",
        "items": [(P_TEE, 2)],
    },
    {
        "id": 4,
        "user_id": U_RAHUL,
        "created_at": "2026-07-19T16:03:28",
        "status": "delivered",
        "coupon_code": "SAVE20",
        "items": [(P_MONITORS, 1)],
    },
    {
        "id": 5,
        "user_id": U_SOFIA,
        "created_at": "2026-07-21T11:44:09",
        "status": "delivered",
        "coupon_code": "SAVE20",
        "items": [(P_BAG, 1)],
    },
    {
        "id": 6,
        "user_id": U_PRIYA,
        "created_at": "2026-07-24T18:41:30",
        "status": "shipped",
        "coupon_code": None,
        # ── ticket #1045: the green jacket arjun says he never bought ─────── #
        "items": [(P_JACKET, 1), (P_BEANIE, 1)],
    },
    {
        "id": 7,
        "user_id": U_ARJUN,
        "created_at": "2026-07-28T13:37:02",
        "status": "shipped",
        "coupon_code": None,
        "items": [(P_THROW, 1), (P_CANDLE, 2)],
    },
    {
        "id": 8,
        "user_id": U_MEI,
        "created_at": "2026-08-01T12:11:47",
        "status": "shipped",
        "coupon_code": None,
        "items": [(P_SPEAKER, 1)],
    },
    {
        "id": 9,
        "user_id": U_PRIYA,
        "created_at": "2026-08-02T09:05:12",
        "status": "placed",
        "coupon_code": None,
        "items": [(P_MUGS, 2)],
    },
    {
        "id": 10,
        "user_id": U_RAHUL,
        "created_at": "2026-08-03T19:22:40",
        "status": "placed",
        "coupon_code": None,
        "items": [(P_LAMP, 1), (P_CANDLE, 1)],
    },
    {
        "id": 11,
        "user_id": U_SOFIA,
        "created_at": "2026-08-04T08:30:55",
        "status": "placed",
        "coupon_code": None,
        "items": [(P_BEANIE, 2), (P_TEE, 1)],
    },
]

#: The order ``priya`` leaks through BUG-004 (owned by ``arjun``).
IDOR_TARGET_ORDER_ID = 1
#: The order ``arjun`` sees in ticket #1045 (owned by ``priya``, has the jacket).
JACKET_ORDER_ID = 6


# --------------------------------------------------------------------------- #
#  Writing
# --------------------------------------------------------------------------- #

_PRODUCT_BY_ID = {p["id"]: p for p in PRODUCTS}

#: Order in which shop tables are emptied (children first). ``sessions`` and the
#: carts are included so a reseed leaves nobody logged in with a stale cart.
_TRUNCATE_ORDER = (
    "order_items",
    "orders",
    "cart_items",
    "carts",
    "sessions",
    "coupons",
    "products",
    "users",
    "feature_flags",
)


def wipe(conn) -> None:
    """Empty every ``shop`` table and restart its identity sequence."""
    schema = dbmod.SHOP_SCHEMA
    tables = ", ".join(f'"{schema}"."{name}"' for name in _TRUNCATE_ORDER)
    conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


def _resync_sequences(conn) -> None:
    """Point each serial sequence past the highest explicitly inserted id.

    Rows are inserted with fixed primary keys so the seed is reproducible; that
    leaves the sequences at 1, and the first real insert would collide.
    """
    schema = dbmod.SHOP_SCHEMA
    for table in ("users", "products", "coupons", "carts", "cart_items", "orders", "order_items"):
        conn.execute(
            text(
                "SELECT setval("
                "  pg_get_serial_sequence(:qualified, 'id'),"
                "  COALESCE((SELECT MAX(id) FROM {schema}.{table}), 0) + 1,"
                "  false"
                ")".format(schema=f'"{schema}"', table=f'"{table}"')
            ),
            {"qualified": f"{schema}.{table}"},
        )


def seed_users(db) -> list[User]:
    # One bcrypt hash, reused. Every seeded user has the same password, and
    # hashing five times costs half a second for no benefit.
    password_hash = hash_password(SEED_PASSWORD)
    rows = [
        User(
            id=spec["id"],
            email=spec["email"],
            password_hash=password_hash,
            name=spec["name"],
            locale=spec["locale"],
            created_at=_utc(spec["created_at"]),
        )
        for spec in USERS
    ]
    db.add_all(rows)
    db.flush()
    return rows


def seed_products(db) -> list[Product]:
    rows = [
        Product(
            id=spec["id"],
            sku=spec["sku"],
            name=spec["name"],
            description=spec["description"],
            price_cents=spec["price_cents"],
            category=spec["category"],
            image_url=spec["image_url"],
            stock=spec["stock"],
        )
        for spec in PRODUCTS
    ]
    db.add_all(rows)
    db.flush()
    return rows


def seed_coupons(db) -> list[Coupon]:
    rows = [
        Coupon(
            id=spec["id"],
            code=spec["code"],
            kind=spec["kind"],
            value=spec["value"],
            max_uses=spec["max_uses"],
            uses=spec["uses"],
            min_subtotal_cents=spec["min_subtotal_cents"],
            expires_at=_utc(spec["expires_at"]) if spec["expires_at"] else None,
            active=spec["active"],
        )
        for spec in COUPONS
    ]
    db.add_all(rows)
    db.flush()
    return rows


def seed_orders(db) -> list[Order]:
    orders: list[Order] = []
    next_item_id = 1

    for spec in ORDERS:
        subtotal_cents = sum(
            _PRODUCT_BY_ID[pid]["price_cents"] * qty for pid, qty in spec["items"]
        )
        coupon = _COUPON_BY_CODE.get(spec["coupon_code"] or "")
        discount_cents = (
            discount_for(coupon["kind"], coupon["value"], subtotal_cents) if coupon else 0
        )
        taxable = max(0, subtotal_cents - discount_cents)
        tax_cents = tax_for(taxable)

        order = Order(
            id=spec["id"],
            user_id=spec["user_id"],
            status=spec["status"],
            coupon_code=spec["coupon_code"],
            subtotal_cents=subtotal_cents,
            discount_cents=discount_cents,
            tax_cents=tax_cents,
            total_cents=taxable + tax_cents,
            created_at=_utc(spec["created_at"]),
        )
        db.add(order)

        for product_id, qty in spec["items"]:
            product = _PRODUCT_BY_ID[product_id]
            db.add(
                OrderItem(
                    id=next_item_id,
                    order_id=order.id,
                    product_id=product_id,
                    name_snapshot=product["name"],
                    qty=qty,
                    unit_price_cents=product["price_cents"],
                )
            )
            next_item_id += 1

        orders.append(order)

    db.flush()
    return orders


# --------------------------------------------------------------------------- #
#  Checksum — proves `make reset` really returned to the same state
# --------------------------------------------------------------------------- #


def checksum(db) -> str:
    """SHA-256 over every seeded row, in a canonical order.

    ``password_hash`` is excluded: bcrypt salts each hash, so it differs run to
    run by design and would make the checksum useless.
    """
    payload: dict[str, Any] = {}

    payload["users"] = [
        [u.id, u.email, u.name, u.locale, u.created_at.isoformat()]
        for u in db.execute(select(User).order_by(User.id)).scalars()
    ]
    payload["products"] = [
        [p.id, p.sku, p.name, p.description, p.price_cents, p.category, p.image_url, p.stock]
        for p in db.execute(select(Product).order_by(Product.id)).scalars()
    ]
    payload["coupons"] = [
        [
            c.id,
            c.code,
            c.kind,
            c.value,
            c.max_uses,
            c.uses,
            c.min_subtotal_cents,
            c.expires_at.isoformat() if c.expires_at else None,
            bool(c.active),
        ]
        for c in db.execute(select(Coupon).order_by(Coupon.id)).scalars()
    ]
    payload["orders"] = [
        [
            o.id,
            o.user_id,
            o.status,
            o.coupon_code,
            o.subtotal_cents,
            o.discount_cents,
            o.tax_cents,
            o.total_cents,
            o.created_at.isoformat(),
        ]
        for o in db.execute(select(Order).order_by(Order.id)).scalars()
    ]
    payload["order_items"] = [
        [i.id, i.order_id, i.product_id, i.name_snapshot, i.qty, i.unit_price_cents]
        for i in db.execute(select(OrderItem).order_by(OrderItem.id)).scalars()
    ]

    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #


def seed() -> dict[str, Any]:
    """Wipe and rewrite the ``shop`` schema. Idempotent."""
    dbmod.create_all()

    # DDL/TRUNCATE fights idle pooled connections; hand them back first.
    dbmod.engine.dispose()

    with dbmod.engine.begin() as conn:
        wipe(conn)

    db = dbmod.SessionLocal()
    try:
        users = seed_users(db)
        products = seed_products(db)
        coupons = seed_coupons(db)
        orders = seed_orders(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    with dbmod.engine.begin() as conn:
        _resync_sequences(conn)

    # Materialise the full switchboard, every switch off.
    flag_values = flags.ensure_defaults(enabled=False)
    flags.reset_flags()

    db = dbmod.SessionLocal()
    try:
        digest = checksum(db)
    finally:
        db.close()

    return {
        "users": len(users),
        "products": len(products),
        "coupons": len(coupons),
        "orders": len(orders),
        "order_items": sum(len(o["items"]) for o in ORDERS),
        "flags": len(flag_values),
        "checksum": digest,
    }


def main() -> int:
    result = seed()
    print("seed: shop schema rewritten")
    print(f"  users        {result['users']}")
    print(f"  products     {result['products']}")
    print(f"  coupons      {result['coupons']}  (SAVE20 primed at 4/5)")
    print(f"  orders       {result['orders']}  ({result['order_items']} line items)")
    print(f"  flags        {result['flags']}  (all disabled)")
    print(f"  IDOR target  order #{IDOR_TARGET_ORDER_ID} (arjun's headphones)")
    print(f"  jacket order order #{JACKET_ORDER_ID} (priya's, seen in ticket #1045)")
    print(f"  checksum     {result['checksum']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
