"""In-code ticket store for supportdesk.

Deliberately NOT backed by the shared database. supportdesk must stay up and
serve tickets even when `db`, `api` and `collector` are all broken, because the
robot reads the ticket first and investigates second.

Five tickets, matching docs/01-store-spec.md section 7. Written in customer
voice on purpose: lowercase, typos, wrong theories, irrelevant detail, no steps
to reproduce. If these ever start reading like engineer-written bug reports the
whole exercise is compromised.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Ticket shape:
#   id              int      ticket number shown to agents
#   subject         str      customer's own subject line
#   body            str      customer's own words
#   customer_email  str      matches a seeded shop.users row
#   customer_name   str
#   status          str      open | pending | closed
#   opened_at       str      ISO8601 with milliseconds, UTC
#   browser         str      from the support widget's user agent sniff
#   device          str      desktop | mobile | tablet
# ---------------------------------------------------------------------------

TICKETS: List[Dict[str, Any]] = [
    {
        # BUG-001 — coupon race (TOCTOU)
        "id": 1042,
        "subject": "order wont go thru?? charged twice??",
        "body": (
            "hi so i was trying to place my order last night and it just spins. "
            "the button goes grey and there is a little spinny thing and it stays "
            "like that forever, i waited like 2 minutes. nothing happens, no "
            "confirmation email either.\n\n"
            "i had the SAVE20 code on it, i got it from the newsletter. i clicked "
            "place order like 3 times because nothing was happening so now im "
            "worried you charged me 3 times?? please check. i have not seen "
            "anything on my card yet but sometimes it takes a day.\n\n"
            "i also tried again this morning on my laptop, same thing. my husband "
            "says its probably my wifi but everything else works fine, i was on a "
            "video call right before.\n\n"
            "can you just place the order for me manually? i need it before the "
            "14th. thanks\n\n"
            "priya"
        ),
        "customer_email": "priya@example.com",
        "customer_name": "Priya Nair",
        "status": "open",
        "opened_at": "2026-08-05T21:47:13.402Z",
        "browser": "Chrome 128 / macOS 14.5",
        "device": "desktop",
    },
    {
        # BUG-002 — invisible click (promo overlay)
        "id": 1043,
        "subject": "cant order on my phone",
        "body": (
            "the place order button doesnt do anything on my phone. i press it and "
            "literally nothing, no error no nothing, it just sits there. i pressed "
            "it a bunch of times.\n\n"
            "everything else works, i can add stuff to the bag and the total looks "
            "right, its only the last button. i thought maybe my screen protector "
            "was messing it up (its cracked in the corner) but other apps are fine "
            "so i dont think its that.\n\n"
            "i borrowed my flatmates laptop later and it worked there straight "
            "away, so its a phone thing. iphone 13, i have the latest update.\n\n"
            "you should really test your site on phones, most people shop on "
            "phones now.\n\n"
            "mei"
        ),
        "customer_email": "mei@example.com",
        "customer_name": "Mei Tanaka",
        "status": "open",
        "opened_at": "2026-08-06T08:12:55.118Z",
        "browser": "Mobile Safari 17.5 / iOS 17.5.1",
        "device": "mobile",
    },
    {
        # BUG-003 — contract drift, total_cents -> total
        "id": 1044,
        "subject": "my order says NaN dollars??",
        "body": (
            "hello me again (sorry). different problem this time.\n\n"
            "i placed an order and where the total is supposed to be it says "
            "$NaN. thats not a number?? it says it on the page after i ordered and "
            "also when i go to my orders list it says $NaN there too for that one. "
            "the older orders look normal.\n\n"
            "so did it go through or not. how much am i being charged. is it zero. "
            "i dont want to get a surprise bill for something weird.\n\n"
            "the items are all listed correctly and the prices next to each item "
            "look fine, its just the big total at the bottom thats broken. maybe "
            "its because i used a discount code? or maybe your site doesnt like "
            "indian cards, ive had that on other sites.\n\n"
            "screenshot attached (i think, the attach thing was being slow)\n\n"
            "priya"
        ),
        "customer_email": "priya@example.com",
        "customer_name": "Priya Nair",
        "status": "open",
        "opened_at": "2026-08-06T15:33:41.771Z",
        "browser": "Chrome 128 / macOS 14.5",
        "device": "desktop",
    },
    {
        # BUG-004 — order leak (IDOR), innocent wording
        "id": 1045,
        "subject": "wrong order showing in my account",
        "body": (
            "hey, i clicked on one of my orders from the orders page and it opened "
            "up someone elses stuff? theres a jacket on there, a green one, i "
            "definitely never bought a jacket. i dont even wear jackets much, i "
            "live in chennai lol.\n\n"
            "the name and address bit i didnt really look at properly, i just saw "
            "the jacket and got confused and closed it.\n\n"
            "i think your site has mixed up the order numbers or something, maybe "
            "two people got the same number. anyway can you make sure im not being "
            "billed for the jacket. and if someone else got my headphones can you "
            "sort that out.\n\n"
            "not a big deal just thought you should know\n\n"
            "arjun"
        ),
        "customer_email": "arjun@example.com",
        "customer_name": "Arjun Mehta",
        "status": "open",
        "opened_at": "2026-08-07T11:05:09.986Z",
        "browser": "Firefox 129 / Windows 11",
        "device": "desktop",
    },
    {
        # BUG-005 — NOT a bug. EXPIRED15 genuinely expired.
        "id": 1046,
        "subject": "your discount codes dont work",
        "body": (
            "your discount codes dont work. i typed EXPIRED15 in the box on the "
            "cart page and hit apply and it wont apply, the price stays the same. "
            "tried it like 5 times, also tried it in caps and small letters and "
            "with a space after in case that mattered.\n\n"
            "i got this code from a deals site so i know its real. this is false "
            "advertising basically. i have been a customer since last year and "
            "this is the second time something like this has happened (last time "
            "was a delivery thing, different issue).\n\n"
            "there was some red text but i didnt read it properly, it went away "
            "when i clicked. probably some generic error.\n\n"
            "fix your site please. i want the 15% honoured either way.\n\n"
            "rahul"
        ),
        "customer_email": "rahul@example.com",
        "customer_name": "Rahul Verma",
        "status": "open",
        "opened_at": "2026-08-07T19:26:34.245Z",
        "browser": "Chrome 127 / Windows 10",
        "device": "desktop",
    },
]

# Fast lookup by id, built once at import.
_BY_ID: Dict[int, Dict[str, Any]] = {t["id"]: t for t in TICKETS}


def list_tickets() -> List[Dict[str, Any]]:
    """All tickets, newest first."""
    return sorted(TICKETS, key=lambda t: t["opened_at"], reverse=True)


def get_ticket(ticket_id: int) -> Optional[Dict[str, Any]]:
    """One ticket, or None if there is no such id."""
    return _BY_ID.get(ticket_id)
