#!/usr/bin/env python3
"""Ghost run for ticket #1044 — BUG-003, contract drift.

    "i placed an order and where the total is supposed to be it says $NaN ...
     so did it go through or not. how much am i being charged"   — priya

With ``BUG_TOTAL_FIELD_RENAME`` on, ``serialize_order`` ships the total as
``total`` instead of ``total_cents``. The order is fine. The money is fine. The
web app reads ``total_cents``, gets ``undefined``, and ``money()`` renders
``$NaN`` — on the confirmation page and again in the history list.

Neither side is wrong on its own, which is why the shortcut is response-shape
telemetry (spec §6.4): the api ``request`` event carries ``response_keys``, and
those keys contain ``total`` where every older order carried ``total_cents``.
This ghost asserts exactly that, plus the thing that makes it a customer
problem: **no error anywhere**. The checkout succeeded, the API is happy, the
logs are clean, and the customer still cannot tell whether she has been charged.

She reloads the page twice, which is what people do when a number looks wrong.
"""

from __future__ import annotations

from run_all import (
    DESKTOP_UA,
    Check,
    Ghost,
    GhostFailure,
    GhostResult,
    apply_flags,
    attrs_of,
    human_pause,
    requests_to,
    select_events,
    summarise,
    wait_for_events,
)

TICKET = 1044
TITLE = "Order total renders as $NaN after checkout"
PERSONA = "priya@example.com"
FLAGS = {"BUG_TOTAL_FIELD_RENAME": True}

PLACE_ORDER_RECT = {"x": 1012, "y": 604, "w": 356, "h": 44}

#: What the confirmation page logs when the total it was handed is not a number.
NAN_CONSOLE_MESSAGE = (
    "Order total is not a number: expected total_cents, received undefined - rendering $NaN"
)
NAN_STACK = (
    "TypeError: total_cents is undefined\n"
    "    at OrderTotal (webpack-internal:///./components/order-summary.tsx:57:14)\n"
    "    at OrderDetailPage (webpack-internal:///./app/orders/[id]/page.tsx:118:9)"
)


def run() -> GhostResult:
    apply_flags(FLAGS)

    ghost = Ghost(
        ticket=TICKET,
        title=TITLE,
        email=PERSONA,
        viewport=(1440, 900),
        user_agent=DESKTOP_UA,
        locale="en-IN",
    )

    with ghost:
        ghost.login()

        ghost.browse_to_product(10)  # Stoneware Mug Set
        ghost.add_to_cart(10, 2)
        human_pause(1.1)

        ghost.go_to_cart()
        ghost.go_to_checkout()
        human_pause(2.1)

        # ── a checkout that works perfectly ─────────────────────────────── #
        ghost.click("place-order", text_="Place Order", rect=PLACE_ORDER_RECT)
        human_pause(0.1)
        response = ghost.api(
            "POST",
            "/api/checkout",
            json_body={
                "address": {
                    "name": "Priya Nair",
                    "line1": "14 Anna Salai",
                    "city": "Chennai",
                    "postal_code": "600002",
                    "country": "IN",
                }
            },
            expect=201,
        )
        order = response.json()
        order_id = int(order["id"])
        if "total_cents" in order and "total" not in order:
            raise GhostFailure(
                "checkout still returned total_cents — BUG_TOTAL_FIELD_RENAME is not in effect, "
                "so there is no $NaN to reproduce"
            )

        # ── the confirmation page renders $NaN ──────────────────────────── #
        ghost.navigate(f"/orders/{order_id}", title="Order")
        ghost.api("GET", f"/api/orders/{order_id}", expect=200)
        human_pause(0.3)
        ghost.console_error(NAN_CONSOLE_MESSAGE, stack=NAN_STACK)
        ghost.ui_message(
            "order-summary-total",
            "$NaN",
            order_id=order_id,
            expected_field="total_cents",
            received_fields=sorted(order.keys()),
        )
        human_pause(4.2)  # staring at it

        # ── reload. twice. ──────────────────────────────────────────────── #
        for _ in range(2):
            ghost.reload(f"/orders/{order_id}")
            ghost.api("GET", f"/api/orders/{order_id}", expect=200)
            human_pause(0.4)
            ghost.console_error(NAN_CONSOLE_MESSAGE, stack=NAN_STACK)
            ghost.ui_message("order-summary-total", "$NaN", order_id=order_id)
            human_pause(3.4)

        # "when i go to my orders list it says $NaN there too for that one"
        ghost.click("nav-orders", text_="Orders", tag="a")
        ghost.navigate("/orders", title="Your orders")
        ghost.api("GET", "/api/orders", expect=200)
        human_pause(1.2)
        ghost.ui_message(f"order-total-{order_id}", "$NaN", order_id=order_id)
        human_pause(2.5)
        ghost.flush()

    # ── verification ───────────────────────────────────────────────────── #
    def want(events: list[dict]) -> bool:
        return (
            len(requests_to(events, "/api/orders/")) >= 3
            and bool(select_events(events, source="web", kind="console"))
        )

    events = wait_for_events(ghost.session_id, want, label="NaN confirmation")

    checkout_requests = requests_to(events, "/api/checkout")
    renamed_checkout = [
        e
        for e in checkout_requests
        if "total" in (attrs_of(e).get("response_keys") or [])
        and "total_cents" not in (attrs_of(e).get("response_keys") or [])
    ]
    detail_requests = [
        e for e in requests_to(events, "/api/orders/") if attrs_of(e).get("response_keys")
    ]
    renamed_detail = [
        e
        for e in detail_requests
        if "total" in (attrs_of(e).get("response_keys") or [])
        and "total_cents" not in (attrs_of(e).get("response_keys") or [])
    ]
    nan_console = select_events(
        events,
        source="web",
        kind="console",
        where=lambda e: "NaN" in str(attrs_of(e).get("message") or ""),
    )
    nan_rendered = select_events(
        events,
        source="web",
        kind="business",
        name="ui_message_shown",
        where=lambda e: str(attrs_of(e).get("message") or "") == "$NaN",
    )
    reloads = select_events(
        events,
        source="web",
        kind="nav",
        name="page_load",
        where=lambda e: bool(attrs_of(e).get("reload")),
    )
    api_errors = select_events(events, source="api", kind="error")
    failed_requests = select_events(
        events,
        source="api",
        kind="request",
        where=lambda e: int(attrs_of(e).get("status") or 0) >= 400,
    )

    checks = [
        Check(
            "checkout succeeded but shipped `total` instead of `total_cents`",
            bool(renamed_checkout),
            f"response_keys: {(attrs_of(checkout_requests[0]).get('response_keys') if checkout_requests else 'no checkout request')}",
        ),
        Check(
            "order detail carries the same renamed key",
            len(renamed_detail) >= 3,
            f"{len(renamed_detail)} of {len(detail_requests)} order reads used `total`",
        ),
        Check(
            "the customer's browser logged the NaN",
            bool(nan_console),
            summarise(nan_console),
        ),
        Check(
            "$NaN was actually rendered to her",
            len(nan_rendered) >= 2,
            f"{len(nan_rendered)} places rendered $NaN",
        ),
        Check(
            "she reloaded twice",
            len(reloads) >= 2,
            f"{len(reloads)} full page reloads",
        ),
        Check(
            "the backend never noticed (no errors, nothing non-2xx)",
            not api_errors and not failed_requests,
            f"{len(api_errors)} api errors, {len(failed_requests)} non-2xx responses",
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
