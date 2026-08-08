# Store Spec — "ShopForge"

The fake store the robot practices on. Covers phases **P0–P2**.

---

## 1. What this is for

ShopForge looks like a small e-commerce app. It is really three things at once:

1. **A realistic app** — real enough that a customer complaint about it sounds like a
   real customer complaint.
2. **A telemetry source** — every click and every query is recorded and joinable, so
   the robot can investigate history instead of guessing.
3. **A bug substrate** — every planted bug has a known file, a known line, a known
   symptom, and a switch.

Every design decision below is judged by one question: *does this make bugs more
realistic and more diagnosable?* Not: is this good e-commerce.

### Design principles

| Principle | Consequence |
|---|---|
| **The answer sheet is written first** | Every bug has a manifest with exact file/lines before any code is written |
| **Observability is not the thing under test** | The collector is a separate service with its own uptime. If the robot breaks the API, telemetry still works |
| **Telemetry is generated, never faked** | Historical customer sessions come from actually driving the buggy app (see §8 Ghost Runs) |
| **Deterministic reset** | `make reset` returns data *and* code to a known state. The robot mutates both |
| **Bugs toggle at runtime** | No rebuild to change scenario. Flip a row in the DB |
| **Small surface, deep instrumentation** | One flow done thoroughly beats five flows done shallowly |

### Explicitly out of scope

Payments, shipping, search, reviews, admin panel, inventory management, email,
responsive polish beyond what BUG-002 needs, accessibility, i18n beyond a `locale`
field, and any bug not in §7.

---

## 2. Services

Six containers. Everything in one `docker-compose.yml`.

| Service | Tech | Port | Role |
|---|---|---|---|
| `web` | Next.js 15 (App Router, TS, Tailwind) | 3000 | The store UI. **Under test.** Ships the telemetry tracker |
| `api` | FastAPI + SQLAlchemy 2.0 (sync) | 8000 | The store backend. **Under test.** Emits telemetry |
| `collector` | FastAPI | 8001 | Ingests telemetry from both sides, serves the query API. **Not under test** |
| `db` | Postgres 16 | 5432 | Two schemas: `shop` (app data) and `telemetry` |
| `supportdesk` | FastAPI + Jinja | 3001 | Ticket list and detail. Deliberately tiny, deliberately separate |
| `gitea` | Gitea | 3002 / 2222 | Sandboxed git host. Wired up in P5 |

**Why `collector` is separate:** the robot edits `api` code. If telemetry lived inside
`api`, a bad patch could blind the robot mid-investigation. The observability plane
must survive the data plane.

**Why `supportdesk` is separate:** same reason. The ticket system must stay up when
the store is broken.

```
        ┌──────────┐        ┌──────────┐
        │   web    │───────▶│   api    │
        └────┬─────┘  HTTP  └────┬─────┘
             │  X-Trace-Id       │
             │ telemetry         │ telemetry
             ▼                   ▼
          ┌────────────────────────┐
          │       collector        │──▶ query API for the robot
          └───────────┬────────────┘
                      ▼
                  ┌───────┐
                  │  db   │  schema: shop | telemetry
                  └───────┘
```

---

## 3. The flow

One user journey, end to end. Nothing else.

```
/            product grid
/product/:id product detail, add to cart
/cart        line items, qty, remove, coupon input
/checkout    address (prefilled), order summary, Place Order
/orders      order history
/orders/:id  order detail
/login       email + password
```

Auth is an opaque bearer token in a `sessions` table, delivered as an httpOnly cookie.
No JWT, no refresh flow, no signup — users are seeded.

---

## 4. Data model — schema `shop`

Money is **integer cents** everywhere. Bugs that violate this do so deliberately.

