"""Product catalog. Public — no session required.

Twelve seeded products across three categories. No search, no pagination games
(that is stretch bug BUG-009), no bug flags: the catalog exists so the rest of
the flow has something to sell.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.db import get_db
from app.models import Product
from app.schemas import ApiError, ProductList, ProductOut

router = APIRouter(tags=["catalog"])


@router.get("/products", response_model=ProductList)
def list_products(
    category: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: DbSession = Depends(get_db),
) -> dict:
    stmt = select(Product).order_by(Product.id)
    if category:
        stmt = stmt.where(Product.category == category)

    products = db.execute(stmt.limit(limit).offset(offset)).scalars().all()
    categories = (
        db.execute(select(Product.category).distinct().order_by(Product.category))
        .scalars()
        .all()
    )

    return {
        "products": [ProductOut.model_validate(p).model_dump() for p in products],
        "count": len(products),
        "categories": list(categories),
    }


@router.get("/products/{product_id}", response_model=ProductOut)
def read_product(
    product_id: int,
    db: DbSession = Depends(get_db),
) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise ApiError(404, "product_not_found", "That product doesn't exist.")
    return ProductOut.model_validate(product).model_dump()


__all__ = ["router"]
