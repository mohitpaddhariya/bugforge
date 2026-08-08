# Push to Prod — submission

Copy-paste ready. Every number here was measured, not estimated.

---

## Project name

**bugforge**

## One-liner

Ticket in, verified pull request out — it reproduces the bug in a real browser, fixes
the root cause, and re-runs the customer's exact path to prove it's gone.

## Short description (~50 words)

bugforge turns a customer support ticket into a reviewed pull request. It reads the
customer's recorded session to find what actually happened, reproduces the bug in a
real browser, fixes the root cause, and proves the symptom is gone — then opens a PR
with the before/after recording attached. It ships with a deliberately broken store to
prove it works.

## Full description

Two halves that need each other.

**ShopForge** — a small e-commerce app (login → cart → coupon → checkout → orders)
running in Docker. Both the frontend and the backend record everything they do into
one telemetry store, joined by a trace ID that is generated on a click in the browser
and survives into the server's stack traces. Five bugs are planted in it, each
switchable at runtime, each with a written answer key: file, cause, expected symptom.

**The bug-triage skill** — an agent that turns a ticket into a PR:

1. Searches telemetry for the customer's session and reads the merged timeline
2. Reads the implicated source and states a root cause naming a file and a line
3. Reproduces the bug in a real browser, recording video and a Playwright trace
4. Writes a test that fails
5. Fixes the root cause
6. Verifies three ways: new test passes, full suite passes, and the original browser
   reproduction re-runs clean
7. Opens a PR with the root cause, the evidence timeline, and the before/after video

"Cannot reproduce" and "working as intended" are first-class successful outcomes. One
of the five tickets is not a bug at all, and the correct behaviour is to close it
without touching any code.

The store is not a demo of the agent. It is the **test track** — you cannot grade a
bug-fixing agent on bugs you did not plant yourself.

---

## The problem

Support tickets and stack traces live in different worlds.

A customer writes *"checkout just spins."* An engineer has a 500 in a log somewhere.
Connecting those two costs the first hour of every bug, and it is almost entirely
mechanical: find the session, find the request, find the line, work out which of the
customer's clicks caused it.

Meanwhile the AI tooling that has appeared around this problem is pointed at the wrong
half. Generating a plausible patch from a stack trace is close to solved. What is not
solved, and what actually blocks shipping, is **proof**:

- Did it reproduce the bug, or just imagine one?
- Did it fix the cause, or silence the symptom?
- Is the customer's symptom actually gone, on the path the customer took?
- Did it break something else?

And underneath that, a measurement problem nobody talks about: **you cannot tell
whether a bug-fixing agent was right.** A patch that looks reasonable and a patch that
is correct read identically in a diff. Existing benchmarks do not help — SWE-bench is
repo-only with no running UI and no telemetry; WebArena measures task completion, not
debugging.

So there is no public way to answer "is this agent any good at triage?"

## The opportunity

Build the missing measurement layer, then build the agent on top of it.

A running full-stack app, instrumented end to end, with bugs whose answers are known
in advance — a **bug gym**. Once that exists, an agent's diagnosis can be graded
instead of admired, and the interesting classes of bug become testable: races,
mobile-only layout faults, contract drift across the frontend/backend boundary,
authorization holes, and tickets that turn out not to be bugs.

The agent is the demo. The gym is the contribution.

---

## What I built during Push to Prod

Everything below was built during the event, starting from an empty directory.

### The store — 6 services in Docker Compose

| Service | Role |
|---|---|
| `web` | Next.js 15 storefront, ships the browser telemetry tracker |
| `api` | FastAPI + SQLAlchemy, emits request/SQL/business/error telemetry |
| `collector` | Ingest + query API. Deliberately separate, so a bad patch to `api` cannot blind the agent mid-investigation |
| `db` | Postgres, schemas `shop` and `telemetry` |
| `supportdesk` | Ticket system, no dependency on the store so it stays up when the store breaks |
| `gitea` | Sandboxed git host |

### Trace correlation — the piece everything depends on

A trace ID is minted per **user interaction**, not per HTTP request, so one click that
fires three API calls produces one trace. It rides an `X-Trace-Id` header into the API
and is written onto every log line, SQL statement, business event and stack frame.

One query returns both sides on one clock:

```
t_5fade 43.194  WEB   click #place-order
t_5fade 43.219  API   coupon_applied  SAVE20 uses=4
t_5fade 43.231  API   ERROR  CheckViolation           checkout.py:71
t_5fade 43.242  WEB   POST /api/checkout → 500
t_5fade 45.400  WEB   click #place-order   ← retried
t_5fade 48.900  WEB   click #place-order   ← retried
```

Those last two lines are the whole argument for doing this: the customer got no
feedback, which is why the ticket says "spinning" and not "error".

### Five planted bugs, each breaking the agent differently