```
users
  id, email (unique), password_hash, name, locale, created_at

sessions
  token (pk), user_id, created_at, expires_at

products
  id, sku, name, description, price_cents, category, image_url, stock

carts
  id, user_id, status ∈ {open, converted}, created_at

cart_items
  id, cart_id, product_id, qty, unit_price_cents

coupons
  id, code (unique), kind ∈ {percent, fixed}, value,
  max_uses, uses, min_subtotal_cents, expires_at, active
  CHECK (uses <= max_uses)          ← load-bearing for BUG-001

orders
  id, user_id, status, coupon_code,
  subtotal_cents, discount_cents, tax_cents, total_cents, created_at

order_items
  id, order_id, product_id, name_snapshot, qty, unit_price_cents

feature_flags
  key (pk), enabled, description     ← the bug switches
```

The `CHECK (uses <= max_uses)` constraint is not decoration. It is what turns the
BUG-001 race from a silent over-redemption into a visible 500, which is what makes the
customer's symptom ("it just spins") match the ticket.

---

## 5. API surface

```
POST   /api/auth/login          → sets cookie
POST   /api/auth/logout
GET    /api/me

GET    /api/products
GET    /api/products/:id

GET    /api/cart
POST   /api/cart/items          { product_id, qty }
PATCH  /api/cart/items/:id      { qty }
DELETE /api/cart/items/:id
POST   /api/cart/coupon         { code }      → 400 on invalid/expired
DELETE /api/cart/coupon

POST   /api/checkout            → creates order, redeems coupon
GET    /api/orders
GET    /api/orders/:id                        ← BUG-004 lives here

GET    /api/debug/flags                       ← control plane, not customer-facing
POST   /api/debug/flags         { key, enabled }
POST   /api/debug/reset
```

The `/api/debug/*` routes are the harness control plane. They are excluded from
telemetry so the robot's own setup calls don't pollute the timeline it's reading.

---

## 6. Telemetry contract

This is the most important section. Everything the robot can do depends on it.

### 6.1 IDs

| ID | Scope | Generated by |
|---|---|---|
| `session_id` | One browser session | `web`, stored in `sessionStorage` |
| `trace_id` | **One user interaction** and everything it causes | `web`, on click / submit / navigation |

A `trace_id` is not per-request. It is per *intent*. One click on "Place Order" that
fires three API calls produces one `trace_id` covering all three. That is what lets
the robot ask "what happened when the user clicked this" rather than "what happened
during this HTTP request".

**Propagation:** `web` wraps global `fetch`. On any user interaction it opens an
interaction window (expires after 5s of inactivity) and stamps every outgoing request
with `X-Trace-Id`. `api` middleware reads the header (or mints one), binds it to a
contextvar, and every emission inside that request carries it.

### 6.2 Event table — schema `telemetry`

One wide table. Indexed on `trace_id`, `session_id`, `user_id`, `ts`.

```
events
  id, ts, trace_id, session_id, user_id,
  source ∈ {web, api},
  kind   ∈ {click, nav, fetch, console, error, request, sql, business, vitals},
  name, level ∈ {debug, info, warn, error},
  duration_ms, attrs (jsonb)
```

### 6.3 What `web` records

| kind | Captured |
|---|---|
| `click` | CSS selector path, element text, tag, whether a listener ran, whether default was prevented, **the element actually hit** |
| `nav` | route from → to |
| `fetch` | method, url, status, duration, request/response byte size, `X-Trace-Id` sent |
| `console` | `console.error` / `console.warn` |
| `error` | `window.onerror` + `unhandledrejection`, with stack |
| `vitals` | page load timings |
| session meta | viewport w/h, user agent, locale, device pixel ratio |

**"The element actually hit"** is what makes BUG-002 solvable. When an invisible
overlay eats a click, the click still fires — on the overlay. Recording the real hit
target turns an invisible bug into a one-line diagnosis.

Delivery: batched every 2s, flushed on `beforeunload` via `sendBeacon`, POSTed to
`collector /ingest`.

### 6.4 What `api` records

| kind | Captured |
|---|---|
| `request` | method, route, status, duration, user_id |
| `sql` | statement, params (redacted), duration — via SQLAlchemy event listeners |
| `business` | explicit domain events: `coupon_applied`, `coupon_rejected`, `order_created`, `checkout_failed` |
| `error` | exception type, message, **full traceback with file:line** |
| response shape | top-level keys of the JSON response (truncated) — needed for BUG-003 |

