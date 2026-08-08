#!/usr/bin/env python3
"""Ghost run for ticket #1042 — BUG-001, the coupon race.

    "i was trying to place my order last night and it just spins ... i had the
     SAVE20 code on it ... i clicked place order like 3 times"   — priya

What this reproduces, on the real app:

* priya signs in on a laptop (1440x900), fills a cart and applies ``SAVE20``,
  which the seed leaves at 4 of 5 uses;
* **two checkouts land at the same instant** (she had the tab open twice, which
  is exactly the double-submit the ticket implies). Both pass a guard that read
  ``uses=4``; Postgres serialises the two UPDATEs, the loser writes 6 and trips
  ``CHECK (uses <= max_uses)`` -> ``IntegrityError`` -> **HTTP 500**;
* with ``BUG_CHECKOUT_SWALLOWS_ERROR`` on, the checkout page awaits the failed
  request outside any ``try``, so the rejection goes unhandled, no error is ever
  rendered, and the button stays in its loading state — "it just spins";
* she clicks Place Order three more times. The button is disabled, so those
  clicks produce **no network request at all** — three clicks, no fetches. That
  pairing is the fingerprint of the second, separate frontend defect;
* she gives up and leaves.

Afterwards the coupon is put back to 4 of 5, because the robot has to find the
store primed the way the seed left it.
"""

from __future__ import annotations

import threading

from run_all import (
    DESKTOP_UA,
    Check,
    Ghost,
    GhostFailure,
    GhostResult,
    apply_flags,
    attrs_of,
    coupon_state,
    human_pause,
    new_trace_id,
    reprime_coupon,
    requests_to,
    select_events,
    summarise,
    wait_for_events,
)

TICKET = 1042
TITLE = "Place Order spins forever with SAVE20 applied"
PERSONA = "priya@example.com"
FLAGS = {"BUG_COUPON_TOCTOU": True, "BUG_CHECKOUT_SWALLOWS_ERROR": True}

COUPON = "SAVE20"

#: Place Order sits inline in the summary card at desktop widths.
PLACE_ORDER_RECT = {"x": 1012, "y": 604, "w": 356, "h": 44}

#: How many times we are willing to re-stage the collision. One well-timed pair
#: is normally enough; the extra attempts exist because a race that does not
#: race is a silently degraded ghost, and that is the thing we refuse to ship.
MAX_COLLISION_ATTEMPTS = 3


def _fire_concurrent_checkouts(ghost: Ghost, traces: tuple[str, str]) -> list[int]:
    """Two POST /api/checkout, released from a barrier at the same moment."""
    barrier = threading.Barrier(2)
    statuses: dict[int, int] = {}
    errors: list[str] = []

    def submit(index: int, trace_id: str) -> None:
        try:
            barrier.wait(timeout=10)
            response = ghost.api(
                "POST",
                "/api/checkout",
                json_body={
                    "coupon_code": COUPON,
                    "address": {
                        "name": "Priya Nair",
                        "line1": "14 Anna Salai",
                        "city": "Chennai",
                        "postal_code": "600002",
                        "country": "IN",
                    },
                },
                trace_id=trace_id,
            )
            statuses[index] = response.status_code
        except Exception as exc:  # noqa: BLE001 - reported through `errors`
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=submit, args=(index, trace), name=f"ghost-checkout-{index}")
        for index, trace in enumerate(traces)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    if errors:
        raise GhostFailure("concurrent checkout could not be driven: " + "; ".join(errors))
    return [statuses[i] for i in sorted(statuses)]


def _restage_cart(ghost: Ghost) -> None:
    """Put a fresh cart and a fresh SAVE20 in place for another collision.

    The winning checkout converts the cart and burns a coupon use, so a retry
    needs both rebuilt. Everything here goes through the real API, so it is all
    still authentic telemetry.
    """
    reprime_coupon(COUPON)
    ghost.api("POST", "/api/cart/items", json_body={"product_id": 3, "qty": 1}, expect=201)
    ghost.api("POST", "/api/cart/coupon", json_body={"code": COUPON}, expect=200)


