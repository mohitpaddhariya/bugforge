#!/usr/bin/env python3
"""Ghost run for ticket #1045 — BUG-004, the order leak (IDOR).

    "i clicked on one of my orders from the orders page and it opened up
     someone elses stuff? theres a jacket on there, a green one, i definitely
     never bought a jacket"   — arjun

``GET /api/orders/{id}`` fetches by primary key with no
``AND user_id = current_user``, so arjun's session reads an order belonging to
priya — the Moss Green Field Jacket the seed gives her — and the API answers a
clean ``200`` with her line items.

The interesting part of this session is what is *absent*. No error, no 4xx, no
warning: to every log in the system this looks like a customer viewing an order.
That is what makes the ticket sound like a display glitch instead of a
disclosure incident, and it is why the ghost asserts the silence explicitly —
the robot has to recognise a security issue from telemetry that flags nothing.
"""

from __future__ import annotations

from run_all import (
    FIREFOX_UA,
    Check,
    Ghost,
    GhostFailure,
    GhostResult,
    apply_flags,
    attrs_of,
    human_pause,
    order_owned_by_someone_else,
    requests_to,
    select_events,
    user_id_for,
    wait_for_events,
)

TICKET = 1045
TITLE = "Order detail shows another customer's order (IDOR)"
PERSONA = "arjun@example.com"
FLAGS = {"BUG_ORDER_IDOR": True}


def run() -> GhostResult:
    apply_flags(FLAGS)

    arjun_id = user_id_for(PERSONA)
    target = order_owned_by_someone_else(PERSONA)
    leaked_order_id = int(target["order_id"])
    owner_id = int(target["user_id"])

    ghost = Ghost(
        ticket=TICKET,
        title=TITLE,
        email=PERSONA,
        viewport=(1512, 916),
        user_agent=FIREFOX_UA,
        locale="en-IN",
    )

    leaked_payload: dict = {}

    with ghost:
        user = ghost.login()
        if int(user["id"]) != arjun_id:
            raise GhostFailure("logged in as the wrong user")

        # His own history first — correctly scoped, which is what makes the
        # detail page's answer so confusing.
        ghost.click("nav-orders", text_="Orders", tag="a")
        ghost.navigate("/orders", title="Your orders")
        own = ghost.api("GET", "/api/orders", expect=200).json()
        own_ids = [int(o["id"]) for o in own.get("orders", [])]
        human_pause(2.6)

        # He opens an order from that page. The id he lands on is not his.
        ghost.click(f"order-link-{leaked_order_id}", text_="View order", tag="a")
        ghost.navigate(f"/orders/{leaked_order_id}", title="Order")
        response = ghost.api("GET", f"/api/orders/{leaked_order_id}", expect=200)
        leaked_payload = response.json()
        human_pause(0.4)

        if int(leaked_payload.get("user_id", arjun_id)) == arjun_id:
            raise GhostFailure(
                f"order #{leaked_order_id} came back owned by arjun — BUG_ORDER_IDOR is not in "
                "effect, so nothing leaked"
            )

        item_names = [i.get("name_snapshot", "") for i in leaked_payload.get("items", [])]
        ghost.ui_message(
            "order-items",
            ", ".join(item_names),
            order_id=leaked_order_id,
            viewing_user_id=arjun_id,
            payload_user_id=leaked_payload.get("user_id"),
        )

        # "i just saw the jacket and got confused and closed it"
        human_pause(5.5)
        ghost.click("back-to-orders", text_="Back to orders", tag="a")
        ghost.navigate("/orders", via="popstate", title="Your orders")
        ghost.api("GET", "/api/orders", expect=200)
        human_pause(2.0)
        ghost.flush()

    # ── verification ───────────────────────────────────────────────────── #
    def want(events: list[dict]) -> bool:
        return bool(requests_to(events, "/api/orders/"))

    events = wait_for_events(ghost.session_id, want, label="leaked order read")

    detail_requests = requests_to(events, "/api/orders/")
    ok_detail = [e for e in detail_requests if int(attrs_of(e).get("status") or 0) == 200]
    session_user_ids = {
        attrs_of(e).get("user_id") for e in select_events(events, source="api", kind="request")
    }
    api_errors = select_events(events, source="api", kind="error")
    non_2xx = select_events(
        events,
        source="api",
        kind="request",
        where=lambda e: int(attrs_of(e).get("status") or 0) >= 400,
    )
    cross_user = select_events(
        events,
        source="api",
        kind="business",
        name="order_viewed_cross_user",
        where=lambda e: attrs_of(e).get("owner_user_id") != attrs_of(e).get("viewer_user_id"),
    )
    jacket = [name for name in
              [i.get("name_snapshot", "") for i in leaked_payload.get("items", [])]
              if "jacket" in name.lower()]

    checks = [
        Check(
            "another customer's order came back 200",
            bool(ok_detail) and int(leaked_payload.get("user_id", -1)) == owner_id,
            f"order #{leaked_order_id} belongs to user {owner_id}, read by user {arjun_id}",
        ),
        Check(
            "the leaked payload contains the jacket from the ticket",
            bool(jacket),
            f"items: {[i.get('name_snapshot') for i in leaked_payload.get('items', [])]}",
        ),
        Check(
            "the order is not in arjun's own order list",
            leaked_order_id not in own_ids,
            f"his orders: {own_ids}",
        ),
        Check(
            "telemetry attributes the read to arjun's session",
            arjun_id in {v for v in session_user_ids if v is not None},
            f"request events carry user_id {sorted(v for v in session_user_ids if v is not None)}",
        ),
        Check(
            "the cross-user read is recorded as order_viewed_cross_user",
            bool(cross_user),
            f"{len(cross_user)} warn-level cross-user reads: "
            + ", ".join(
                f"order {attrs_of(e).get('order_id')} owner {attrs_of(e).get('owner_user_id')} "
                f"viewer {attrs_of(e).get('viewer_user_id')}"
                for e in cross_user[:2]
            ),
        ),
        Check(
            "nothing failed — the leak is a clean 200 (this is the dangerous part)",
            not api_errors and not non_2xx,
            f"{len(api_errors)} api errors, {len(non_2xx)} non-2xx responses",
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