| Bug | Class | What it tests |
|---|---|---|
| Coupon race (TOCTOU) | backend concurrency | can it deliberately induce a race |
| Invisible click (overlay eats the mobile button) | frontend only | **zero backend logs** — diagnosable only from the click's real hit target |
| `total_cents` → `total` rename | contract drift | requires reading both sides |
| Order IDOR | authorization | recognising a security issue from an innocent ticket |
| Expired coupon | **not a bug** | must close with no patch |

Each toggles at runtime with no rebuild. Each has a manifest with the answer key.

### Ghost runs

For the agent to investigate "the customer's session from Tuesday," that session has
to exist before the agent runs. Rather than hand-writing telemetry rows, seeding
**drives the broken app** as the customer — including the human parts, like clicking
the button three times and giving up. The telemetry is real because it was really
produced. Each ghost asserts its own symptom appeared and fails loudly otherwise:
**31/31 confirmed across four consecutive resets.**

### The agent, as a harness-agnostic skill

Claude Code, Cursor, Codex, OpenHands and Aider agree on exactly three capabilities:
read a file, write a file, run a shell command. So the skill assumes only those, and
everything else is a CLI (`bf`) shipped with it. No MCP server, no subagents, no
harness-specific tool calls.

```
SKILL.md + references/   judgment: the loop, budgets, when to stop, what counts as proof
bf CLI                   mechanics: telemetry, browser, tests, git, PR
```

The split rule: if two competent engineers would produce the same output, it is a
script. The model never hand-writes an HTTP request or parses a log format.

Run state lives on disk (`.bugforge/runs/<ticket>/`), not in context, so a run
survives a restart and any harness can resume one it did not start.

### Ticket #1042, end to end — the result

The agent reached the root cause from telemetry alone, in five commands, before
opening a browser. Graded against the answer key written in the design doc:

| | Answer key | Diagnosis |
|---|---|---|
| File | `api/app/routers/checkout.py` | `checkout.py:71` ✅ |
| Cause | read-modify-write on `coupons.uses` without row locking | same, and it also named the time-of-check site the key omits ✅ |
| Secondary | `web/app/checkout/page.tsx` — loading state never cleared | found at `page.tsx:99` ✅ |

Verification:

```
✓ new_test    test_coupon_race — FAILED before, PASSED after
✓ full_suite  4 passed, 0 failed
✓ repro       symptom present before, absent after (same script, hash-pinned)
verdict: VERIFIED
```

**PR: https://github.com/mohitpaddhariya/bugforge/pull/1** — root cause with file and
line, the evidence timeline, before/after GIFs embedded, and the reproduction script
committed so a reviewer can run it themselves.

`bf pr open` refuses to run on an unverified ticket.

### Honest status

| | |
|---|---|
| Store, telemetry, ghost runs | complete, all acceptance criteria passing |
| Skill + `bf` CLI | complete |
| Ticket 1042 | **full loop, PR merged-ready** |
| Tickets 1043–1046 | bugs planted, tickets written, telemetry seeded — **not yet run through the agent** |
| browser-use exploration | not wired up; reproductions are Playwright, written against the CLI's scaffold |
| Gitea | running, but the PR went to GitHub |
| Scoring harness across all 5 | not built |

---

## Challenges and key decisions

### Decision: the collector is a separate service

The agent edits `api` code. If telemetry lived inside `api`, a bad patch could blind
the agent mid-investigation. The observability plane has to survive the data plane.
Verified by killing `api` and confirming the collector still served past telemetry.

### Decision: trace ID per intent, not per request

Per-request tracing answers "what happened during this HTTP call." Per-interaction
tracing answers "what happened when the user clicked this" — which is the question a
ticket actually asks.

### Decision: generate the historical telemetry, never fake it

Hand-written telemetry rows drift from what the app really emits, and the agent learns
to trust fiction. Driving the broken app to produce it costs more and is the only
version that stays true.

### Decision: exploration compiles to a deterministic script

An LLM navigating a browser is the right tool for finding a path from a vague ticket
and the wrong tool for proving a fix — if it re-navigates differently, "it worked the
second time" proves very little. So the reproduction is a Playwright script that is
run three times: to confirm, to verify, and by the reviewer. `bf verify` hashes it, so
editing the script to make the "after" run pass voids the verification.

### Challenge: my own tooling faked a pass three times

The most useful thing that went wrong. `ctx.api` sent a JSON body without a
content-type header, so every authenticated call in a reproduction 401'd — and the
reproduction reported **ABSENT**, i.e. "no bug here", while the bug was live and
firing. A green result from a broken harness is worse than a red one.

Two more of the same shape: `/api/debug/reset` wipes the sessions table and killed the
browser's cookie mid-run; and Playwright names videos per page, so re-runs accumulated
files and the GIF step picked an arbitrary old one.

This is why every ghost run asserts its own symptom and fails loudly. Silent
degradation is the failure mode that matters in agent tooling, because everything
downstream still looks fine.

