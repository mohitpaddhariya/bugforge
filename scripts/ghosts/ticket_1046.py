#!/usr/bin/env python3
"""Ghost run for ticket #1046 — BUG-005, the ticket that is not a bug.

    "your discount codes dont work. i typed EXPIRED15 ... tried it like 5 times,
     also tried it in caps and small letters and with a space after ... there
     was some red text but i didnt read it properly"   — rahul

``EXPIRED15`` genuinely expired on 2026-06-30. ``POST /api/cart/coupon`` answers
``400 {"error": "coupon_expired"}``, emits a ``coupon_rejected`` business event,
and the cart UI renders "This coupon has expired." Everything in this session is
the system working exactly as designed. **No flags are enabled.**

This is the most important ghost of the five, and the only one whose success
condition is that nothing is broken. It exists so the robot can be handed a
furious ticket, investigate a real session, find the rejection *and the message
the customer was shown*, and close the ticket with no patch. An agent that
always produces a patch is worse than useless — so this run asserts the correct
behaviour just as strictly as the others assert their breakage: three clean
400s, three ``coupon_expired`` reasons, three rendered messages, zero errors
anywhere, and a cart total that never moved.
"""

from __future__ import annotations

from run_all import (
    WINDOWS_CHROME_UA,
    Check,
    Ghost,
    GhostFailure,
    GhostResult,
    apply_flags,
    attrs_of,
    coupon_state,
    human_pause,
    requests_to,
    select_events,
    summarise,
    wait_for_events,
)

TICKET = 1046
TITLE = "EXPIRED15 will not apply (correct behaviour — coupon is expired)"
PERSONA = "rahul@example.com"

#: Deliberately empty. Nothing about this ticket is a bug.
FLAGS: dict[str, bool] = {}

#: The three ways he typed it, straight out of the ticket.
ATTEMPTS = ("EXPIRED15", "expired15", "EXPIRED15 ")

EXPIRY_MESSAGE = "This coupon has expired."


def run() -> GhostResult:
    apply_flags(FLAGS)  # no-op; the ticket has no switch

    coupon = coupon_state("EXPIRED15")
    if not coupon:
        raise GhostFailure("seed is missing EXPIRED15")

    ghost = Ghost(
        ticket=TICKET,
        title=TITLE,
        email=PERSONA,
        viewport=(1366, 768),
        user_agent=WINDOWS_CHROME_UA,
        locale="en-IN",
    )

    statuses: list[int] = []
    codes: list[str] = []

    with ghost:
        ghost.login()

        ghost.browse_to_product(12)  # Folding Desk Lamp
        ghost.add_to_cart(12, 1)
        human_pause(1.2)
        ghost.browse_to_product(9)  # Cedar & Smoke Candle
        ghost.add_to_cart(9, 1)
        human_pause(0.9)

        cart_before = ghost.go_to_cart()
        subtotal_before = int(cart_before["subtotal_cents"])
        total_before = int(cart_before["total_cents"])

        # ── three attempts, three rejections, three messages ────────────── #
        for index, code in enumerate(ATTEMPTS, start=1):
            if index > 1:
                human_pause(4.0)  # retyping it, convinced it is a typo
            response = ghost.apply_coupon(code)
            statuses.append(response.status_code)
            try:
                codes.append(str(response.json().get("error")))
            except Exception:  # noqa: BLE001
                codes.append(f"<unparseable {response.status_code}>")

            ghost.ui_message(
                "coupon-error",
                EXPIRY_MESSAGE,
                attempt=index,
                typed=code,
                error_code=codes[-1],
            )
            human_pause(1.6)

        if not all(status == 400 for status in statuses):
            raise GhostFailure(
                f"EXPIRED15 did not answer 400 every time: {statuses} — "
                "this ticket is only meaningful if the API is behaving correctly"
            )
        if not all(code == "coupon_expired" for code in codes):
            raise GhostFailure(f"expected coupon_expired every time, got {codes}")

        # The price did not move, which is his actual complaint.
        cart_after = ghost.api("GET", "/api/cart", expect=200).json()
        human_pause(3.0)

        # He gives up and writes to support.
        ghost.click("cart-continue-shopping", text_="Browse products", tag="a")
        ghost.navigate("/", via="pushState", title="ShopForge")
        human_pause(2.2)
        ghost.flush()

    # ── verification ───────────────────────────────────────────────────── #
    def want(events: list[dict]) -> bool:
        return len(requests_to(events, "/api/cart/coupon")) >= len(ATTEMPTS)

    events = wait_for_events(ghost.session_id, want, label="coupon rejections")

    coupon_requests = requests_to(events, "/api/cart/coupon")
    rejected_400 = [e for e in coupon_requests if int(attrs_of(e).get("status") or 0) == 400]
    rejections = select_events(
        events,
        source="api",
        kind="business",
        name="coupon_rejected",
        where=lambda e: attrs_of(e).get("reason") == "coupon_expired",
    )
    shown = select_events(
        events,
        source="web",
        kind="business",
        name="ui_message_shown",
        where=lambda e: attrs_of(e).get("testid") == "coupon-error",
    )
    errors_anywhere = select_events(events, level="error")
    unchanged = (
        int(cart_after["subtotal_cents"]) == subtotal_before
        and int(cart_after["total_cents"]) == total_before
        and int(cart_after["discount_cents"]) == 0
    )

    checks = [
        Check(
            "every attempt was rejected with 400",
            len(rejected_400) >= len(ATTEMPTS),
            f"{len(rejected_400)} of {len(coupon_requests)} coupon requests answered 400",
        ),
        Check(
            "the reason recorded is coupon_expired",
            len(rejections) >= len(ATTEMPTS),
            f"{len(rejections)} coupon_rejected/coupon_expired business events",
        ),
        Check(
            "the customer was shown the expiry message",
            len(shown) >= len(ATTEMPTS),
            f'"{EXPIRY_MESSAGE}" rendered {len(shown)} times',
        ),
        Check(
            "the cart total never changed",
            unchanged,
            f"subtotal {subtotal_before} -> {cart_after['subtotal_cents']}, "
            f"total {total_before} -> {cart_after['total_cents']}",
        ),
        Check(
            "zero errors anywhere in the session (this is correct behaviour)",
            not errors_anywhere,
            summarise(errors_anywhere) if errors_anywhere else "web and api both clean",
        ),
    ]

    return GhostResult(
        ticket=TICKET,
        title=TITLE,
        persona=PERSONA,
        session_id=ghost.session_id,
        checks=checks,
        traces=ghost.traces,
        event_count=len(events),
    )


if __name__ == "__main__":
    import sys

    import run_all as harness

    sys.exit(harness.cli(sys.modules[__name__]))
