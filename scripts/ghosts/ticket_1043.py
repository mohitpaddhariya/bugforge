#!/usr/bin/env python3
"""Ghost run for ticket #1043 — BUG-002, the invisible click.

    "the place order button doesnt do anything on my phone. i press it and
     literally nothing ... i pressed it a bunch of times"   — mei

This is the ghost that justifies frontend telemetry existing. mei shops on an
iPhone at 390x844. Below the 768px breakpoint the promo banner's dismiss layer
(``.promo-layer--wide``: ``position:fixed; bottom:0; height:96px; z-index:9998``)
covers the docked checkout action bar (``z-index:30``) that holds Place Order.
The layer is transparent, so nothing looks wrong.

She taps Place Order four times. Every tap dispatches a click — onto the
overlay. No handler on the button runs, **no fetch is ever issued**, and the
backend records literally nothing. The only evidence anywhere is four web-side
``click`` events whose recorded hit target is ``promo-dismiss-layer`` with the
Place Order button sitting underneath it in ``element_stack_at_point``.

So this ghost asserts two things that are usually opposites: that the web events
exist, and that the api events **do not**. A ghost that quietly started issuing
checkout requests here would destroy the whole point of the bug.
"""

from __future__ import annotations

from run_all import (
    IPHONE_UA,
    Check,
    Ghost,
    GhostFailure,
    GhostResult,
    apply_flags,
    attrs_of,
    element,
    human_pause,
    select_events,
    summarise,
    wait_for_events,
)

TICKET = 1043
TITLE = "Place Order does nothing on mobile (promo overlay eats the tap)"
PERSONA = "mei@example.com"
FLAGS = {"BUG_PROMO_OVERLAY": True}

VIEWPORT = (390, 844)

#: The docked action bar puts the button across the bottom of the screen.
PLACE_ORDER_RECT = {"x": 16, "y": 786, "w": 358, "h": 44}
#: ...and the wide promo layer covers the bottom 96px of the viewport.
OVERLAY_RECT = {"x": 0, "y": 748, "w": 390, "h": 96}

TAP_POINT = (195, 808)

#: How many times she tapped. The ticket says "a bunch"; four is what the
#: repro in bugs/BUG-002.yaml describes.
TAPS = 4


def _overlay() -> dict:
    return element(
        tag="div",
        testid="promo-dismiss-layer",
        classes="promo-layer promo-layer--wide",
        rect=OVERLAY_RECT,
        position="fixed",
        z_index="9998",
        pointer_events="auto",
        opacity="1",
        background="rgba(0, 0, 0, 0)",
    )


def run() -> GhostResult:
    apply_flags(FLAGS)

    ghost = Ghost(
        ticket=TICKET,
        title=TITLE,
        email=PERSONA,
        viewport=VIEWPORT,
        user_agent=IPHONE_UA,
        locale="en-GB",
        device_pixel_ratio=3.0,
    )

    tap_traces: list[str] = []

    with ghost:
        ghost.login()

        ghost.browse_to_product(6)  # Everyday Merino Tee
        ghost.add_to_cart(6, 2)
        human_pause(1.4)

        cart = ghost.go_to_cart()
        if not cart.get("items"):
            raise GhostFailure("cart is empty — cannot reach checkout")

        ghost.go_to_checkout()
        human_pause(2.8)  # filling in the address on a phone takes a while

        # ── four taps that land on nothing ──────────────────────────────── #
        for delay in (0.0, 2.3, 3.1, 4.4)[:TAPS]:
            human_pause(delay)
            tap_traces.append(
                ghost.blocked_click(
                    intended_testid="place-order",
                    intended_text="Place Order",
                    intended_rect=PLACE_ORDER_RECT,
                    overlay=_overlay(),
                    at=TAP_POINT,
                )
            )

        # She checks the total is still right (it is), then gives up.
        human_pause(3.0)
        ghost.click("checkout-summary-total", text_="Total", tag="span", listener_ran=False)
        human_pause(4.5)
        ghost.navigate("/cart", via="popstate", title="Your cart")
        ghost.flush()

    # ── verification ───────────────────────────────────────────────────── #
    def want(events: list[dict]) -> bool:
        clicks = select_events(
            events,
            source="web",
            kind="click",
            where=lambda e: bool(attrs_of(e).get("click_blocked_by_overlay")),
        )
        # Wait for the api side too: we are about to assert it is empty, and an
        # empty result that just has not arrived yet would be a false pass.
        return len(clicks) >= TAPS and bool(select_events(events, source="api"))

    events = wait_for_events(ghost.session_id, want, label="blocked taps")

    session_meta = select_events(events, source="web", kind="vitals", name="session_start")
    viewports = [int(attrs_of(e).get("viewport_w") or 0) for e in session_meta]

    blocked = select_events(
        events,
        source="web",
        kind="click",
        where=lambda e: bool(attrs_of(e).get("click_blocked_by_overlay")),
    )
    on_overlay = [
        e
        for e in blocked
        if str((attrs_of(e).get("hit_element") or {}).get("testid")) == "promo-dismiss-layer"
    ]
    names_button_underneath = [
        e
        for e in blocked
        if str((attrs_of(e).get("obscured_interactive_element") or {}).get("testid"))
        == "place-order"
    ]

    tap_trace_ids = {e.get("trace_id") for e in blocked}
    api_in_tap_traces = select_events(
        events, source="api", where=lambda e: e.get("trace_id") in tap_trace_ids
    )
    checkout_fetches = select_events(
        events,
        source="web",
        kind="fetch",
        where=lambda e: "/api/checkout" in str(attrs_of(e).get("path") or ""),
    )
    checkout_requests = select_events(
        events,
        source="api",
        kind="request",
        where=lambda e: "/api/checkout" in str(attrs_of(e).get("route") or "")
        or "/api/checkout" in str(attrs_of(e).get("path") or ""),
    )
    api_errors = select_events(events, source="api", level="error")
    api_events = select_events(events, source="api")

    checks = [
        Check(
            "session recorded at a mobile viewport",
            bool(viewports) and all(0 < w < 768 for w in viewports),
            f"viewport widths {viewports or 'missing'}",
        ),
        Check(
            f"{TAPS} taps recorded, all swallowed by an overlay",
            len(blocked) >= TAPS,
            f"{len(blocked)} click events with click_blocked_by_overlay=true",
        ),
        Check(
            "the real hit target is promo-dismiss-layer",
            len(on_overlay) >= TAPS,
            f"{len(on_overlay)} taps landed on the promo layer",
        ),
        Check(
            "place-order recorded as the obscured element underneath",
            len(names_button_underneath) >= TAPS,
            f"{len(names_button_underneath)} taps name place-order as obscured",
        ),
        Check(
            "no checkout request was ever made",
            not checkout_fetches and not checkout_requests,
            f"{len(checkout_fetches)} web fetches, {len(checkout_requests)} api requests to "
            "/api/checkout",
        ),
        Check(
            "the tap traces contain zero api events",
            not api_in_tap_traces,
            f"{len(api_in_tap_traces)} api events in the tap traces "
            f"({summarise(api_in_tap_traces)})",
        ),
        Check(
            "zero api-side errors in the whole session",
            not api_errors,
            f"{len(api_errors)} api error events across {len(api_events)} api events",
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
