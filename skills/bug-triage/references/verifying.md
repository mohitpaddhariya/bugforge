# Verifying

What counts as proof that the fix worked.

## Why this step carries the project

Generating a plausible patch is easy and getting easier. Proving the customer's
symptom is gone, through the same path the customer took, without breaking anything
else — that is the part that is actually hard, and the part reviewers cannot do for
you.

If you skip it, you have produced a suggestion. If you do it, you have produced a fix.

## The three checks

```bash
bf verify --ticket 1042
```

### a) The new test passes

It failed before the patch and passes after. Both halves matter. A test that never
failed proves only that the code does what it currently does.

If you did not run it before the fix, you cannot claim this. Go back and run it on the
pre-fix code.

### b) The full suite still passes

Every existing test, not just the ones near your change. This is the "did I break
something else" check, and it is the one most often skipped under time pressure.

**Never edit a test to make it pass.** If an existing test breaks, one of two things
is true, and both mean stop:

- your fix is wrong, or
- that test encoded the buggy behaviour as correct

The second is real and worth reporting — but it is a conversation with the reviewer,
not something to quietly rewrite.

### c) The browser reproduction re-runs clean

```bash
bf repro run .bugforge/runs/1042/repro.py --label after
```

The **same script**, unmodified, from the confirmation step. All symptom checks now
report absent.

If you had to change the script to make it pass, the verification is void. Changing
the test to match the code is the oldest mistake in the book, and it looks identical
to success.

## Reading the verify output

```json
{
  "ticket": "1042",
  "checks": {
    "new_test":   {"passed": true,  "before": "FAILED", "after": "PASSED"},
    "full_suite": {"passed": true,  "total": 47, "failed": 0},
    "repro":      {"passed": true,  "symptom_before": true, "symptom_after": false}
  },
  "verdict": "VERIFIED"
}
```

`VERIFIED` requires all three. Anything else means you are not done — do not open the
PR.

## When verification fails

Diagnose before you re-patch. The failure mode tells you where the mistake is:

| Failure | What it means |
|---|---|
| New test still fails | The fix does not address the cause you named |
| New test passes, repro still shows the symptom | You fixed a code path the user does not take, or the symptom has a second cause |
| Suite broke | Your change has a blast radius you did not anticipate |
| Repro passes but the timeline looks different from the customer's | You may have masked the symptom rather than removed the cause |

Budget: **2 fix attempts.** If the second fails, escalate with what you learned. Three
consecutive patches at the same site is how a wrong hypothesis becomes a mess.

## Comparing timelines

The strongest available evidence. Pull the telemetry for your post-fix run and compare
it against the customer's original:

```bash
bf telemetry bundle <trace_id_after> --pretty
```

You are looking for the error to be **absent**, and for the sequence of SQL and
business events to be *correct* — not merely quiet. A fix that silences the error by
skipping the work is a worse bug than the one you started with.

## The honesty rule

Report what actually happened. If the suite has a pre-existing failure unrelated to
your change, say so and name it rather than describing the suite as green. If check (c)
is flaky, say it is flaky and how many runs you did.

A reviewer who finds one overstated claim will re-check everything else you wrote, and
they will be right to.
