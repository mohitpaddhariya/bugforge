# Build Status — P0/P1/P2 acceptance

Adversarial verification of every criterion in [`01-store-spec.md` §10](01-store-spec.md).
Everything below was exercised against the running stack — real Chromium, real
`curl`, real `docker compose kill`, real `psql`. Nothing is asserted from reading
code alone.

**Verdict: all 14 criteria PASS.** Four of them only pass *because of fixes made
during this pass* — they were failing when verification started. Those are called
out in the table and detailed in [§2](#2-what-was-broken-and-fixed).

Verified on: 2026-08-08. Stack: six containers, `docker compose up -d --wait`
green from cold in 26s.

---

## 1. Acceptance criteria

### P0

| ✓ | Criterion | Result | Evidence |
|---|---|---|---|
| ☑ | `docker compose up` brings all six services healthy | **PASS** | `docker compose down --remove-orphans` then `up -d --wait` returned clean in **25.9s**. All six report `healthy`: web, api, collector, db, supportdesk, gitea. |
| ☑ | Log in as priya, add to cart, apply `WELCOME10`, place an order, see it in history | **PASS** | Real Chromium at 1440×900. Login → `/product/1` → add-to-cart → `WELCOME10` applied → Place Order → redirected to `/orders/15` → order appears in `/orders` as `Order #15 · placed · WELCOME10 · $193.32`. Zero page errors, zero failed requests. |
| ☑ | `make reset` returns to identical state, verified by a checksum of seeded rows | **PASS** | Seed checksum `9e8b28662162d382e0a853b4b998678d684e85a7c7ea07a291728588507c2875` — identical across **4 full `make reset` runs and 3 standalone `seed.py` runs**. Ghost runs re-confirmed 31/31 on every one. |

### P1

| ✓ | Criterion | Result | Evidence |
|---|---|---|---|
| ☑ | Clicking Place Order produces one `trace_id` covering every request it caused | **PASS** *(was failing — fixed)* | Network-level capture of `X-Trace-Id` on every outgoing request. One click on `#place-order` → **`t_4fe12d2a` on all three requests it caused**: `POST /api/checkout`, `GET /api/me`, `GET /api/orders/15`. The one request in the same window that it did *not* cause — the harness flag poll — now carries no trace id at all. |
| ☑ | `GET /telemetry/trace/{id}` returns the interleaved web + api timeline of §6.5 | **PASS** *(was degraded — fixed)* | See [§3](#3-the-target-timeline-actual-output). Web and api lines interleave by timestamp; `WEB POST /api/checkout → 201` sits 3ms after `API request POST /api/checkout → 201 user=1`. Matches the §6.5 target shape. |
| ☑ | A backend exception appears in the timeline with file and line number | **PASS** | `t_2f2cb96e … API ERROR CheckViolation new row for relation "coupons" violates check constraint "ck_coupons_uses_within_max" … **checkout.py:71**`. Cross-checked against source: `api/app/routers/checkout.py:71` is the `db.execute(update(Coupon)…uses=Coupon.uses + 1)` inside `_redeem_coupon` — the exact BUG-001 site. `stack_frames[]` marks it `app: true, innermost: true`. |
| ☑ | A click that fires no network request still appears in telemetry, with the real hit target | **PASS** *(was misreported — fixed)* | BUG-002 at 390×844. The trace is **one event**: `WEB click #place-order (hit #promo-dismiss-layer)`. Session headline reads `click #place-order → no request fired`. `attrs.hit_element` names the layer with `z_index=9998, position=fixed`; `attrs.obscured_interactive_element` names `place-order` underneath. |
| ☑ | Killing `api` does not stop `collector` from serving past telemetry | **PASS** | `docker compose kill api` (SIGKILL), run twice on two separate stacks. With api at `000`: collector `/health` 200, `/telemetry/trace/{id}` 200 serving 16 events, `/telemetry/bundle/{id}` 200, `/telemetry/session/{id}` 200, `/telemetry/search?user=priya@example.com` 200. **`POST /ingest` still returned 202 and the new event was immediately readable back.** supportdesk also stayed at 200. |

### P2

| ✓ | Criterion | Result | Evidence |
|---|---|---|---|
| ☑ | Each of the 5 bugs toggles at runtime with no rebuild | **PASS** (see caveat) | Live page, no reload: flipped `BUG_PROMO_OVERLAY` on → the dismiss layer moved from `top:0px h:52` to `top:748px h:96` and `document.elementFromPoint()` at the button's centre returned the **overlay**; flipped off → returned the button again. **0 page navigations, 0 rebuilds, 0 restarts.** Backend flags proven by response diff: BUG-003 `total_cents`↔`total`; BUG-004 `404`↔`200` on another user's order; BUG-001 `[201,201]`↔`[201,500]` on concurrent checkout. `BUG_CHECKOUT_SWALLOWS_ERROR` reaches the live page through the same `useFlags` hook. **Caveat:** there are 5 flags covering 4 bugs — BUG-005 has no flag *by design*, because it is correct behaviour. |
| ☑ | Each bug has a manifest with a filled `answer_sheet` | **PASS** (with deviation) | All 5 of `bugs/BUG-00{1..5}.yaml` carry `answer_sheet` with `files`, `root_cause`, `correct_fix`, `incorrect_fixes`. **Deviation from §9:** `files[].lines` is `null` everywhere; the manifests locate code by `anchor` string instead. All 4 primary anchors resolve uniquely in the current source (verified by `grep -c`). See [§4](#4-known-broken--gaps). |
| ☑ | Ghost runs populate a realistic historical session per ticket | **PASS** | 1042 7/7 · 1043 7/7 · 1044 6/6 · 1045 6/6 · 1046 5/5 — **31/31 confirmed on four consecutive resets**. Each ghost asserts its own symptom in telemetry and fails loudly otherwise (closes spec §12 open question 2). Sessions are real: `/telemetry/search?user=mei@example.com&since=7d` finds `s_a637c435`, 54 events, viewport `[390, 844]`. |
| ☑ | `supportdesk` lists 5 tickets in believable customer voice | **PASS** | 5 tickets: #1042 "order wont go thru?? charged twice??", #1043 "cant order on my phone", #1044 "my order says NaN dollars??", #1045 "wrong order showing in my account", #1046 "your discount codes dont work". #1045 carries no hint that it is a security issue, as §7 requires. |
| ☑ | With all flags off, the full flow works cleanly and telemetry shows no errors | **PASS** | Full browser flow on a freshly reset stack. **0 events at `level='error'` and 0 at `kind='error'`** across the whole session. Bundle verdict: `clean`. The only non-info rows are 4 × `GET /api/me → 401` at `warn` — the app asking "am I logged in?" on the login page and being correctly told no. |
| ☑ | BUG-002 produces **zero** api-side error telemetry, and is still fully diagnosable from web telemetry alone | **PASS** *(diagnosis was being buried — fixed)* | `SELECT count(*) … WHERE session_id=… AND source='api' AND (kind='error' OR level='error')` → **0**. Also 0 events anywhere touching `/api/checkout` — the request genuinely never fired. Yet `/telemetry/bundle` returns verdict `frontend-only`, signals `['click-produced-no-request', 'click-hit-wrong-element', 'viewport-width-390']`, and the prose: *"The click landed on #promo-dismiss-layer (z_index=9998, position=fixed), not on the intended element — something is overlaying the control. The session's viewport was 390px wide — reproduce at that width."* Same result on the ghost session and on a live browser session. |

---

## 2. What was broken, and fixed

Seven defects were found and fixed during verification. Four of them were
directly load-bearing for a P1/P2 criterion.

### 2.1 The browser stamped the harness control plane, and that broke trace scoping
`web/lib/telemetry.ts`

Spec §5 says `/api/debug/*` is excluded from telemetry "so the robot's own setup
calls don't pollute the timeline it's reading". `api` honoured that. **The browser
did not.** `useFlags` polls `GET /api/debug/flags` every 5s; the tracker treated it
as an ordinary API call, stamped it with `X-Trace-Id`, recorded a `fetch` event for
it — and called `traceIdForRequest()`, which *extends the interaction window*.

The window is 5s. The poll interval is 5s. **The interaction window therefore never
expired.** Observed live: a single page-load trace `t_5186ba72` stayed open across
five seconds of unrelated polls and swallowed everything in between.

The damage landed hardest exactly where it mattered most. BUG-002's whole
diagnosis is *"the click fired no request"*, and the session summary read:

```
t_bf9fc8d1 | click #place-order → 2 request(s) ok     ← before
t_93bbedbc | click #place-order → no request fired    ← after
```

A robot scanning session headlines for the failing interaction would have walked
straight past it. The trace itself carried two phantom `WEB GET` lines that no
user action produced.

**Fix:** the tracker now passes `/api/debug/*` through untouched — no stamp, no
event, no window extension. Same treatment for Next's dev-mode `hot-update` polls,
which were joining traces as pure noise. RSC navigation fetches under `/_next/` are
deliberately *not* excluded; those are real navigations.

### 2.2 Web `fetch` lines rendered the page route, not the request path
`collector/app/query.py::_http_route`

The web tracker stamps `attrs.route` with the *page* the call was made from.
`_http_route` read `route` first, so three different calls rendered identically:

```
WEB   GET  /orders/16 → 200 (10ms)     ← was actually GET /api/me
WEB   GET  /orders/16 → 200 (11ms)     ← was actually GET /api/orders/16
WEB   GET  /orders/16 → 200 (11ms)     ← was actually GET /api/debug/flags
```

§6.5's target output shows `WEB POST /api/checkout`. **Fix:** for web `fetch`
events, prefer `attrs.path` (the request path). Now reads `WEB POST /api/checkout → 201`.

### 2.3 JavaScript stack frames reported the column as the line
`collector/app/query.py::_frames_from_text`

`_JS_FRAME_RE`'s URL alternative allows colons, so it swallowed the line number:
`webpack-internal:///./lib/api.ts:88:15` parsed as file `web/lib/api.ts:88`, line
`15`. `implicated_files` therefore listed **paths no one can open**:

```
('web/lib/api.ts:88', [15])            ← before
('web/lib/api.ts', [88])               ← after
('web/app/checkout/page.tsx:99', [21]) ← before
('web/app/checkout/page.tsx', [99])    ← after
```

**Fix:** when the `col` group is absent but the captured path ends in `:<digits>`,
shift the numbers back one place. Regression-checked against three frame shapes.

### 2.4 "Start reading at" sent a backend error to a frontend file
`collector/app/query.py::build_summary`

For the BUG-001 trace — verdict `backend-error`, exception `CheckViolation` at
`checkout.py:71` — the bundle's closing instruction was:

> Start reading at **web/app/checkout/page.tsx:99:21** in placeOrder

That is the browser correctly reporting a 500 it did nothing to cause. The logic
took the *last* app frame in the trace, and the browser's own error event is always
last. **Fix:** when the verdict is `backend-error`, prefer api frames; and prefer
the *first* innermost frame (the original exception) over later ones (the
propagation path — router re-raise, middleware re-raise). Now reads:

> Start reading at **api/app/routers/checkout.py:71** in `_redeem_coupon`

`StackFrame` gained a `source` field so this is decidable.

### 2.5 The reference "correct fix" for BUG-001 did not actually fix the race
`api/app/routers/checkout.py::_redeem_coupon`

This is the one that matters most for P3. With `BUG_COUPON_TOCTOU` **off** — the
healthy path, the implementation the robot will be graded against — two concurrent
checkouts produced:

```
statuses [201, 201]        two orders, #12 and #13
both coupon_code=SAVE20, both discount_cents=3780
SAVE20 uses: 4 → 5         the counter moved ONCE
```

The coupon was redeemed twice and recorded once. The lock was taken, but
`select(Coupon)…with_for_update()` returns the instance already in the Session's
identity map and **discards the columns the locking SELECT just fetched**. Both
transactions computed `4 + 1 = 5` and both wrote 5. Postgres serialised the writes;
the `CHECK (uses <= max_uses)` constraint never fired, so the over-redemption was
completely silent — the exact failure mode the constraint exists to make visible.

**Fix:** `.execution_options(populate_existing=True)` on the locking read. Verified
after the fix, same test:

```
statuses [201, 400]        one order, #12
loser: 400 coupon_exhausted "This coupon has reached its usage limit."
SAVE20 uses: 4 → 5
```

which is exactly what `checkout.py`'s own docstring and `BUG-001.yaml`'s
`correct_fix` promise.

### 2.6 & 2.7
`collector/app/schemas.py` — `StackFrame.source` added (supports 2.4).
`web/lib/telemetry.ts` — dev-server HMR polls excluded (supports 2.1).

**Files touched:** `web/lib/telemetry.ts`, `collector/app/query.py`,
`collector/app/schemas.py`, `api/app/routers/checkout.py`.
`npx tsc --noEmit` clean · `bf doctor` all adapters green · 31/31 ghost checks
re-confirmed after every change.

---

## 3. The target timeline, actual output

Spec §6.5 defines the output that "makes the whole project work". This is the real
thing, copied from `GET /telemetry/trace/{id}` (SQL lines elided for width):

**A clean checkout, all flags off** — one click, one trace, both sides interleaved:

```
t_4fe12  08:59:13.443  WEB   click  #place-order
t_4fe12  08:59:13.451  API   coupon_applied  code=WELCOME10 uses=1 stage=checkout value=1000
t_4fe12  08:59:13.455  API   order_created  order_id=15 tax_cents=1432 item_count=1 coupon_code=WELCOME10
t_4fe12  08:59:13.456  API   request  POST /api/checkout → 201 (19ms) user=1
t_4fe12  08:59:13.458  WEB   POST  /api/checkout → 201 (25ms)
t_4fe12  08:59:13.525  WEB   GET  /orders/15 → 200 (64ms)
t_4fe12  08:59:13.539  WEB   nav  /checkout → /orders/15
t_4fe12  08:59:13.548  API   request  GET /api/me → 200 (8ms) user=1
t_4fe12  08:59:13.550  API   request  GET /api/orders/{order_id} → 200 (8ms) user=1
t_4fe12  08:59:13.552  WEB   GET  /api/orders/15 → 200 (12ms)
```

**BUG-001, the coupon race** — with the exception located to a line:

```
t_2f2cb  08:45:52.319  WEB   click  #place-order
t_2f2cb  08:45:52.342  API   coupon_applied  code=SAVE20 uses=4 stage=checkout value=20
t_2f2cb  08:45:52.370  API   ERROR  CheckViolation  … "ck_coupons_uses_within_max" …  checkout.py:71
t_2f2cb  08:45:52.372  API   ERROR  IntegrityError  coupon_over_redeemed
t_2f2cb  08:45:52.376  API   request  POST /api/checkout → 500 (47ms) user=1
t_2f2cb  08:45:52.381  WEB   POST  /api/checkout → 500 (58ms)
t_2f2cb  08:45:52.782  WEB   ERROR  ApiError  HTTP 500 from POST /api/checkout  page.tsx:99
```

**BUG-002, the invisible click** — the entire trace, and it is enough:

```
t_93bbe  08:50:33.541  WEB   click  #place-order  (hit #promo-dismiss-layer)
```

---

## 4. Known broken / gaps

Nothing here fails a P0–P2 criterion. Everything here will bite in P3.

### Blockers for P3

**`make reset` does not restore code.** Spec §8.3 requires the reset to restore
*data and code* — `git reset --hard` to the scenario baseline, discarding the
robot's edits. It does not. `scripts/reset.py:28` explicitly disclaims it ("the
Makefile owns that"); the Makefile's `reset` target runs only `schemas → seed →
ghost`. Each side points at the other. Worse: **the repository has no commits at
all** — every file is untracked — so there is no baseline for `git reset --hard` to
target even if it were wired. The robot mutates code; without this, every P3
iteration starts from whatever the last one left behind.

**No regression tests exist, and no harness runs them.** All five manifests declare
a `regression_test` with `must_fail_before_fix: true`
(`api/tests/test_coupon_race.py`, `web/tests/checkout-overlay.spec.ts`, …).
`api/tests/` contains one empty `.gitkeep`; `web/tests/` does not exist. There is no
pytest config, no Playwright config, and no `make test`. The robot is meant to write
these tests, but "must fail before the fix" cannot be *checked* without a runner
that can execute a test against the pre-fix code.

**The answer sheet cannot be graded by line number.** §9 shows
`lines: [88, 102]`. Every manifest has `lines: null` and locates code by `anchor`
string instead. Anchors are more robust to edits and all four primary anchors
currently resolve uniquely — but P3's scoring logic has to be written against
anchors, not line ranges, and it must handle the case where the robot's own patch
*removes the anchor it was supposed to find*.

### Sharp edges

**Ghost session ids change on every reset.** The seed checksum is stable, but ghost
telemetry is regenerated, so `s_a637c435` becomes something else after the next
`make reset`. Any P3 artefact that pins a session id — a saved repro, a cached
investigation, a fixture — breaks silently. Pin by ticket number and re-resolve
through `/telemetry/search`.

**`api/app/telemetry.py:717` still appears in `implicated_files`.** That is the
middleware's `raw_response = await call_next(request)` — the re-raise site, not the
fault site. It is correctly *not* marked `innermost`, and "start reading at" now
names `checkout.py:71`, so this is cosmetic noise in a list, not a wrong answer.

**One of BUG-001's three error events carries no location.** The router's
`IntegrityError coupon_over_redeemed` re-raise is a business-level translation with
no traceback attached. The other two both carry `checkout.py:71`, so the trace is
still fully located.

**Pre-auth `GET /api/me → 401` produces 4 `warn` rows in every clean session.** The
app asking whether anyone is logged in, and being told no. Correct behaviour, but a
P3 heuristic that treats `level >= warn` as "something went wrong" will fire on
every single session including the clean ones. Filter on `level='error'`, or on
`kind='error'`.

**BUG-005 has no flag.** By design — it is correct behaviour. Any P3 scenario
loader that iterates "the five bugs" and expects to set a flag for each will trip
over it. `bugs/BUG-005.yaml` has `flags: {}` and `forbidden_changes`.

---

## 5. Next steps for P3 — the robot

In dependency order. The first two are prerequisites for measuring anything.

### 5.1 Make the reset total (blocker)

1. `git init` is already done but nothing is committed. Commit the current tree and
   tag it `baseline`. Every scenario branches from there.
2. Add a `reset-code` target to the Makefile: `git reset --hard <baseline>` +
   `git clean -fd` restricted to `api/ web/ collector/ supportdesk/ scripts/`
   (never `agent/`, never `docs/`, never `bugs/` — those are the harness, not the
   subject).
3. Wire `reset: reset-code schemas seed ghost` and delete the disclaimer in
   `scripts/reset.py:28`.
4. Assert idempotence: `make reset && git status --porcelain` must be empty.

### 5.2 Stand up the test harness (blocker)

1. `api/pytest.ini` + a `conftest.py` that gives a test a transactional session and
   a flag-setting fixture. `make test-api` runs it inside the api container.
2. `web/playwright.config.ts` pointed at `http://web:3000`, plus `make test-web`.
   The browsers are already installed in `agent/.venv`.
3. `make test` = both. Exit code is the P3 grade signal.
4. Write **one** reference regression test per bug, matching the paths the
   manifests already declare, and verify each one fails with its flag on and passes
   with it off. That is the `must_fail_before_fix` contract, and it also proves the
   harness works before the robot depends on it.

### 5.3 Build the scoring pass

The manifests are the answer sheet; nothing reads them yet.

1. `bf grade <ticket>` — load `bugs/BUG-00N.yaml`, diff the working tree against
   `baseline`, and score: did the patch touch `answer_sheet.files[].path` where
   `role: primary`? Did it touch anything under `forbidden_changes` or a
   `role: victim` file (BUG-002 explicitly says *do not patch* `checkout/page.tsx`)?
2. Match by **anchor**, not line number — and handle the anchor being deleted by
   the patch, which is the *correct* outcome for BUG-003 and BUG-004.
3. Score `incorrect_fixes` as an explicit negative. A patch that matches one of
   those is worse than no patch.
4. **BUG-005 scores inverted.** The pass condition is *zero* diff plus a written
   "working as intended" conclusion. This is the most important single number in
   the whole project — an agent that always produces a patch is worse than useless.

### 5.4 Point the robot at `/bundle` first

`/telemetry/bundle/{trace_id}` now returns, in one call, everything needed to form
a hypothesis: `verdict`, `signals`, plain-English `summary`, `rendered` timeline,
`stack_frames` with the innermost app frame flagged, `implicated_files` with real
openable paths, `response_shapes`, and `preceding_actions`.

The investigation loop should be: ticket → `/telemetry/search?user=&since=` →
pick the session → `/telemetry/session/{id}` → **read the headlines**, find the one
that diverges → `/telemetry/bundle/{trace}` → hypothesis. The headlines are now
trustworthy enough to route on (§2.1); before this pass they were not.

Route on `verdict` before anything else:

| verdict | what it means for the robot |
|---|---|
| `backend-error` | Read `implicated_files`. Walk sql + business events backwards from the exception. |
| `frontend-only` | **Do not investigate the backend.** Read the click event's `hit_element` vs `obscured_interactive_element`, and reproduce at `session_meta.viewport`. |
| `contract-drift` | Compare `response_shapes` against what the client reads. Neither side is wrong alone. |
| `rejected-by-design` | Candidate BUG-005. Confirm the UI showed the rejection, then close with no patch. |
| `clean` | Nothing failed. Check the customer's expectation before assuming a defect. |

### 5.5 Close the loop with a repro script

Every interactive element already has a stable kebab-case `data-testid`, and each
manifest carries a `repro` block with persona, viewport and steps. The robot should
emit a runnable Playwright script that drives the repro at the recorded viewport
and asserts the symptom — then re-run it after the patch. **BUG-002 needs 390px to
reproduce at all**, so viewport must come from `session_meta`, never from a default.

### 5.6 Watch for these while building P3

- Don't reintroduce the trace-window leak (§2.1). Any new browser-side polling must
  be excluded from the tracker, or traces stop meaning "one user interaction".
- The healthy coupon path is now correct (§2.5) but has no test. Write
  `test_coupon_race.py` so it stays correct.
- Two concurrent checkouts on one cart still both succeed when the coupon has
  headroom — cart conversion is not guarded. Out of scope for the bug catalog, but
  it will surface if P3 stress-tests checkout.
