# Investigating

Reading recorded telemetry to form a hypothesis, before touching a browser.

## Why this comes first

The ticket says "it spins." The telemetry says `IntegrityError at checkout.py:94` in
that customer's actual session. Starting from the recording means reproduction becomes
a *confirmation* rather than a search — and it makes hard-to-trigger bugs (races,
specific cart states, one device) tractable, because the recording tells you the
conditions.

## Finding the session

The ticket gives you a person and a rough time. That is enough.

```bash
bf telemetry search --user priya@example.com --since 7d --level error --pretty
```

If that returns nothing, widen in this order — do not jump straight to giving up:

1. drop `--level error` (frontend-only bugs produce **no errors at all**)
2. widen `--since`
3. search by symptom text: `--text checkout`, `--text coupon`
4. search by kind: `--kind click` to see what they were doing at all

Then open the session:

```bash
bf telemetry session s_4b2e11a0 --pretty
```

You get every interaction in order. Look for the one where behaviour diverges from
intent — usually a trace with an error, or a trace where the user repeated themselves.

## Reading the bundle

```bash
bf telemetry bundle t_9f3a --pretty
```

This is the front door. One call gives you the merged web+api timeline, extracted
stack frames with file and line, the implicated source files, response payload shapes,
and the actions preceding the failure.

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

### What to actually look at

**The gap between intent and effect.** A click at 22.118, a 500 at 22.201, and then
two more clicks. The retries tell you the user was given no feedback — that is a
second, separate defect, and it is why the ticket says "spins" instead of "error".

**The SQL immediately before an error.** Races, missing constraints, and N+1s are all
visible in the query sequence, not in the traceback.

**Business events.** `coupon_applied uses=4/5` tells you the state of the world at
failure time. That is usually the reproduction precondition you need.

**What is missing.** Absence is evidence — see below.

## Two failure modes that will burn you

### 1. A click with no network request

If the timeline shows `click` and then **nothing**, the bug is entirely in the
frontend. There will be no backend logs, no stack trace, no error. Searching harder on
the server side is wasted effort.

Look at the click event's `attrs`. It records the element **actually hit**, not the
element intended:

```json
{ "intended": {"testid": "place-order", "tag": "button"},
  "actual":   {"tag": "div", "class": "promo-scrim", "zIndex": 9999,
               "position": "fixed"},
  "listener_ran": false }
```

That is the whole diagnosis. An invisible overlay is on top of the button. Check
`viewport` in the session metadata — this class of bug is usually width-conditional.

### 2. The stack frame is not the cause

The innermost frame is where it *surfaced*, not where it *went wrong*. An
`IntegrityError` on an `UPDATE` means the invariant was violated earlier — by the
unlocked read a few lines above.

Ask: *what made this line's precondition false?* Walk backwards through the SQL and
business events until you find the decision that was wrong. Then read that code:

```bash
bf code show api/app/routers/checkout.py --anchor "_redeem_coupon" --context 30
```

## Writing the hypothesis

One sentence. It must name a file and a line, and describe a mechanism.

Good:

> `checkout.py:94` — the coupon usage counter is read and written back without a row
> lock, so concurrent checkouts both read `uses=4` and the second `UPDATE` violates
> `CHECK (uses <= max_uses)`.

Not good:

> There is a race condition in checkout.

> The checkout endpoint throws an IntegrityError.

The first is a category, the second is a restatement of the symptom. Neither tells you
what to reproduce or what to change.

Save it before moving on:

```bash
bf state set 1042 --phase investigated --key hypothesis --value "..."
```

## When telemetry shows nothing

Say so plainly. Do not manufacture a hypothesis to fill the silence — a confident
wrong hypothesis is worse than an honest blank, because reproduction will then be
aimed in the wrong direction.

Go to reproduction with a wide net and let the browser tell you where to look.