def run() -> GhostResult:
    apply_flags(FLAGS)
    reprime_coupon(COUPON)

    ghost = Ghost(
        ticket=TICKET,
        title=TITLE,
        email=PERSONA,
        viewport=(1440, 900),
        user_agent=DESKTOP_UA,
        locale="en-IN",
    )

    live_500 = False
    live_201 = False
    retry_traces: list[str] = []

    with ghost:
        ghost.login()

        # Shopping.
        ghost.browse_to_product(3)  # Trailhead Earbuds
        ghost.add_to_cart(3, 1)
        human_pause(1.3)
        ghost.browse_to_product(9)  # Cedar & Smoke Candle
        ghost.add_to_cart(9, 2)
        human_pause(0.8)

        ghost.go_to_cart()

        response = ghost.apply_coupon(COUPON)
        if response.status_code != 200:
            raise GhostFailure(
                f"{COUPON} would not apply ({response.status_code}: {response.text[:200]}) — "
                "the seed should leave it usable at 4 of 5"
            )
        ghost.ui_message("coupon-applied", f"{COUPON} applied - 20% off", code=COUPON)
        human_pause(1.1)

        ghost.go_to_checkout()
        human_pause(2.4)  # reading the address form

        # ── the collision ───────────────────────────────────────────────── #
        for attempt in range(1, MAX_COLLISION_ATTEMPTS + 1):
            if attempt > 1:
                # "i also tried again this morning on my laptop, same thing"
                human_pause(2.0)
                _restage_cart(ghost)
                ghost.reload("/checkout")
                ghost.api("GET", "/api/cart", expect=200)
                human_pause(1.2)

            trace_a = ghost.open_interaction("click")
            ghost.click(
                "place-order",
                text_="Place Order",
                rect=PLACE_ORDER_RECT,
                new_interaction=False,
                extra={"attempt": attempt},
            )
            # The second tab, same browser session, milliseconds later.
            trace_b = new_trace_id()
            ghost.traces.append(trace_b)
            ghost.record(
                "click",
                "place-order",
                {
                    "selector": 'button[data-testid="place-order"]',
                    "testid": "place-order",
                    "text": "Place Order",
                    "tag": "button",
                    "hit_element": {
                        "tag": "button",
                        "testid": "place-order",
                        "text": "Place Order",
                        "selector": 'button[data-testid="place-order"]',
                        "disabled": False,
                        "rect": PLACE_ORDER_RECT,
                    },
                    "hit_is_intended_target": True,
                    "click_blocked_by_overlay": False,
                    "listeners_on_path": 1,
                    "listener_ran": True,
                    "viewport_w": ghost.viewport_w,
                    "viewport_h": ghost.viewport_h,
                    "route": "/checkout",
                    "duplicate_tab": True,
                    "attempt": attempt,
                },
                trace_id=trace_b,
            )

            statuses = _fire_concurrent_checkouts(ghost, (trace_a, trace_b))
            live_500 = any(status >= 500 for status in statuses)
            live_201 = any(200 <= status < 300 for status in statuses)
            if live_500:
                break

        # ── the spinner that never stops ────────────────────────────────── #
        if live_500:
            # BUG_CHECKOUT_SWALLOWS_ERROR: placeOrder() awaits outside a try, so
            # the rejection is never caught, `submitting` is never cleared, and
            # nothing is rendered to the customer.
            human_pause(0.4)
            ghost.unhandled_rejection(
                "HTTP 500 from POST /api/checkout", error_type="ApiError"
            )

        # Three retries on a button that is stuck in its loading state. The
        # clicks are recorded; no request follows any of them.
        for index, delay in enumerate((3.2, 4.1, 5.6), start=1):
            human_pause(delay)
            retry_traces.append(
                ghost.click(
                    "place-order",
                    text_="Placing order...",
                    rect=PLACE_ORDER_RECT,
                    disabled=True,
                    listener_ran=False,
                    extra={"retry": index, "still_loading": True},
                )
            )

        # She gives up.
        human_pause(6.0)
        ghost.navigate("/cart", via="popstate", title="Your cart")
        ghost.flush()

    # The store must be handed back primed exactly as the seed left it.
    reprime_coupon(COUPON)

    # ── verification ───────────────────────────────────────────────────── #
    def want(events: list[dict]) -> bool:
        return bool(
            select_events(
                events,
                source="api",
                kind="request",
                where=lambda e: attrs_of(e).get("status") is not None
                and int(attrs_of(e).get("status") or 0) >= 500,
            )
        ) and bool(select_events(events, source="api", kind="error"))

    events = wait_for_events(ghost.session_id, want, label="checkout 500")

    checkout_requests = requests_to(events, "/api/checkout")
    failed = [e for e in checkout_requests if int(attrs_of(e).get("status") or 0) >= 500]
    succeeded = [e for e in checkout_requests if 200 <= int(attrs_of(e).get("status") or 0) < 300]
    api_errors = select_events(events, source="api", kind="error")
    integrity = [
        e
        for e in api_errors
        if "IntegrityError" in str(attrs_of(e).get("exception_type") or e.get("name") or "")
    ]
    located = [e for e in integrity if "checkout" in str(attrs_of(e).get("file") or "")]
    over_redeemed = select_events(
        events,
        source="api",
        kind="business",
        name="checkout_failed",
        where=lambda e: attrs_of(e).get("reason") == "coupon_over_redeemed",
    )
    retries = select_events(
        events,
        source="web",
        kind="click",
        name="place-order",
        where=lambda e: bool(attrs_of(e).get("retry")),
    )
    retry_trace_ids = {e.get("trace_id") for e in retries}
    requests_in_retry_traces = select_events(
        events, source="api", where=lambda e: e.get("trace_id") in retry_trace_ids
    )
    swallowed = select_events(events, source="web", kind="error", name="unhandledrejection")

    coupon = coupon_state(COUPON) or {}

    checks = [
        Check(
            "concurrent checkout returned 500",
            bool(failed) and live_500,
            f"{len(failed)} of {len(checkout_requests)} POST /api/checkout answered 5xx",
        ),
        Check(
            "IntegrityError captured with file:line",
            bool(located),
            summarise(located) if located else f"api error events: {summarise(api_errors)}",
        ),
        Check(
            "checkout_failed business event names the over-redemption",
            bool(over_redeemed),
            f"{len(over_redeemed)} coupon_over_redeemed event(s)",
        ),
        Check(
            "the other checkout succeeded",
            bool(succeeded) and live_201,
            f"{len(succeeded)} order(s) created in the race",
        ),
        Check(
            "frontend swallowed the failure (no error shown)",
            bool(swallowed),
            "unhandledrejection recorded, checkout page rendered no error",
        ),
        Check(
            "three retry clicks, none of which made a request",
            len(retries) >= 3 and not requests_in_retry_traces,
            f"{len(retries)} retry clicks, {len(requests_in_retry_traces)} requests behind them",
        ),
        Check(
            f"{COUPON} handed back primed at max_uses-1",
            int(coupon.get("uses", -1)) == int(coupon.get("max_uses", 0)) - 1,
            f"uses={coupon.get('uses')}/{coupon.get('max_uses')}",
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
