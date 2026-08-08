"""BUG-001 — concurrent redemption of a limited-use coupon.

Regression test for ticket #1042. Must FAIL while ``BUG_COUPON_TOCTOU`` is on
and PASS once the coupon row is locked before the counter is incremented.

The collision is not guaranteed on any single attempt — two requests released
from a barrier still have to interleave inside the window between the guard
read and the write. So we open the window repeatedly and assert on the whole
set: a 500 anywhere means the invariant was enforced by the database constraint
instead of by the code.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

API = "http://localhost:8000"
COUPON = "SAVE20"
ATTEMPTS = 6
CONCURRENCY = 3


def _reprime():
    """Restore the seed: SAVE20 back to one redemption from its limit.

    ``/api/debug/reset`` also clears the flags, so capture and restore them —
    otherwise this test silently exercises the fixed path and always passes.
    """
    before = httpx.get(f"{API}/api/debug/flags", timeout=10).json()
    before = before.get("flags", before)
    httpx.post(f"{API}/api/debug/reset", timeout=300)
    for key, enabled in (before.items() if isinstance(before, dict) else []):
        if enabled:
            httpx.post(f"{API}/api/debug/flags",
                       json={"key": key, "enabled": True}, timeout=10)


def _ready_to_checkout(email: str) -> httpx.Client:
    c = httpx.Client(base_url=API, timeout=30)
    r = c.post("/api/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    products = c.get("/api/products").json()["products"]
    c.post("/api/cart/items", json={"product_id": products[0]["id"], "qty": 1})
    r = c.post("/api/cart/coupon", json={"code": COUPON})
    assert r.status_code == 200, r.text
    return c


def _collide() -> list[int]:
    """Fire N checkouts on the same coupon, released together."""
    clients = [_ready_to_checkout(e) for e in
               ("priya@example.com", "arjun@example.com", "mei@example.com")[:CONCURRENCY]]
    barrier = threading.Barrier(len(clients))

    body = {
        "coupon_code": COUPON,
        "address": {"name": "Test Persona", "line1": "14 Anna Salai",
                    "city": "Chennai", "postal_code": "600002", "country": "IN"},
    }

    def fire(c):
        barrier.wait(timeout=15)
        return c.post("/api/checkout", json=body).status_code

    try:
        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            return [f.result() for f in [pool.submit(fire, c) for c in clients]]
    finally:
        for c in clients:
            c.close()


def test_concurrent_checkout_never_500s():
    """A losing checkout is fine — a 500 is not.

    The coupon has a usage limit, so some requests must be rejected. Rejecting
    them with 400/409 is correct. Letting the CHECK constraint fire is the bug.
    """
    seen: list[list[int]] = []
    for _ in range(ATTEMPTS):
        _reprime()
        statuses = _collide()
        seen.append(statuses)
        if 500 in statuses:
            break

    flat = [s for row in seen for s in row]
    assert 500 not in flat, (
        f"concurrent checkout returned {seen}; a race on coupons.uses let the "
        "CHECK constraint fire instead of the application rejecting cleanly. "
        "Root cause: read-modify-write without SELECT ... FOR UPDATE."
    )
    assert {200, 201} & set(flat), f"every checkout was rejected: {seen}"