### Challenge: the race could not be filmed

The 500 needs two checkouts to interleave. The browser's connection is already warm,
so the customer's request won that race in every single recorded run — the camera kept
ending up pointed at a success.

I stopped trying to force it and split the evidence instead: the video shows the
**customer-visible half**, which is deterministic (a rejected checkout the page never
surfaces — the spinning button), and the race itself is evidenced by the timeline and
by `test_coupon_race`. The PR says exactly that in a sentence rather than implying the
video shows something it does not.

### Challenge: making a race a reliable regression test

A barrier alone was not enough — the first request usually finished before the second
was dispatched, and the test passed while the bug was live. It now fires three
concurrent checkouts, retries up to six collisions, and re-primes the coupon to
`uses = max_uses − 1` between attempts. Verified in both directions: red with the bug
on, green with it off.

### Decision: "no patch" is a success state

An agent that always produces a patch will confidently fix working code, and nobody
will notice. Ticket 1046 exists to test the refusal, and `bf report
--working-as-intended` produces a finished document rather than a shrug.

---

## Links and assets

| | |
|---|---|
| **Public repo** | https://github.com/mohitpaddhariya/bugforge |
| **Example PR** | https://github.com/mohitpaddhariya/bugforge/pull/1 |
| **Hero image** | `docs/images/hero.png` |
| **Before / after** | `docs/images/before.png`, `docs/images/after.png` |
| **Design docs** | `docs/01-store-spec.md`, `docs/03-agent-spec.md` |
| **The skill** | `skills/bug-triage/SKILL.md` |

### Running it

```bash
git clone https://github.com/mohitpaddhariya/bugforge && cd bugforge
docker compose up -d --wait      # 6 services, ~26s cold
make reset                       # seed + ghost runs
cp agent/bugforge.example.yaml bugforge.yaml
./agent/bf doctor --pretty       # should be all green
```

Then, in any harness with the skill loaded: `triage ticket 1042`.

### On deployment

Not deployed publicly, deliberately. The store contains planted vulnerabilities —
including an IDOR that exposes other users' orders — so hosting it on the open
internet would be irresponsible. It is a one-command local stack instead.

---

## Demo video — 90 second shot list

1. **0:00** Read ticket #1042 aloud from supportdesk, verbatim. Point out: no error, no
   steps, and a wrong theory about being charged three times.
2. **0:15** `bf telemetry bundle <trace> --pretty`. Hold on the timeline. Say: *"left
   is the browser, right is the server, one clock."* Point at the two retry clicks —
   *"nobody told her it failed."*
3. **0:40** `bf repro run … --label before --headed`. Watch the button wedge on
   "Placing order…".
4. **0:55** `bf verify --ticket 1042 --pretty` → three ticks, VERIFIED.
5. **1:05** Open PR #1. Scroll to the before/after pair.
6. **1:20** Open `bugs/BUG-001.yaml`. *"The answer was specified before the code was
   written. So we're not asking whether the diagnosis sounds plausible — we know what
   it was."*
7. **1:30** *"And none of this is tied to one AI tool. The skill needs read, write, and
   shell. Nothing else."*

Say **"this is live, unedited"** out loud, and let something take three seconds.

---

## Disclosure: pre-existing work and reused code

**Pre-existing work: none.** The repository was created empty at the start of the
event. Every line of application code, telemetry, skill instruction and design
document in it was written during Push to Prod. Full history is public: 3 commits on
`main` plus the fix branch, 137 tracked files.

**Third-party code**, used as ordinary dependencies, unmodified: Next.js 15, React,
Tailwind, FastAPI, SQLAlchemy 2.0, Pydantic, psycopg2, Postgres 16, Playwright, httpx,
PyYAML, Docker Compose, Gitea, ffmpeg. No code was copied from tutorials, templates,
starters, or another project.

**AI assistance.** This was built with Claude Code (Opus). I wrote the specifications
and made the architecture calls — separating the collector from the API, tracing per
interaction rather than per request, generating rather than faking historical
telemetry, compiling exploration into a deterministic script — and Claude Code
implemented against those specs. One phase used a parallel multi-agent workflow: 11
agents on disjoint directories building the store scaffold from
`docs/01-store-spec.md`, then integrating and adversarially verifying it against the
spec's acceptance criteria.

**Note on the "answer key written first" claim.** The bug catalogue — file, cause,
expected symptom for all five bugs — was specified in `docs/01-store-spec.md` §7
before any implementation code existed. The per-bug YAML manifests were written in the
same phase as the code, from that spec. So the claim is that the *specification*
predates the implementation, which the commit history supports; it is not a claim that
each manifest file predates its corresponding source file.

**Prior art this builds on, none of it reused as code:** SWE-bench (repo-only, no
runtime), WebArena (task completion, not debugging), and commercial session-replay and
error-monitoring tools, whose trace-correlation idea is the thing this project takes
seriously and builds the rest on top of.
