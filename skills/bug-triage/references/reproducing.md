# Reproducing

Turning a vague description into a deterministic script that proves the bug exists.

## The two-stage idea

Exploration and proof need opposite properties.

**Exploration** needs flexibility. The ticket says "checkout just spins" — no steps, no
selectors. An LLM driving the browser can look at the page and work it out.

**Proof** needs determinism. "I re-ran it and it worked" only means something if *it*
was the same both times. An LLM re-navigating might take a different route, buy a
different item, and prove nothing.

So exploration is a **compiler**: vague English in, deterministic Playwright out.

```
bf repro explore  ──►  repro.py  ──►  run before  (confirm broken)
   (browser-use)                 ──►  run after   (prove fixed)
                                 ──►  ship in the PR (reviewer runs it)
```

The script is the deliverable. The exploration is scaffolding you throw away.

## Explore

```bash
bf repro explore --ticket 1042 \
  --goal "log in as priya, add an item, apply SAVE20, then place the order twice at the same time" \
  --persona priya@example.com \
  --viewport 1440x900
```

Write the `--goal` the way you would brief a junior engineer: the intent and the
preconditions, not the selectors. Include state that matters ("with a coupon applied",
"with more than 10 items", "as a user who has ordered before").

## The four variables that decide whether you reproduce

Most failed reproductions are one of these being wrong.

| Variable | Gets it wrong when |
|---|---|
| **Viewport** | The bug is width-conditional. Mobile tickets need `--viewport 390x844`. A responsive bug is invisible at desktop width and you will conclude "cannot reproduce" |
| **Persona** | The bug needs account state — order history, a specific locale, an account created before some date. Use the persona the ticket names, not a fresh user |
| **Precondition state** | Coupon usage counts, stock levels, cart contents. Check the telemetry for the state at failure time and set it up |
| **Concurrency / timing** | Races need genuinely simultaneous requests. A step-by-step agent cannot hit them; the emitted script must fire requests in parallel |

For a race, the script must do the parallel part in raw code, not in browser steps:

```python
# in repro.py — two checkouts in flight at once
async with asyncio.TaskGroup() as tg:
    tg.create_task(place_order(ctx_a))
    tg.create_task(place_order(ctx_b))
```

## Symptom checks are the point

A repro script that just performs actions is useless. It must **assert what broken
looks like**, so the same script can be run after the fix and mechanically report
"gone".

Declare checks explicitly:

```python
SYMPTOM_CHECKS = [
    check("checkout_returns_500",   lambda r: any(x.status == 500 for x in r.requests)),
    check("button_stuck_loading",   lambda r: r.page.locator('[data-testid=place-order]')
                                                 .get_attribute("data-loading") == "true"),
    check("no_error_shown",         lambda r: not r.page.locator('[data-testid=checkout-error]')
                                                 .is_visible()),
]
```

Good checks are **specific and observable**. Bad checks are vague ("page looks wrong")
or over-broad ("any console error") — the latter will pass after the fix for unrelated
reasons and tell you nothing.

Aim for 2–4 checks that together describe the customer's experience.

## Run it

```bash
bf repro run .bugforge/runs/1042/repro.py --label before
```

Returns a verdict, and records video, Playwright trace, HAR, and console output. It
also captures the `trace_id`s it generated, so you can pull the telemetry for your own
reproduction and compare it against the customer's:

```bash
bf telemetry bundle <trace_id_from_verdict> --pretty
```

**Your timeline should look like theirs.** If it does not, you reproduced *a* problem,
not *their* problem. That comparison is the strongest confirmation available — use it.

## The retry budget

**Three attempts.** Change **one variable per attempt** — otherwise you learn nothing
from a failure.

A sensible ladder:

1. Exactly what the telemetry showed
2. Vary the most likely wrong variable from the table above
3. Widen: more concurrency, extreme viewport, different persona

Then stop:

```bash
bf report 1042 --escalate
```

Escalation is not failure. It hands a human the timeline, the three variations tried,
what each ruled out, and your best remaining hypothesis. That is a genuinely useful
artifact and it is far better than a confident wrong patch.

## Before you call it reproduced

Ask: **did I reproduce the bug, or just the behaviour?**

The app doing what the customer described is not the same as the app doing something
wrong. An expired coupon being rejected reproduces the customer's experience
perfectly — and is correct behaviour.

If the app behaved correctly, go to step 4 in SKILL.md and close it as working as
intended.
