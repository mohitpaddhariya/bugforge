"""The flow the store exists to support. If this breaks, nothing else matters."""


def test_catalog_lists_products(client):
    r = client.get("/api/products")
    assert r.status_code == 200
    assert len(r.json()["products"]) >= 6


def test_place_an_order(login):
    c = login("priya@example.com")
    products = c.get("/api/products").json()["products"]
    c.post("/api/cart/items", json={"product_id": products[0]["id"], "qty": 1})
    c.post("/api/cart/coupon", json={"code": "WELCOME10"})
    r = c.post("/api/checkout")
    assert r.status_code in (200, 201), r.text
    order = r.json().get("order", r.json())
    assert order["id"]
    assert c.get(f"/api/orders/{order['id']}").status_code == 200


def test_expired_coupon_is_rejected(login):
    """BUG-005 is not a bug: this is correct behaviour and must stay correct."""
    c = login("rahul@example.com")
    r = c.post("/api/cart/coupon", json={"code": "EXPIRED15"})
    assert r.status_code == 400
    assert "expired" in r.text.lower()
