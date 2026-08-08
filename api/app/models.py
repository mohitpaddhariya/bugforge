"""ShopForge domain model — schema ``shop``.

Money is **integer cents** everywhere; every monetary column ends in
``_cents``. Nothing in this module is ever a float.

The ``CHECK (uses <= max_uses)`` constraint on ``coupons`` is load-bearing for
BUG-001: it is what turns the read-modify-write race in checkout into a visible
``IntegrityError`` (HTTP 500) rather than a silent over-redemption.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# --------------------------------------------------------------------------- #
#  Enumerated string values (kept as plain text — easier to seed and to break)
# --------------------------------------------------------------------------- #

CART_STATUSES = ("open", "converted")
COUPON_KINDS = ("percent", "fixed")

ORDER_STATUS_PLACED = "placed"
ORDER_STATUS_SHIPPED = "shipped"
ORDER_STATUS_DELIVERED = "delivered"
ORDER_STATUS_CANCELLED = "cancelled"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
#  users
# --------------------------------------------------------------------------- #


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en-US", server_default="en-US")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    carts: Mapped[list["Cart"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    orders: Mapped[list["Order"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<User id={self.id} email={self.email!r}>"


# --------------------------------------------------------------------------- #
#  sessions  (opaque bearer token, delivered as the httpOnly cookie sf_session)
# --------------------------------------------------------------------------- #


class Session(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("shop.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped["User"] = relationship(back_populates="sessions")

    __table_args__ = (Index("ix_sessions_expires_at", "expires_at"),)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Session user_id={self.user_id} expires_at={self.expires_at}>"


# --------------------------------------------------------------------------- #
#  products
# --------------------------------------------------------------------------- #


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint("price_cents >= 0", name="price_non_negative"),
        CheckConstraint("stock >= 0", name="stock_non_negative"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Product id={self.id} sku={self.sku!r} price_cents={self.price_cents}>"


# --------------------------------------------------------------------------- #
#  carts / cart_items
# --------------------------------------------------------------------------- #


class Cart(Base):
    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("shop.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", server_default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="carts")
    items: Mapped[list["CartItem"]] = relationship(
        back_populates="cart",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CartItem.id",
    )

    __table_args__ = (
        CheckConstraint("status IN ('open', 'converted')", name="status_valid"),
        Index("ix_carts_user_id_status", "user_id", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Cart id={self.id} user_id={self.user_id} status={self.status!r}>"


class CartItem(Base):
    __tablename__ = "cart_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cart_id: Mapped[int] = mapped_column(
        ForeignKey("shop.carts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("shop.products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    cart: Mapped["Cart"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("unit_price_cents >= 0", name="unit_price_non_negative"),
        Index("ix_cart_items_cart_id_product_id", "cart_id", "product_id"),
    )

    @property
    def line_total_cents(self) -> int:
        return self.qty * self.unit_price_cents

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<CartItem id={self.id} cart_id={self.cart_id} qty={self.qty}>"


# --------------------------------------------------------------------------- #
#  coupons
# --------------------------------------------------------------------------- #


class Coupon(Base):
    """Discount code.

    ``kind='percent'`` -> ``value`` is a whole-number percentage (20 == 20%).
    ``kind='fixed'``   -> ``value`` is an amount in **cents**.
    """

    __tablename__ = "coupons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    min_subtotal_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    __table_args__ = (
        # ── load-bearing for BUG-001 ──────────────────────────────────────────
        # Turns the checkout read-modify-write race into a visible IntegrityError
        # instead of a silent over-redemption.
        CheckConstraint("uses <= max_uses", name="uses_within_max"),
        CheckConstraint("kind IN ('percent', 'fixed')", name="kind_valid"),
        CheckConstraint("value >= 0", name="value_non_negative"),
        CheckConstraint("uses >= 0", name="uses_non_negative"),
        CheckConstraint("max_uses >= 0", name="max_uses_non_negative"),
        CheckConstraint("min_subtotal_cents >= 0", name="min_subtotal_non_negative"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Coupon code={self.code!r} {self.kind}:{self.value} uses={self.uses}/{self.max_uses}>"


# --------------------------------------------------------------------------- #
#  orders / order_items
# --------------------------------------------------------------------------- #


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("shop.users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ORDER_STATUS_PLACED, server_default=ORDER_STATUS_PLACED
    )
    coupon_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subtotal_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    discount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tax_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OrderItem.id",
    )

    __table_args__ = (
        CheckConstraint("subtotal_cents >= 0", name="subtotal_non_negative"),
        CheckConstraint("discount_cents >= 0", name="discount_non_negative"),
        CheckConstraint("tax_cents >= 0", name="tax_non_negative"),
        CheckConstraint("total_cents >= 0", name="total_non_negative"),
        Index("ix_orders_user_id_created_at", "user_id", "created_at"),
        Index("ix_orders_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Order id={self.id} user_id={self.user_id} total_cents={self.total_cents}>"


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("shop.orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("shop.products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint("qty > 0", name="qty_positive"),
        CheckConstraint("unit_price_cents >= 0", name="unit_price_non_negative"),
    )

    @property
    def line_total_cents(self) -> int:
        return self.qty * self.unit_price_cents

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<OrderItem id={self.id} order_id={self.order_id} qty={self.qty}>"


# --------------------------------------------------------------------------- #
#  feature_flags  (the runtime bug switches — see app/flags.py)
# --------------------------------------------------------------------------- #


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<FeatureFlag {self.key}={self.enabled}>"


__all__ = [
    "CART_STATUSES",
    "COUPON_KINDS",
    "ORDER_STATUS_CANCELLED",
    "ORDER_STATUS_DELIVERED",
    "ORDER_STATUS_PLACED",
    "ORDER_STATUS_SHIPPED",
    "Cart",
    "CartItem",
    "Coupon",
    "FeatureFlag",
    "Order",
    "OrderItem",
    "Product",
    "Session",
    "User",
]
