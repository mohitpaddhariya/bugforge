"""Pydantic v2 request/response models and the shared error type.

Two conventions live here and are relied on by every router:

**Money is integer cents.** Every monetary field ends in ``_cents`` and is an
``int``. The only place that convention is violated is BUG-003
(``BUG_TOTAL_FIELD_RENAME``), which renames ``total_cents`` -> ``total`` on the
order response. That rename is applied in :func:`serialize_order`, not here.

**Errors are ``{"error": <code>, ...}``.** :class:`ApiError` raises an
``HTTPException`` whose ``detail`` is a dict; ``main.py`` installs a handler
that emits it verbatim. So ``POST /api/cart/coupon`` with an expired code
answers ``400 {"error": "coupon_expired", "message": "..."}``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
#  Errors
# --------------------------------------------------------------------------- #


class ApiError(HTTPException):
    """An HTTP error with a machine-readable code.

    ``raise ApiError(400, "coupon_expired", "This coupon has expired.")``
    serialises to ``{"error": "coupon_expired", "message": "...", "detail": "..."}``.
    """

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str | None = None,
        **extra: Any,
    ) -> None:
        # NOTE: the parameter is `error_code`, not `code`, so that callers can
        # attach a `code=` attribute (the coupon code) to the payload without
        # colliding with the positional argument.
        payload: dict[str, Any] = {
            "error": error_code,
            "message": message or error_code.replace("_", " "),
        }
        payload["detail"] = payload["message"]
        payload.update(extra)
        super().__init__(status_code=status_code, detail=payload)
        self.code = error_code


#: Reason code -> customer-facing sentence. Shared by cart + checkout so the
#: message the UI shows is identical no matter where the rejection happened.
COUPON_MESSAGES: dict[str, str] = {
    "coupon_not_found": "That coupon code isn't valid.",
    "coupon_inactive": "That coupon is no longer available.",
    "coupon_expired": "This coupon has expired.",
    "coupon_exhausted": "This coupon has reached its usage limit.",
    "coupon_min_subtotal": "Your subtotal doesn't meet this coupon's minimum.",
}


class CouponRejected(Exception):
    """Raised by coupon validation. Carries the reason code."""

    def __init__(self, reason: str, **attrs: Any) -> None:
        self.reason = reason
        self.attrs = attrs
        self.message = COUPON_MESSAGES.get(reason, "That coupon can't be used.")
        super().__init__(self.message)

    def as_api_error(self) -> ApiError:
        return ApiError(400, self.reason, self.message, **self.attrs)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #


def iso(dt: datetime | None) -> str | None:
    """ISO8601 with milliseconds, UTC — the project-wide timestamp format."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def as_utc(dt: datetime | None) -> datetime | None:
    """Coerce a possibly-naive datetime to an aware UTC one."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
#  Auth
# --------------------------------------------------------------------------- #


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Deliberately a plain str, not EmailStr: seeded logins must never fail
    # validation before they reach the password check, and `email-validator`
    # is not a dependency of this service.
    # Deliberately loose: a blank or malformed login must reach the password
    # check and come back as 401 invalid_credentials, not as a 422 the login
    # form has no way to render.
    email: str = Field(default="", max_length=255)
    password: str = Field(default="", max_length=200)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str
    locale: str


class UserEnvelope(BaseModel):
    user: UserOut


class OkResponse(BaseModel):
    ok: bool = True


# --------------------------------------------------------------------------- #
#  Catalog
# --------------------------------------------------------------------------- #


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    description: str
    price_cents: int
    category: str
    image_url: str | None = None
    stock: int


class ProductList(BaseModel):
    products: list[ProductOut]
    count: int
    categories: list[str]


# --------------------------------------------------------------------------- #
#  Cart
# --------------------------------------------------------------------------- #


class AddItemRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_id: int = Field(gt=0)
    qty: int = Field(default=1, ge=1, le=99)


class UpdateItemRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    #: ``0`` removes the line — the qty stepper in the UI can go to zero.
    qty: int = Field(ge=0, le=99)


class CouponRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str = Field(min_length=1, max_length=64)


class CouponOut(BaseModel):
    code: str
    kind: Literal["percent", "fixed"]
    value: int
    discount_cents: int
    uses: int
    max_uses: int
    expires_at: str | None = None


class CartItemOut(BaseModel):
    id: int
    product_id: int
    sku: str
    name: str
    image_url: str | None = None
    qty: int
    unit_price_cents: int
    line_total_cents: int


class CartOut(BaseModel):
    id: int
    status: str
    items: list[CartItemOut]
    item_count: int
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    total_cents: int
    coupon: CouponOut | None = None
    coupon_error: str | None = None


# --------------------------------------------------------------------------- #
#  Checkout / orders
# --------------------------------------------------------------------------- #


class CheckoutRequest(BaseModel):
    """Everything here is optional — checkout works on an empty body.

    The address block is accepted (the UI prefills and posts it) but not
    persisted: shipping is explicitly out of scope in the spec.
    """

    model_config = ConfigDict(extra="ignore")

    coupon_code: str | None = None
    address: dict[str, Any] | None = None
    email: str | None = None


class OrderItemOut(BaseModel):
    id: int
    product_id: int
    name_snapshot: str
    qty: int
    unit_price_cents: int
    line_total_cents: int


class OrderOut(BaseModel):
    """Documentation shape only.

    Orders are serialised by hand in :func:`serialize_order` because
    ``BUG_TOTAL_FIELD_RENAME`` changes a key name at runtime, which a static
    ``response_model`` cannot express.
    """

    id: int
    user_id: int
    status: str
    coupon_code: str | None = None
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    total_cents: int
    created_at: str | None = None
    item_count: int
    items: list[OrderItemOut]


# --------------------------------------------------------------------------- #
#  Debug control plane
# --------------------------------------------------------------------------- #


class FlagUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str = Field(min_length=1, max_length=64)
    enabled: bool


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    #: Drop and recreate every ``shop`` table before reseeding.
    drop: bool = True
    #: Run ``scripts/seed.py`` after recreating the schema.
    seed: bool = True
    #: Also empty ``telemetry.events`` (the table itself is left in place —
    #: the collector owns it and must keep serving).
    telemetry: bool = False
    #: Flag values to apply after the reset. ``None`` turns every bug off.
    flags: dict[str, bool] | None = None


__all__ = [
    "COUPON_MESSAGES",
    "AddItemRequest",
    "ApiError",
    "CartItemOut",
    "CartOut",
    "CheckoutRequest",
    "CouponOut",
    "CouponRejected",
    "CouponRequest",
    "FlagUpdate",
    "LoginRequest",
    "OkResponse",
    "OrderItemOut",
    "OrderOut",
    "ProductList",
    "ProductOut",
    "ResetRequest",
    "UpdateItemRequest",
    "UserEnvelope",
    "UserOut",
    "as_utc",
    "iso",
]