Emission is fire-and-forget onto a background queue so telemetry never blocks or
breaks a request.

### 6.5 Query API (collector)

```
GET /telemetry/trace/{trace_id}
      → merged, time-ordered timeline across web + api

GET /telemetry/session/{session_id}
      → every trace in the session, summarised

GET /telemetry/search?user=&since=&until=&level=&kind=&name=&text=
      → find the session a ticket is talking about

GET /telemetry/bundle/{trace_id}
      → agent-shaped: timeline + extracted stack frames + implicated
        source files + response payload shapes + preceding user actions
```

`/bundle` is the robot's front door. It should return everything needed to form a
hypothesis in a single call.

**Target output** (the thing that makes the whole project work):

```
t_9f3a  12:04:22.118  WEB   click #place-order
t_9f3a  12:04:22.130  WEB   POST /api/checkout → pending
t_9f3a  12:04:22.140  API   request start  user=priya  cart=3
t_9f3a  12:04:22.155  API   SQL  SELECT * FROM coupons WHERE code='SAVE20'
t_9f3a  12:04:22.161  API   coupon_applied  code=SAVE20 uses=4/5
t_9f3a  12:04:22.190  API   SQL  UPDATE coupons SET uses=5 ...
t_9f3a  12:04:22.194  API   ERROR  IntegrityError  checkout.py:94
t_9f3a  12:04:22.201  WEB   POST /api/checkout → 500
t_9f3a  12:04:22.203  WEB   console.error "Unexpected token < in JSON"
t_9f3a  12:04:25.400  WEB   click #place-order        ← retried
t_9f3a  12:04:28.900  WEB   click #place-order        ← retried
```

### 6.6 Testid convention

Every interactive element gets `data-testid`. Kebab-case, stable, never derived from
copy. `data-testid="place-order"`, `data-testid="coupon-input"`,
`data-testid="cart-item-remove-{id}"`.

Two reasons: browser-use navigates more reliably, and the deterministic repro script
it emits doesn't break on a copy change.

---

## 7. Bug catalog

Five bugs. Each chosen to break the robot in a *different* way. Every one has a
manifest in `bugs/BUG-00N.yaml` with the exact answer sheet.

### BUG-001 — Coupon race (TOCTOU)
| | |
|---|---|
| **Flag** | `BUG_COUPON_TOCTOU`, `BUG_CHECKOUT_SWALLOWS_ERROR` |
| **Layer** | Backend, concurrency |
| **Cause** | `checkout.py` reads `coupons.uses`, computes `uses+1`, writes it back — no `SELECT … FOR UPDATE`. Concurrent checkouts both read the same value; the second write violates `CHECK (uses <= max_uses)` |
| **Symptom** | 500 on the second checkout. Frontend swallows it and spins forever |
| **Ticket** | "tried to place my order twice and it just spins, i had SAVE20 applied" |
| **Tests** | Can the robot deliberately induce concurrency? Does it notice the *second*, separate frontend defect? |

### BUG-002 — Invisible click
| | |
|---|---|
| **Flag** | `BUG_PROMO_OVERLAY` |
| **Layer** | Frontend only |
| **Cause** | Promo banner's dismiss layer has `position:fixed` and, below 768px, spans the region containing the Place Order button. Higher `z-index`, transparent |
| **Symptom** | On mobile widths, clicking Place Order does nothing. **Zero backend logs — the request never fires** |
| **Ticket** | "can't order from my phone, the button does nothing" |
| **Tests** | This is the bug that justifies frontend telemetry existing. Backend investigation finds literally nothing. The robot must reproduce at the right viewport |

