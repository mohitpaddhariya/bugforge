# Writing the PR

The reviewer was not on the investigation. Everything they need is in this document.

## The shape

```markdown
## fix: <what changed>, not <what broke> (#1042)

**Root cause**
`api/app/routers/checkout.py:88-102` reads `coupons.uses`, computes `uses + 1`, and
writes it back without a row lock. Two concurrent checkouts both read `uses=4`; the
second UPDATE violates `CHECK (uses <= max_uses)` and returns 500.

**Customer impact**
Any two checkouts overlapping on the same coupon. The second fails, and because the
frontend does not handle a non-2xx response, the button spins with no error shown —
which is why the ticket says "spinning" rather than "error".

**Evidence**
Customer session `s_4b2e11a0`, trace `t_9f3a`:

    12:04:22.161  API   coupon_applied  code=SAVE20 uses=4/5
    12:04:22.190  API   SQL  UPDATE coupons SET uses=5 ...
    12:04:22.194  API   ERROR  IntegrityError  checkout.py:94
    12:04:22.201  WEB   POST /api/checkout → 500
    12:04:25.400  WEB   click #place-order        ← retried
    12:04:28.900  WEB   click #place-order        ← retried

**The fix**
`SELECT ... FOR UPDATE` on the coupon row before incrementing, inside the checkout
transaction. Serialises redemption; the loser now gets a clean 409.

**Verification**
- `test_coupon_race` — FAILED before, PASSES after
- Full suite: 47 passed, 0 failed
- Browser reproduction re-run: symptom absent (before.webm / after.webm)
- Reviewers can run it: `bf repro run .bugforge/runs/1042/repro.py --label check`

**Also found — not fixed here**
`web/app/checkout/page.tsx` leaves the submit button in a loading state on any non-2xx
response, so every checkout failure looks like a hang. Separate defect, wider blast
radius. Filed as #1047.

Closes #1042
```

## Rules

**Root cause first, in one sentence, with a file and a line.** A reviewer should be
able to decide whether to trust the rest of the PR from that sentence alone.

**Distinguish the cause from the surface.** "Throws IntegrityError" is the surface.
"Read-modify-write without a lock" is the cause. If your sentence restates the error
message, you have not found the cause yet.

**Quote the evidence, do not describe it.** Paste the timeline lines. Six lines of
real telemetry are worth a paragraph of narration.

**Explain the symptom-to-cause link.** The customer said "spinning"; the code said
"500". Say why those are the same event. That gap is usually where reviewer doubt
lives.

**State verification as facts with numbers.** "Tests pass" is unverifiable.
"47 passed, 0 failed; test_coupon_race FAILED before, PASSES after" is checkable.

**Make it reproducible by the reviewer.** Ship the repro script and the command to run
it. This is the single thing that most raises trust in an agent-authored PR.

## Severity

If the bug has a security dimension, say so explicitly and prominently — even when the
ticket did not. A customer writing "I saw an order I never placed" has reported an
authorization flaw without knowing it. The PR title and the first line must reflect
that, because it changes who reviews it and how fast it ships.

Do not soften it, and do not bury it under the reproduction steps.

## Secondary findings

Investigations surface things the ticket did not ask about. Handle them like this:

- **Part of the customer's symptom** → fix it here, and say why it is in scope
- **Anything else** → name it, describe it, file it separately, do **not** fix it

A PR that fixes three unrelated things cannot be reviewed properly, and the reviewer
loses the ability to reason about the blast radius of the change they care about.

## Not-a-bug tickets

There is no PR. Write the customer reply instead:

```markdown
## #1046 — working as intended, no code change

**What happened**
`EXPIRED15` expired on 2026-07-02. The API correctly rejected it with
`400 coupon_expired`, and the UI correctly displayed "This coupon has expired."

**Evidence**
Session `s_77c1e2`: three apply attempts, three clean 400s, expiry message rendered
each time. No errors anywhere in the trace.

**Reproduced**
Yes — behaviour is correct at every step.

**Suggested reply**
That code expired on 2 July. Here is a current one: WELCOME10.

**Worth considering**
The expiry message does not say *when* it expired, which is probably why this was read
as a malfunction. Small copy change, filed as #1048.
```

Closing a ticket without a patch is a real outcome and should look like a finished
piece of work, not a shrug.

## Escalations

Same standard. A "cannot reproduce" report must contain the customer's timeline, each
variation attempted and what it ruled out, and the best remaining hypothesis with what
would confirm it. The next person should start from where you stopped, not from the
beginning.
