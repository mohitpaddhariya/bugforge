"""Checkout: turn the open cart into an order and redeem the coupon.

This is where BUG-001 lives.

The coupon guard (``uses < max_uses``) is evaluated in :func:`validate_coupon`
at the top of the request — the **time of check**. The counter is incremented
at the bottom, after the order rows have been written — the **time of use**.
With ``BUG_COUPON_TOCTOU`` on, nothing holds a lock across that gap, so two
simultaneous checkouts both pass a guard that says "4 of 5 used" and both go on
to increment. Postgres serialises the two UPDATEs on the row lock, the loser
re-evaluates ``uses + 1`` against the winner's committed 5, writes 6, and trips
``CHECK (uses <= max_uses)`` -> ``IntegrityError`` -> HTTP 500.

With the flag off, the coupon row is re-read ``FOR UPDATE`` before the guard is
re-checked, so the loser gets a clean ``400 coupon_exhausted`` instead.

BUG-003 (``BUG_TOTAL_FIELD_RENAME``) also shows up in this file's response,
because the confirmation page reads the same payload shape as order detail —
see :func:`app.routers.orders.serialize_order`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app import flags
from app.auth import get_current_user
from app.db import get_db
from app.models import ORDER_STATUS_PLACED, Coupon, Order, OrderItem, User
from app.routers.cart import (
    cart_subtotal_cents,
    clear_applied_code,
    compute_totals,
    get_applied_code,
    get_or_create_open_cart,
    validate_coupon,
)
from app.routers.orders import serialize_order
from app.schemas import ApiError, CheckoutRequest, CouponRejected
from app.telemetry import emit

router = APIRouter(tags=["checkout"])


def _fail(reason: str, message: str, status: int = 400, **attrs) -> ApiError:
    """Emit ``checkout_failed`` and build the matching HTTP error."""
    emit("business", "checkout_failed", level="warn", **{"reason": reason, **attrs})
    return ApiError(status, reason, message, **attrs)


def _redeem_coupon(db: DbSession, coupon: Coupon) -> None:
    """Increment ``coupons.uses`` under a row lock.

    The guard that decided this coupon was redeemable ran at the top of the
    request (``validate_coupon``) — that is the time of check. This is the time
    of use. Everything hinges on whether anything held a lock in between, so
    this function takes one.

    Fixes #1042. Previously this had an unlocked read-modify-write path: two
    concurrent checkouts both passed a guard that read ``uses=4`` of 5 and both
    arrived here, so the loser wrote 6 and violated
    ``CHECK (uses <= max_uses)`` — an IntegrityError surfacing as HTTP 500. The
    invariant is now enforced by the application rather than by the constraint,
    and a losing checkout gets a clean rejection instead of a server error.
    """
    # Take the row lock first, then re-check the guard against the value
    # nobody else can be holding.
    #
    # ``populate_existing`` is load-bearing, not style. This Session already has
    # the Coupon in its identity map from the time-of-check read, and by default
    # SQLAlchemy returns that cached instance and DISCARDS the columns the
    # locking SELECT just fetched. The lock would be taken and the stale
    # ``uses`` used anyway, so two racing checkouts would both compute 4 + 1 = 5,
    # both write 5, and the coupon would be redeemed twice while the counter
    # moved once — an over-redemption the CHECK constraint can never catch.
    locked = (
        db.execute(
            select(Coupon)
            .where(Coupon.id == coupon.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalar_one()
    )
    if locked.uses >= locked.max_uses:
        db.rollback()
        emit(
            "business",
            "coupon_rejected",
            level="warn",
            code=locked.code,
            reason="coupon_exhausted",
            uses=locked.uses,
            max_uses=locked.max_uses,
            stage="checkout_locked",
        )
        raise _fail(
            "coupon_exhausted",
            "This coupon has reached its usage limit.",
            code=locked.code,
            uses=locked.uses,
            max_uses=locked.max_uses,
        )
    locked.uses = locked.uses + 1
    db.flush()


@router.post("/checkout", status_code=201)
def checkout(
    payload: CheckoutRequest | None = None,
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    payload = payload or CheckoutRequest()
    cart = get_or_create_open_cart(db, user)

    if not cart.items:
        raise _fail("cart_empty", "Your cart is empty.", cart_id=cart.id)

    subtotal_cents = cart_subtotal_cents(cart)

    # ---------------------------------------------------------------- #
    #  Coupon — TIME OF CHECK. Plain SELECT, no row lock.
    # ---------------------------------------------------------------- #
    code = (payload.coupon_code or get_applied_code(cart.id) or "").strip().upper()
    coupon: Coupon | None = None
    if code:
        try:
            coupon = validate_coupon(db, code, subtotal_cents)
        except CouponRejected as rejected:
            emit(
                "business",
                "coupon_rejected",
                level="warn",
                **{
                    "code": code,
                    "reason": rejected.reason,
                    "message": rejected.message,
                    "subtotal_cents": subtotal_cents,
                    "cart_id": cart.id,
                    "stage": "checkout",
                    **rejected.attrs,
                },
            )
            raise _fail(
                rejected.reason,
                rejected.message,
                cart_id=cart.id,
                code=code,
                stage="checkout",
            ) from rejected

    totals = compute_totals(subtotal_cents, coupon)

    # ---------------------------------------------------------------- #
    #  Write the order
    # ---------------------------------------------------------------- #
    order = Order(
        user_id=user.id,
        status=ORDER_STATUS_PLACED,
        coupon_code=coupon.code if coupon else None,
        subtotal_cents=totals.subtotal_cents,
        discount_cents=totals.discount_cents,
        tax_cents=totals.tax_cents,
        total_cents=totals.total_cents,
        created_at=datetime.now(timezone.utc),
    )
    db.add(order)
    db.flush()

    for item in cart.items:
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=item.product_id,
                name_snapshot=item.product.name if item.product else "",
                qty=item.qty,
                unit_price_cents=item.unit_price_cents,
            )
        )
    db.flush()

    # ---------------------------------------------------------------- #
    #  Coupon — TIME OF USE. BUG-001 lives in the branch below.
    # ---------------------------------------------------------------- #
    if coupon is not None:
        emit(
            "business",
            "coupon_applied",
            code=coupon.code,
            coupon_kind=coupon.kind,
            value=coupon.value,
            uses=coupon.uses,
            max_uses=coupon.max_uses,
            discount_cents=totals.discount_cents,
            subtotal_cents=totals.subtotal_cents,
            order_id=order.id,
            stage="checkout",
        )

    try:
        if coupon is not None:
            _redeem_coupon(db, coupon)
        cart.status = "converted"
        db.flush()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        emit(
            "business",
            "checkout_failed",
            level="error",
            reason="coupon_over_redeemed",
            code=coupon.code if coupon else None,
            max_uses=coupon.max_uses if coupon else None,
            constraint="ck_coupons_uses_within_max",
            exception=type(exc).__name__,
        )
        # Deliberately re-raised: the traceback (checkout.py, this line) is
        # what the robot reads out of the timeline, and the browser needs to
        # see the 500 that the customer described as "it just spins".
        raise

    clear_applied_code(cart.id)

    body = serialize_order(order)
    emit(
        "business",
        "order_created",
        order_id=order.id,
        user_id=user.id,
        coupon_code=order.coupon_code,
        item_count=len(order.items),
        subtotal_cents=order.subtotal_cents,
        discount_cents=order.discount_cents,
        tax_cents=order.tax_cents,
        total_cents=order.total_cents,
        response_keys=sorted(body.keys()),
    )
    return body


__all__ = ["router"]
