"""Cart: line items, quantities and coupon application.

Also the home of the money maths (:func:`price_cart`) and of coupon validation
(:func:`validate_coupon`), both of which ``checkout.py`` imports so that the
total the customer sees in the cart is computed by exactly the same code that
computes the total on the order.

**Coupon apply is deliberately correct.** ``POST /api/cart/coupon`` with
``EXPIRED15`` answers ``400 {"error": "coupon_expired"}`` and the UI shows
"This coupon has expired". That is BUG-005 — the ticket that is *not* a bug.
Nothing in this file branches on a bug flag.

The applied coupon is held in a process-local map rather than on ``shop.carts``
because the schema has no column for it (see spec §4). It is intentionally
ephemeral: the code only needs to survive between "apply" and "checkout".
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.auth import get_current_user
from app.db import get_db
from app.models import Cart, CartItem, Coupon, Product, User
from app.schemas import (
    AddItemRequest,
    ApiError,
    CartOut,
    CouponRejected,
    CouponRequest,
    UpdateItemRequest,
    as_utc,
    iso,
)
from app.telemetry import emit

router = APIRouter(tags=["cart"])

# --------------------------------------------------------------------------- #
#  Money. Integer cents, always.
# --------------------------------------------------------------------------- #

#: Sales tax in basis points (800 = 8.00%), applied to the discounted subtotal.
#: Integer arithmetic only — floats are a stretch bug (BUG-006), not this one.
TAX_BPS = 800


def discount_for(coupon: Coupon | None, subtotal_cents: int) -> int:
    """Discount in cents. Never negative, never larger than the subtotal."""
    if coupon is None or subtotal_cents <= 0:
        return 0
    if coupon.kind == "percent":
        raw = (subtotal_cents * int(coupon.value)) // 100
    else:  # fixed — value is already cents
        raw = int(coupon.value)
    return max(0, min(raw, subtotal_cents))


def tax_for(taxable_cents: int) -> int:
    return max(0, (taxable_cents * TAX_BPS) // 10_000)


@dataclass(frozen=True)
class Totals:
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    total_cents: int


def compute_totals(subtotal_cents: int, coupon: Coupon | None) -> Totals:
    discount_cents = discount_for(coupon, subtotal_cents)
    taxable = max(0, subtotal_cents - discount_cents)
    tax_cents = tax_for(taxable)
    return Totals(
        subtotal_cents=subtotal_cents,
        discount_cents=discount_cents,
        tax_cents=tax_cents,
        total_cents=taxable + tax_cents,
    )


# --------------------------------------------------------------------------- #
#  Applied-coupon store (process-local; see module docstring)
# --------------------------------------------------------------------------- #

_applied_lock = threading.RLock()
_applied_codes: dict[int, str] = {}


def get_applied_code(cart_id: int) -> str | None:
    with _applied_lock:
        return _applied_codes.get(cart_id)


def set_applied_code(cart_id: int, code: str) -> None:
    with _applied_lock:
        _applied_codes[cart_id] = code


def clear_applied_code(cart_id: int) -> None:
    with _applied_lock:
        _applied_codes.pop(cart_id, None)


def clear_all_applied_codes() -> None:
    """Called by ``POST /api/debug/reset`` so a reset is really a reset."""
    with _applied_lock:
        _applied_codes.clear()


# --------------------------------------------------------------------------- #
#  Cart access
# --------------------------------------------------------------------------- #


def get_open_cart(db: DbSession, user: User) -> Cart | None:
    return db.execute(
        select(Cart)
        .where(Cart.user_id == user.id, Cart.status == "open")
        .order_by(Cart.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def get_or_create_open_cart(db: DbSession, user: User) -> Cart:
    cart = get_open_cart(db, user)
    if cart is None:
        cart = Cart(user_id=user.id, status="open", created_at=datetime.now(timezone.utc))
        db.add(cart)
        db.flush()
    return cart


def cart_subtotal_cents(cart: Cart) -> int:
    return sum(item.qty * item.unit_price_cents for item in cart.items)


# --------------------------------------------------------------------------- #
#  Coupon validation — the BUG-005 path. Correct on purpose.
# --------------------------------------------------------------------------- #


def validate_coupon(
    db: DbSession,
    code: str,
    subtotal_cents: int,
    *,
    now: datetime | None = None,
) -> Coupon:
    """Return the usable coupon, or raise :class:`CouponRejected`.

    Order of checks matters for the tickets: expiry is reported before
    exhaustion so ``EXPIRED15`` always reads back ``coupon_expired``.

    NOTE: this is a plain ``SELECT`` with no row lock. That is fine here (the
    cart is not redeeming anything) but it is also the *time of check* half of
    BUG-001 when ``checkout.py`` calls it — see the redemption block there.
    """
    now = now or datetime.now(timezone.utc)
    normalized = code.strip().upper()

    coupon = db.execute(
        select(Coupon).where(Coupon.code == normalized)
    ).scalar_one_or_none()

    if coupon is None:
        raise CouponRejected("coupon_not_found", code=normalized)
    if not coupon.active:
        raise CouponRejected("coupon_inactive", code=normalized)

    expires_at = as_utc(coupon.expires_at)
    if expires_at is not None and expires_at <= now:
        raise CouponRejected("coupon_expired", code=normalized, expires_at=iso(expires_at))

    # Time of check for BUG-001: uses is read here, unlocked.
    if coupon.uses >= coupon.max_uses:
        raise CouponRejected(
            "coupon_exhausted", code=normalized, uses=coupon.uses, max_uses=coupon.max_uses
        )

    if subtotal_cents < coupon.min_subtotal_cents:
        raise CouponRejected(
            "coupon_min_subtotal",
            code=normalized,
            min_subtotal_cents=coupon.min_subtotal_cents,
            subtotal_cents=subtotal_cents,
        )

    return coupon


def serialize_coupon(coupon: Coupon, discount_cents: int) -> dict:
    return {
        "code": coupon.code,
        "kind": coupon.kind,
        "value": coupon.value,
        "discount_cents": discount_cents,
        "uses": coupon.uses,
        "max_uses": coupon.max_uses,
        "expires_at": iso(coupon.expires_at),
    }


# --------------------------------------------------------------------------- #
#  Serialisation
# --------------------------------------------------------------------------- #


def serialize_cart(db: DbSession, cart: Cart) -> dict:
    """Flat cart payload. No envelope — the top-level keys are the cart's."""
    items = []
    for item in cart.items:
        product = item.product
        items.append(
            {
                "id": item.id,
                "product_id": item.product_id,
                "sku": product.sku if product else "",
                "name": product.name if product else "",
                "image_url": product.image_url if product else None,
                "qty": item.qty,
                "unit_price_cents": item.unit_price_cents,
                "line_total_cents": item.qty * item.unit_price_cents,
            }
        )

    subtotal_cents = sum(i["line_total_cents"] for i in items)

    coupon: Coupon | None = None
    coupon_error: str | None = None
    code = get_applied_code(cart.id)
    if code:
        try:
            coupon = validate_coupon(db, code, subtotal_cents)
        except CouponRejected as rejected:
            # The code stays attached so the UI can explain itself, but it
            # contributes no discount.
            coupon_error = rejected.reason

    totals = compute_totals(subtotal_cents, coupon)

    return {
        "id": cart.id,
        "status": cart.status,
        "items": items,
        "item_count": sum(i["qty"] for i in items),
        "subtotal_cents": totals.subtotal_cents,
        "discount_cents": totals.discount_cents,
        "tax_cents": totals.tax_cents,
        "total_cents": totals.total_cents,
        "coupon": serialize_coupon(coupon, totals.discount_cents) if coupon else None,
        "coupon_error": coupon_error,
    }


