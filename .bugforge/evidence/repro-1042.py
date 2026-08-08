"""Reproduction for ticket #1042 — "order wont go thru?? charged twice??"

Goal: place an order with SAVE20 applied while a second checkout is in flight,
and show what the customer saw — the Place Order button spinning forever.

Run it:
    bf repro run .bugforge/runs/1042/repro.py --label before
    bf repro run .bugforge/runs/1042/repro.py --label after

Symptom checks below must be TRUE while the bug exists and FALSE once it is
fixed. Do not edit this script to make the "after" run pass — that voids the
verification.
"""
from bugforge_agent.reprolib import check

VIEWPORT = (1440, 900)
PERSONA = "priya@example.com"

# The coupon has to be one redemption from its limit for the window to open at
# all, and the losing writer only collides some of the time — so we open the
# window repeatedly rather than once.
COLLISION_ATTEMPTS = 2

ADDRESS = {
    "name": "Priya Nair",
    "line1": "14 Anna Salai",
    "city": "Chennai",
    "postal_code": "600002",
    "country": "IN",
}


async def _stuck_spinner(r) -> bool:
    """The customer's actual complaint: grey button, spinner, forever."""
    btn = r.page.locator('[data-testid="place-order"]')
    if not await btn.count():
        return False
    disabled = await btn.first.is_disabled()
    text = (await btn.first.inner_text()).strip().lower()
    return disabled and "placing order" in text


async def _no_error_shown(r) -> bool:
    """A 500 with nothing on screen is why she thought it was still working."""
    return not await r.visible("checkout-error")


SYMPTOM_CHECKS = [
    check("checkout_returns_500", lambda r: r.any_status(500)),
    check("button_stuck_spinning", _stuck_spinner),
    check("no_error_shown", _no_error_shown),
]


async def _restage(ctx):
    """Fresh cart and a freshly primed SAVE20, through the real API.

    The reset drops and reseeds the shop schema, which takes the sessions table
    with it — so the browser's cookie is dead afterwards and we have to log in
    again before anything else will authenticate.
    """
    await ctx.api("POST", "/api/debug/reset")
    for key in ("BUG_COUPON_TOCTOU", "BUG_CHECKOUT_SWALLOWS_ERROR"):
        await ctx.api("POST", "/api/debug/flags",
                      json_body={"key": key, "enabled": True})
    await ctx.api("POST", "/api/auth/login",
                  json_body={"email": PERSONA, "password": "password123"})
    await ctx.api("POST", "/api/cart/items", json_body={"product_id": 3, "qty": 1})
    await ctx.api("POST", "/api/cart/coupon", json_body={"code": "SAVE20"})


async def run(ctx):
    await ctx.login()

    # The customer's path, through the UI, so the recording shows what she saw.
    await ctx.goto("/")
    await ctx.click("product-card-3")
    await ctx.click("add-to-cart")
    await ctx.goto("/cart")
    await ctx.wait(300)

    body = {"coupon_code": "SAVE20", "address": ADDRESS}

    for attempt in range(COLLISION_ATTEMPTS):
        await _restage(ctx)
        await ctx.goto("/checkout")
        await ctx.wait(300)

        # She had the tab open twice. One checkout goes through the button so
        # the UI state is real; the others race it from the same session.
        await ctx.parallel(
            ctx.click("place-order"),
            ctx.api("POST", "/api/checkout", json_body=body),
            ctx.api("POST", "/api/checkout", json_body=body),
        )
        await ctx.wait(1200)

        if any(r["status"] == 500 for r in ctx.rec.requests):
            break

    # ---------------------------------------------------------------- #
    #  Finish on the frame that matters.
    # ---------------------------------------------------------------- #
    #
    # The race above proves the 500, but it cannot be filmed: her request wins
    # it almost every time, so the camera ends up pointed at a success. The
    # customer-visible half of the same defect is deterministic, so film that
    # instead — a failed checkout that the page never surfaces.
    #
    # Exhaust the coupon first, so her checkout is guaranteed to be rejected.
    # While the bug is live that rejection is awaited without a catch and the
    # button stays in its loading state forever. Once fixed, the same rejection
    # is caught and shown, and she can go on to place the order.
    await _restage(ctx)
    rival = await ctx.rival("arjun@example.com")
    # The rival needs a cart of its own — an empty one checks out as cart_empty
    # and never touches the coupon, which is what quietly made this a no-op.
    await rival("POST", "/api/cart/items", {"product_id": 5, "qty": 1})
    await rival("POST", "/api/cart/coupon", {"code": "SAVE20"})
    burned = await rival("POST", "/api/checkout", body)   # burns the last use
    assert burned["status"] in (200, 201), (
        f"could not exhaust SAVE20 before the demo click: {burned['status']}")

    await ctx.goto("/checkout")
    await ctx.wait(1000)
    await ctx.click("place-order")
    await ctx.wait(3500)

    if await _stuck_spinner(ctx.rec):
        # Wedged. Let it sit, the way she waited two minutes.
        await ctx.wait(6000)
        return

    # Fixed: the rejection was surfaced. Show that ordering still works.
    await _restage(ctx)
    await ctx.goto("/checkout")
    await ctx.wait(1000)
    await ctx.click("place-order")
    await ctx.wait(5000)
