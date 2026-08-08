"""Order history and order detail.

Two planted bugs live in this file:

* **BUG-003 / ``BUG_TOTAL_FIELD_RENAME``** — :func:`serialize_order` emits the
  total under the key ``total`` instead of ``total_cents``. The web app still
  reads ``total_cents``, gets ``undefined``, and renders ``$NaN``. Orders are
  serialised at the top level of the response precisely so response-shape
  telemetry (spec §6.4) makes the drift visible without reading any code.

* **BUG-004 / ``BUG_ORDER_IDOR``** — :func:`read_order` looks an order up by
  primary key with no owner check, so any signed-in customer can read anyone
  else's order.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app import flags
from app.auth import get_current_user
from app.db import get_db
from app.models import Order, User
from app.schemas import ApiError, iso
from app.telemetry import emit

router = APIRouter(tags=["orders"])


# --------------------------------------------------------------------------- #
#  Serialisation  —  BUG-003 lives in the one branch below
# --------------------------------------------------------------------------- #


def serialize_order(order: Order) -> dict:
    """Flat order payload, shared by ``/api/checkout`` and ``/api/orders*``.

    The response is deliberately *not* wrapped in an envelope: the order's own
    fields are the top-level keys of the JSON body, which is what makes the
    field rename below detectable from response-shape telemetry alone.
    """
    items = [
        {
            "id": item.id,
            "product_id": item.product_id,
            "name_snapshot": item.name_snapshot,
            "qty": item.qty,
            "unit_price_cents": item.unit_price_cents,
            "line_total_cents": item.qty * item.unit_price_cents,
        }
        for item in order.items
    ]

    payload: dict = {
        "id": order.id,
        "user_id": order.user_id,
        "status": order.status,
        "coupon_code": order.coupon_code,
        "subtotal_cents": order.subtotal_cents,
        "discount_cents": order.discount_cents,
        "tax_cents": order.tax_cents,
        "created_at": iso(order.created_at),
        "item_count": sum(item["qty"] for item in items),
        "items": items,
    }

    if flags.is_enabled(flags.BUG_TOTAL_FIELD_RENAME):
        # ── BUG-003 (BUG_TOTAL_FIELD_RENAME) ──────────────────────────────
        # The total ships under `total`. web/ reads `total_cents`, gets
        # undefined, and formats it as "$NaN" on the confirmation and history
        # pages. Neither side is wrong on its own.
        payload["total"] = order.total_cents
    else:
        payload["total_cents"] = order.total_cents

    return payload


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #


@router.get("/orders")
def list_orders(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """The signed-in customer's own orders, newest first.

    The list is always scoped to ``current_user``; BUG-004 only affects the
    detail route, which is what makes the leak look like a rendering glitch
    rather than an obviously broken list.
    """
    orders = (
        db.execute(
            select(Order)
            .where(Order.user_id == user.id)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return {
        "orders": [serialize_order(order) for order in orders],
        "count": len(orders),
    }


@router.get("/orders/{order_id}")
def read_order(
    order_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Order detail.

    BUG-004 (``BUG_ORDER_IDOR``): with the flag on, the lookup is by primary
    key only. Ticket #1044 reads "clicked my order and it showed a jacket I
    never bought" — an authorization failure reported as a display glitch.
    """
    if flags.is_enabled(flags.BUG_ORDER_IDOR):
        # ── BUG-004 (BUG_ORDER_IDOR) ──────────────────────────────────────
        # Primary key only. No `AND user_id == current_user.id`, so any
        # authenticated user can read any order by guessing its id.
        order = db.get(Order, order_id)
    else:
        order = db.execute(
            select(Order).where(Order.id == order_id, Order.user_id == user.id)
        ).scalar_one_or_none()

    if order is None:
        raise ApiError(404, "order_not_found", "We couldn't find that order.")

    if order.user_id != user.id:
        # Reached only with the flag on. Recorded so the leak is visible in
        # telemetry after the fact, without changing what the customer sees.
        emit(
            "business",
            "order_viewed_cross_user",
            level="warn",
            order_id=order.id,
            owner_user_id=order.user_id,
            viewer_user_id=user.id,
        )

    return serialize_order(order)


__all__ = ["router", "serialize_order"]