# --------------------------------------------------------------------------- #
#  Routes
# --------------------------------------------------------------------------- #


@router.get("/cart", response_model=CartOut)
def read_cart(
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    cart = get_or_create_open_cart(db, user)
    return serialize_cart(db, cart)


@router.post("/cart/items", response_model=CartOut, status_code=201)
def add_item(
    payload: AddItemRequest,
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    product = db.get(Product, payload.product_id)
    if product is None:
        raise ApiError(404, "product_not_found", "That product doesn't exist.")

    cart = get_or_create_open_cart(db, user)

    existing = db.execute(
        select(CartItem).where(
            CartItem.cart_id == cart.id, CartItem.product_id == product.id
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.qty = min(99, existing.qty + payload.qty)
    else:
        db.add(
            CartItem(
                cart_id=cart.id,
                product_id=product.id,
                qty=payload.qty,
                # Price is snapshotted at add time; the catalog can move later.
                unit_price_cents=product.price_cents,
            )
        )

    db.flush()
    db.refresh(cart)

    emit(
        "business",
        "cart_item_added",
        product_id=product.id,
        sku=product.sku,
        qty=payload.qty,
        unit_price_cents=product.price_cents,
        cart_id=cart.id,
    )
    return serialize_cart(db, cart)


@router.patch("/cart/items/{item_id}", response_model=CartOut)
def update_item(
    item_id: int,
    payload: UpdateItemRequest,
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    cart = get_or_create_open_cart(db, user)
    item = db.execute(
        select(CartItem).where(CartItem.id == item_id, CartItem.cart_id == cart.id)
    ).scalar_one_or_none()
    if item is None:
        raise ApiError(404, "cart_item_not_found", "That item isn't in your cart.")

    if payload.qty == 0:
        db.delete(item)
    else:
        item.qty = payload.qty

    db.flush()
    db.refresh(cart)
    return serialize_cart(db, cart)


@router.delete("/cart/items/{item_id}", response_model=CartOut)
def remove_item(
    item_id: int,
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    cart = get_or_create_open_cart(db, user)
    item = db.execute(
        select(CartItem).where(CartItem.id == item_id, CartItem.cart_id == cart.id)
    ).scalar_one_or_none()
    if item is None:
        raise ApiError(404, "cart_item_not_found", "That item isn't in your cart.")

    db.delete(item)
    db.flush()
    db.refresh(cart)
    return serialize_cart(db, cart)


@router.post("/cart/coupon", response_model=CartOut)
def apply_coupon(
    payload: CouponRequest,
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Apply a coupon to the open cart.

    BUG-005 lives here and is **correct behaviour**: an expired code is
    rejected with ``400 {"error": "coupon_expired"}`` and a ``coupon_rejected``
    business event, which is exactly what the customer saw. There is no flag
    to flip.
    """
    cart = get_or_create_open_cart(db, user)
    subtotal_cents = cart_subtotal_cents(cart)
    code = payload.code.strip().upper()

    try:
        coupon = validate_coupon(db, code, subtotal_cents)
    except CouponRejected as rejected:
        clear_applied_code(cart.id)
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
                "stage": "cart",
                **rejected.attrs,
            },
        )
        raise rejected.as_api_error() from rejected

    set_applied_code(cart.id, coupon.code)
    discount_cents = discount_for(coupon, subtotal_cents)

    emit(
        "business",
        "coupon_applied",
        code=coupon.code,
        coupon_kind=coupon.kind,
        value=coupon.value,
        uses=coupon.uses,
        max_uses=coupon.max_uses,
        discount_cents=discount_cents,
        subtotal_cents=subtotal_cents,
        cart_id=cart.id,
        stage="cart",
    )
    return serialize_cart(db, cart)


@router.delete("/cart/coupon", response_model=CartOut)
def remove_coupon(
    db: DbSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    cart = get_or_create_open_cart(db, user)
    code = get_applied_code(cart.id)
    clear_applied_code(cart.id)
    if code:
        emit("business", "coupon_removed", code=code, cart_id=cart.id)
    return serialize_cart(db, cart)


__all__ = [
    "TAX_BPS",
    "Totals",
    "cart_subtotal_cents",
    "clear_all_applied_codes",
    "clear_applied_code",
    "compute_totals",
    "discount_for",
    "get_applied_code",
    "get_open_cart",
    "get_or_create_open_cart",
    "router",
    "serialize_cart",
    "serialize_coupon",
    "set_applied_code",
    "tax_for",
    "validate_coupon",
]
