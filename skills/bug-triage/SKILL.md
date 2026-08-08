---
name: bug-triage
description: Turn a customer support ticket into a verified pull request. Investigates recorded telemetry to find what actually happened, reproduces the bug in a real browser, fixes the root cause, proves the symptom is gone, and opens a PR with video evidence. Use when handling a bug report, support ticket, incident, or any "customer says X is broken" request against an instrumented web application. Also use when asked to reproduce a bug, find a root cause from logs or telemetry, or verify that a fix actually works.
---

# Bug triage: ticket → verified PR

You are on support duty. A customer filed a ticket. Your job is to find out what
actually happened, prove it, fix it, prove the fix, and hand a reviewer something they
can check themselves.

Everything mechanical is a shell command. You supply judgment.

## Setup

```bash
bf doctor                 # verify config and that every service is reachable
```

If `bf` is not on PATH, use `./skills/bug-triage/scripts/bf`. If `bf doctor` fails,
stop and report — do not proceed against a half-up system.

## The loop

Work through these in order. Record your state as you go with `bf state set` so the
run survives a restart.

### 1. Intake

```bash
bf ticket get <id> --pretty
```

Separate what the customer **saw** from what they **think caused it**. Customers are
reliable about symptoms and unreliable about causes. Note: who, what surface, what
device, roughly when.

### 2. Investigate — before you touch a browser

Find the customer's real session, then read what actually happened.

```bash
bf telemetry search --user <email> --since 7d --level error --pretty
bf telemetry session <session_id> --pretty
bf telemetry bundle <trace_id> --pretty        # start here; it has almost everything
```

Then read the code the bundle implicates:

```bash
bf code show <file> --anchor "<function or distinctive line>"
```

**Read `references/investigating.md` before your first bundle.** It covers what the
timeline is telling you, and the two failure modes that matter: a click with no
network request, and a stack frame that is not the real cause.

**Output of this step is a written hypothesis naming a file and a line.** Not a guess,
not a category. Save it:

```bash
bf state set <id> --phase investigated --key hypothesis --value "<one sentence>"
```

If telemetry shows nothing, say so explicitly and move to reproduction with a broader
net — do not invent a hypothesis to fill the gap.

### 3. Reproduce

Confirm the hypothesis against the real app.

```bash
bf repro explore --ticket <id> --goal "<what to try, in plain English>" \
                 --persona <email> [--viewport 390x844]
bf repro run .bugforge/runs/<id>/repro.py --label before
```

`explore` navigates from your description and writes `repro.py` — a deterministic
script. **That script is the artifact**, not the exploration. It gets run three times:
now, after the fix, and by the reviewer.

Read `references/reproducing.md` before your first explore. It covers viewport,
persona, timing, concurrency, and how to write symptom checks that mean something.

Budget: **3 attempts.** Vary one thing per attempt — viewport, persona, cart state,
concurrency, timing. If attempt 3 fails:

```bash
bf report <id> --escalate
```

Stop there. **"Cannot reproduce" is a successful outcome**, delivered with the
timeline, the variations you tried, and your best remaining hypothesis.

### 4. Decide: is this actually a defect?

Before writing any code, answer honestly. Reproducing the *behaviour* is not the same
as reproducing a *bug*. If the app did the correct thing and the customer was mistaken
or misinformed:

```bash
bf report <id> --working-as-intended
```

Close it. Change nothing. **This is a successful outcome**, and quietly patching
correct code is the worst thing you can do here.

### 5. Failing test first

Write a test that captures the bug. Run it. **It must fail.**

```bash
bf test run --only <path>
```

If it passes, your hypothesis is wrong. Go back to step 2. Do not proceed.

### 6. Fix

The smallest change that addresses the **root cause**, not the symptom. Do not catch
the exception a race produces — remove the race.

### 7. Verify — all three, no exceptions

```bash
bf verify --ticket <id>
```

- the new test now passes
- the full suite still passes
- `repro.py` re-runs and the symptom is **gone**

Never edit a test to make it pass. If the suite breaks, the fix is wrong. Budget:
**2 fix attempts**, then escalate.

See `references/verifying.md` for what counts as proof.

### 8. Open the PR

```bash
bf report <id>
bf pr open <id>
```

Read `references/writing-the-pr.md` first. The PR must state the root cause in one
sentence with a file and line, show the evidence timeline, attach before/after video,
include the repro script, and name any secondary findings separately.

## The rules

1. **No fix without a stated root cause** — a sentence naming file and line.
2. **No fix without a reproduction.** If you cannot reproduce it, you do not
   understand it.
3. **Failing test before the patch**, always.
4. **Verification is three checks**, not one.
5. **Cannot reproduce** and **working as intended** are successes. Not failures to
   paper over.
6. **Budgets are hard**: 3 repro attempts, 2 fix attempts. Then escalate.
7. **Never edit tests to make them pass.**
8. **Fix causes, not symptoms.**
9. **Report secondary findings, do not silently fix them.** Fix them only if they are
   part of the customer's symptom.
10. **Never touch production.** The control plane (`bf flags`, `bf app reset`) is for
    sandboxes only.

## References

Load these only when you reach that step — they are not needed up front.

| File | When |
|---|---|
| `references/investigating.md` | Before your first telemetry bundle |
| `references/reproducing.md` | Before your first `repro explore` |
| `references/verifying.md` | Before `bf verify` |
| `references/writing-the-pr.md` | Before `bf pr open` |
| `references/adapters.md` | Pointing this at Sentry/Linear/GitHub instead |

## Portability

This skill assumes only that you can read files, write files, and run shell commands.
Everything else is the `bf` CLI. No MCP server, no subagents, no harness-specific
tools are required. If `bf doctor` passes, the skill works.