### BUG-003 — Contract drift
| | |
|---|---|
| **Flag** | `BUG_TOTAL_FIELD_RENAME` |
| **Layer** | Frontend ↔ backend boundary |
| **Cause** | `api` renames `total_cents` → `total` in the order response. `web` still reads `total_cents` → `undefined` → `$NaN` |
| **Symptom** | Order total renders as `$NaN` on the confirmation and history pages |
| **Ticket** | "my order says NaN dollars, did it go through??" |
| **Tests** | Neither side is wrong alone. Requires reading both. Response-shape telemetry (§6.4) is the shortcut |

### BUG-004 — Order leak (IDOR)
| | |
|---|---|
| **Flag** | `BUG_ORDER_IDOR` |
| **Layer** | Backend, authorization |
| **Cause** | `GET /api/orders/{id}` looks up by primary key without `AND user_id = current_user` |
| **Symptom** | A user can view any order by ID |
| **Ticket** | "clicked my order and it showed a jacket I never bought" — innocent wording, no mention of security |
| **Tests** | Does the robot recognise a security issue from a non-security ticket, and escalate severity in the PR? |

### BUG-005 — Not a bug
| | |
|---|---|
| **Flag** | none — this is correct behaviour |
| **Layer** | — |
| **Reality** | `EXPIRED15` genuinely expired. The API correctly returns 400 `coupon_expired`. The UI correctly displays "This coupon has expired" |
| **Ticket** | "your discount codes don't work, EXPIRED15 won't apply, fix your site" |
| **Expected outcome** | Robot reproduces, sees correct behaviour, checks telemetry, confirms the expiry message *was* shown to the customer, and closes as **working as intended with no patch** |
| **Tests** | The most important one. An agent that always produces a patch is worse than useless |

### Stretch (not in v1)
`BUG-006` float tax rounding · `BUG-007` N+1 timeout under load ·
`BUG-008` stale cache after cart edit · `BUG-009` pagination off-by-one

---

## 8. Seed data and Ghost Runs

### 8.1 Base seed

Fixed, deterministic. No randomness.

- **12 products** across 3 categories, prices 1200–24900 cents
- **5 users**, password `password123`
  - `priya@example.com` — author of tickets #1042, #1044; has order history
  - `arjun@example.com` — owns the order that leaks in BUG-004
  - `mei@example.com` — author of the mobile ticket
  - plus two for volume
- **Coupons**
  - `SAVE20` — percent 20, `max_uses=5`, `uses=4` ← primed one redemption from the edge
  - `WELCOME10` — fixed 1000 cents
  - `EXPIRED15` — percent 15, `expires_at` in the past (BUG-005)
- **Orders** — a handful of historical orders per user
- **Tickets** — five, in `supportdesk`, written in customer voice

`SAVE20` seeded at `uses=4` of 5 is deliberate: the race is reachable on the very
next checkout instead of requiring five warm-up runs.

### 8.2 Ghost Runs — the important bit

For the robot to "look up the customer's session from Tuesday", **that session has to
already exist in telemetry before the robot ever runs.**

The wrong way is to hand-write telemetry rows in the seed. They will be subtly
inconsistent with what the app actually produces, and the robot will learn to trust
fiction.

The right way: at seed time, **actually drive the broken app** with a headless script
impersonating the customer, and let it generate authentic telemetry.

```
make reset
  1. drop + recreate both schemas
  2. load base seed
  3. enable the bug flags for the active scenario
  4. GHOST RUNS  ── headless script logs in as priya, adds to cart,
     │              applies SAVE20, fires two concurrent checkouts,
     │              retries three times, gives up and leaves
     └──▶ produces real web + api telemetry, real trace IDs, real stack traces
  5. done — the robot can now investigate a genuine historical session
```

Every ticket gets a ghost run. The telemetry the robot reads is the same telemetry the
app produces live, because it *is* the telemetry the app produced live.

### 8.3 Reset semantics

`make reset` restores **data and code**:

- Data — drop and recreate `shop` + `telemetry`, reseed, re-run ghosts
- Code — `git reset --hard` to the scenario baseline branch, discarding the robot's edits
- Flags — set to the active scenario's manifest

