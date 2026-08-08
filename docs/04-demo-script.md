# Demo script

~8 minutes. Three tickets, in this order, for a reason.

---

## Before you start

```bash
make reset                 # clean data, ghost runs replayed, flags set
bf doctor --pretty         # must be all green before anyone is watching
```

Screen layout:

- **Left**: terminal running `bf`
- **Right top**: the browser (run repro `--headed` so they watch it break)
- **Right bottom**: supportdesk, then Gitea at the end

Have `before.webm` / `after.webm` from a prior run open in a tab. If the live
reproduction flakes, cut to them and keep talking. Never debug live.

---

## Beat 1 — the ticket (30s)

Open supportdesk. Read #1042 aloud, verbatim:

> hey, tried to place my order twice this morning and it just spins forever. i had
> the SAVE20 code applied. now i'm scared i got charged twice? using chrome on my
> macbook.

Say what it does **not** contain: no error, no steps, no timestamp — and a wrong
theory about double-charging.

> "This is what support actually gets. Not a stack trace."

## Beat 2 — the store is real (20s)

Click through the store quickly. Product, cart, checkout. Don't linger.

> "Normal app. Frontend, backend, Postgres. It's instrumented, which is the only
> unusual thing about it."

## Beat 3 — investigate, before touching a browser (90s) ← **the money beat**

```bash
bf telemetry search --user priya@example.com --since 7d --level error --pretty
bf telemetry bundle t_9f3a --pretty
```

Let the timeline sit on screen. Read three lines out loud:

```
12:04:22.161  API   coupon_applied  code=SAVE20 uses=4/5
12:04:22.194  API   ERROR  IntegrityError  checkout.py:94
12:04:25.400  WEB   click #place-order        ← retried
```

> "Left column is the browser. Right column is the server. Same timeline, because
> every click carries an ID that survives into the server logs. That join is the
> whole trick — and it's why this isn't just a stack-trace-to-patch tool."

Point at the retries:

> "She clicked three more times. Nobody told her it failed. That's why the ticket
> says 'spinning' instead of 'error' — and that's a *second* bug we haven't
> mentioned yet."

Then the hypothesis, with a file and a line, before any browser opens.

## Beat 4 — reproduce (90s)

```bash
bf repro run .bugforge/runs/1042/repro.py --label before --headed
```

Let them watch the browser drive itself and hang. Then:

> "That script was written by exploring the app from the ticket's description —
> but what got saved is deterministic. It runs three times: to confirm the bug,
> to prove the fix, and by the reviewer."

Show the verdict: `symptom PRESENT`, both checks ticked.

## Beat 5 — fix and verify (90s)

Show the failing test first — **emphasise the order**:

> "Test first, and it has to fail. A test written after the fix only proves the
> code does what it currently does."

Then the fix, then:

```bash
bf verify --ticket 1042 --pretty
```

```
✓ new_test    FAILED before, PASSED after
✓ full_suite  47 passed, 0 failed
✓ repro       symptom absent
verdict: VERIFIED
```

> "Three checks. The third one — re-running the customer's exact path in a real
> browser — is the one nobody else does. Anyone can generate a patch now. Proving
> the symptom is gone is the hard part."

## Beat 6 — the PR (45s)

```bash
bf pr open 1042
```

Open it in Gitea. Scroll: root cause with file and line, the evidence timeline,
before/after video, and the secondary finding **named but not fixed**.

> "It found the swallowed-500 bug too. It didn't quietly fix it — it filed it.
> A PR that fixes three unrelated things can't be reviewed."

---

## Beat 7 — ticket 1043, the invisible bug (75s)

> "Different kind of bug."

Read #1043: can't order from her phone, button does nothing.

```bash
bf telemetry bundle <trace> --pretty
```

Point at the silence:

> "Click. Then nothing. No request, no error, no server logs. A backend-only tool
> is blind here."

Then the click event:

```json
"intended": {"testid": "place-order"},
"actual":   {"class": "promo-scrim", "zIndex": 9999, "position": "fixed"},
"listener_ran": false
```

> "An invisible overlay is on top of the button, below 768 pixels. That's the whole
> diagnosis, and it's only there because the frontend records what was *actually*
> hit, not what was aimed at."

Reproduce at `--viewport 390x844`. Watch it fail on a phone-sized window.

---

## Beat 8 — ticket 1046, the punchline (60s)

> "Last one."

Read #1046: *"your discount codes don't work, EXPIRED15 won't apply, fix your site."*

Run it. The agent reproduces, sees a clean `400 coupon_expired`, sees the UI
correctly showing "This coupon has expired" — and stops.

```bash
bf report 1046 --working-as-intended
```

> "No patch. The code was right, the customer was wrong. It wrote her a reply
> instead, and suggested the expiry message should say *when* it expired.
>
> This is the one I care about. An agent that always produces a patch will
> confidently 'fix' working code, and you'd never know. Refusing is a feature."

---

## Beat 9 — the reveal (30s)

Open `bugs/BUG-001.yaml`.

```yaml
answer_sheet:
  files: [api/app/routers/checkout.py]
  root_cause: >
    Read-modify-write on coupons.uses without row-level locking.
```

> "We planted every bug. This file was written before the code was. So we're not
> asking whether the answer sounds plausible — we know what the answer was, and we
> can tell you it got it right.
>
> That's the actual project. The store isn't a demo of the agent. It's the test
> track — you can't grade a bug-fixing agent on bugs you didn't plant yourself."

---

## Closing line

> "One more thing: none of this is tied to Claude Code. The skill assumes you can
> read a file, write a file, and run a shell command. Everything else is a CLI.
> Swap four config lines and it triages real Sentry errors from real Linear tickets
> into real GitHub PRs."

---

## If you only have 3 minutes

Beat 1 → Beat 3 (the timeline) → Beat 8 (the refusal) → Beat 9 (the answer sheet).

Skip the fix entirely. The timeline and the refusal are what people remember.

---

## Failure drills

| If | Do |
|---|---|
| The race doesn't fire | `make reset` re-primes SAVE20 at 4/5. Have a recorded run ready |
| The browser hangs | Cut to `before.webm`. Keep narrating; don't debug on camera |
| `bf doctor` is red mid-demo | Stop. Fix off-camera. Never present against a half-up stack |
| Someone asks "did it really find that?" | Open `bugs/BUG-00N.yaml`. That's what the answer sheet is for |

## Questions you will get

**"Couldn't an LLM just guess this from the stack trace?"**
For 1042, maybe. For 1043 there is no stack trace. That's why it's in the demo.

**"What if it fixes the wrong thing?"**
Three-check verification, and it can't open a PR without passing — `bf pr open`
refuses on an unverified run. Budgets are hard: 3 repro attempts, 2 fix attempts,
then it escalates instead of guessing.

**"Does this work on a real codebase?"**
The loop does. The prerequisite is trace correlation between frontend and backend.
Without it you're back to reading stack traces. That's the capability worth adding
before adopting this — see `references/adapters.md`.