The robot mutates both. Without a one-command reset, iterating on the agent is
miserable.

---

## 9. Bug manifest format

`bugs/BUG-001.yaml` — written *before* the code.

```yaml
id: BUG-001
title: Coupon usage counter race condition
ticket: 1042
layer: backend
class: concurrency
flags:
  BUG_COUPON_TOCTOU: true
  BUG_CHECKOUT_SWALLOWS_ERROR: true
symptom:
  user_visible: "Place Order spins forever, no error shown"
  http: "POST /api/checkout → 500 IntegrityError"
  backend_logs: true
  frontend_logs: true
answer_sheet:
  files:
    - path: api/app/routers/checkout.py
      lines: [88, 102]
  root_cause: >
    Read-modify-write on coupons.uses without row-level locking.
    Concurrent checkouts read the same value; the second UPDATE
    violates CHECK (uses <= max_uses).
  correct_fix: "SELECT ... FOR UPDATE on the coupon row before increment"
  secondary:
    - path: web/app/checkout/page.tsx
      issue: "non-2xx response leaves the submit button in loading state"
repro:
  persona: priya@example.com
  viewport: [1440, 900]
  concurrency: 2
  steps: [login, add_item, apply_coupon SAVE20, checkout ×2 simultaneously]
regression_test: api/tests/test_coupon_race.py
```

The `answer_sheet` block is what makes it possible to say the robot was *right*, not
just plausible.

---

## 10. Acceptance criteria

The store phase is done when all of these are true.

**P0**
- [ ] `docker compose up` brings all six services healthy
- [ ] Log in as priya, add to cart, apply `WELCOME10`, place an order, see it in history
- [ ] `make reset` returns to identical state, verified by a checksum of seeded rows

**P1**
- [ ] Clicking Place Order produces one `trace_id` covering every request it caused
- [ ] `GET /telemetry/trace/{id}` returns the interleaved web + api timeline of §6.5
- [ ] A backend exception appears in the timeline with file and line number
- [ ] A click that fires no network request still appears in telemetry, with the real hit target
- [ ] Killing `api` does not stop `collector` from serving past telemetry

**P2**
- [ ] Each of the 5 bugs toggles at runtime with no rebuild
- [ ] Each bug has a manifest with a filled `answer_sheet`
- [ ] Ghost runs populate a realistic historical session per ticket
- [ ] `supportdesk` lists 5 tickets in believable customer voice
- [ ] With all flags off, the full flow works cleanly and telemetry shows no errors
- [ ] BUG-002 produces **zero** api-side error telemetry, and is still fully diagnosable from web telemetry alone

---

## 11. Layout

```
bugforge/
├── docker-compose.yml
├── Makefile                  # up, down, reset, seed, ghost, logs
├── docs/01-store-spec.md
├── bugs/BUG-00{1..5}.yaml    # the answer sheets
├── api/
│   └── app/
│       ├── main.py  db.py  models.py  flags.py
│       ├── telemetry.py      # contextvar, middleware, SQL listeners, emit()
│       ├── routers/          # auth cart catalog checkout orders debug
│       └── tests/
├── web/
│   ├── app/                  # / product cart checkout orders login
│   └── lib/telemetry.ts      # tracker: fetch wrap, click capture, batching
├── collector/
│   └── app/                  # ingest.py  query.py  models.py
├── supportdesk/
│   └── app/                  # tickets, Jinja templates
└── scripts/
    ├── seed.py
    └── ghosts/               # one script per ticket
```

---

## 12. Open questions

1. **Session replay** — deferred. Structured timeline first. Revisit after P3; if the
   diagnosis quality is already good, replay is pure demo polish.
2. **Ghost run drift** — if a ghost run stops reproducing after a code change, seeding
   silently degrades. Needs an assertion: each ghost must verify its expected symptom
   appeared, and fail loudly otherwise.
3. **Telemetry volume** — ghost runs plus agent runs will accumulate. Probably fine at
   demo scale; add retention if queries slow down.
