# Agent Spec — the bug-triage skill

Phases **P3–P5**. The thing that turns a ticket into a pull request.

---

## 1. The requirement that shapes everything

> "I want this harness-agnostic — I can drop this skill into any harness and it works."

That single requirement decides the architecture. It rules out the obvious design
(a Python agent with an LLM loop inside it) and forces a different one.

### What a harness gives you

Claude Code, Cursor, Codex, OpenHands, Aider, the Agent SDK — they differ in almost
everything. Tool names, context limits, permission models, subagents, MCP support,
whether they can drive a browser.

They agree on exactly three capabilities:

```
read a file  ·  write a file  ·  run a shell command
```

That intersection is the entire portable substrate. **Anything the skill needs beyond
those three must be a shell command we ship ourselves.**

### Consequence: the skill is instructions + a CLI

```
┌─────────────────────────────────────────────────────┐
│  THE HARNESS (whatever it is)                       │
│  provides: the model, file access, a shell          │
└───────────────────────┬─────────────────────────────┘
                        │ reads
                        ▼
              ┌───────────────────┐
              │     SKILL.md      │   judgment: the loop, the rules,
              │   + references/   │   when to stop, what counts as proof
              └─────────┬─────────┘
                        │ invokes via shell
                        ▼
              ┌───────────────────┐
              │      bf CLI       │   mechanics: telemetry queries, browser
              │  (JSON in / out)  │   driving, test runs, git, PR creation
              └─────────┬─────────┘
                        ▼
                 the app under test
```

No MCP dependency. No harness-specific tool calls. No subagent requirement. If the
harness can run `bf telemetry bundle t_9f3a`, the skill works.

---

## 2. The split: what is deterministic, what is judgment

Getting this line in the right place is the whole design.

| Deterministic — belongs in the CLI | Judgment — belongs in SKILL.md |
|---|---|
| Query telemetry, merge and format the timeline | Which session is the ticket actually about |
| Extract stack frames, resolve file:line | What the root cause is |
| Drive the browser, record video and trace | Whether the reproduction confirms the hypothesis |
| Run the test suite, diff it before/after | Whether this is a real bug at all |
| Toggle flags, reset state, manage git | When to give up and escalate |
| Assemble the evidence bundle | What to write in the PR |

**Rule:** if two competent engineers would produce the same output, it is a script.
If they might disagree, it is the model's call.

The model should never hand-write an HTTP request, parse a log format, or drive
Playwright by hand. Those are solved problems, and doing them in-context burns tokens
and invites errors that look like insight.

---

## 3. Portability beyond bugforge

The skill must not hardcode bugforge. It talks to four abstract capabilities, each
behind an adapter:

```yaml
# bugforge.yaml
tickets:
  driver: supportdesk        # | linear | zendesk | github-issues
  url: http://localhost:3001

telemetry:
  driver: bugforge           # | sentry | otel | datadog
  url: http://localhost:8001

vcs:
  driver: gitea              # | github
  url: http://localhost:3002
  repo: bugforge/shopforge

app:
  url: http://localhost:3000
  repo_path: .
  api_url: http://localhost:8000
  test_cmd: "docker compose exec -T api pytest -q"
  personas_file: agent/personas.yaml
  control_plane: http://localhost:8000/api/debug   # flags + reset; bugforge only
```

Today all four point at our containers. Swap the drivers and the same skill triages
real Sentry errors from real Linear tickets into real GitHub PRs. That is what makes
this more than a demo — the store is the test harness, not the product.

---

## 4. State lives on disk, never in context

Different harnesses have different context windows and some will compact or restart
mid-run. So the run is a directory, not a conversation:

```
.bugforge/runs/1042/
  state.json          phase, hypothesis, attempts, decisions, timestamps
  timeline.txt        the merged telemetry timeline
  bundle.json         raw /telemetry/bundle response
  repro.py            the deterministic reproduction script  ← the key artifact
  before/             video.webm, trace.zip, console.log, network.har
  after/              same, post-fix
  patch.diff
  test_output.txt
  report.md           the evidence bundle
  pr.json
```

Any harness can pick up a half-finished run by reading `state.json`. The skill is
resumable by construction, and the evidence survives the session.

---

## 5. The loop

```
   ticket
     │
     ▼
┌─────────────────────────────────────────────────────────┐
│ 1. INTAKE                                               │
│    bf ticket get 1042                                   │
│    Extract: customer, symptom, surface, device, time.   │
│    Note what the customer THEORISES vs what they SAW.   │
└─────────────────────────────────────────────────────────┘
     ▼
┌─────────────────────────────────────────────────────────┐
│ 2. INVESTIGATE          (before touching a browser)     │
│    bf telemetry search --user ... --since ...           │
│    bf telemetry session s_...                           │
│    bf telemetry bundle t_...                            │
│    Read the implicated source.                          │
│    OUTPUT: a written hypothesis naming file and line.   │
└─────────────────────────────────────────────────────────┘
     ▼
┌─────────────────────────────────────────────────────────┐
│ 3. REPRODUCE                                            │
│    bf repro explore --ticket 1042 --goal "..."          │
│      browser-use navigates from the vague description   │
│      and EMITS repro.py — a deterministic Playwright    │
│      script. That script, not the exploration, is the   │
│      artifact.                                          │
│    bf repro run repro.py --label before                 │
│                                                         │
│    confirmed? ──no──► retry with variations (max 3)     │
│         │                    │                          │
│         │              still no ──► ESCALATE. Stop.     │
│         ▼ yes                                           │
└─────────────────────────────────────────────────────────┘
     ▼
┌─────────────────────────────────────────────────────────┐
│ 4. DECIDE                                               │
│    Is this a defect, or correct behaviour?              │
│    If correct ──► CLOSE as working-as-intended.         │
│                   No patch. This is a SUCCESS.          │
└─────────────────────────────────────────────────────────┘
     ▼
┌─────────────────────────────────────────────────────────┐
│ 5. FAILING TEST FIRST                                   │
│    Write it. Run it. It MUST fail. If it passes, the    │
│    hypothesis is wrong — go back to 2.                  │
└─────────────────────────────────────────────────────────┘
     ▼
┌─────────────────────────────────────────────────────────┐
│ 6. FIX — smallest change that addresses the root cause  │
└─────────────────────────────────────────────────────────┘
     ▼
┌─────────────────────────────────────────────────────────┐
│ 7. VERIFY — all three must pass                         │
│    bf verify --ticket 1042                              │
│      a) the new test now passes                         │
│      b) the full suite still passes                     │
│      c) repro.py re-runs and the symptom is GONE        │
│    (c) is the one nobody else does.                     │
└─────────────────────────────────────────────────────────┘
     ▼
┌─────────────────────────────────────────────────────────┐
│ 8. PR                                                   │
│    bf pr open --ticket 1042                             │
│    root cause · evidence timeline · before/after video  │
│    · the repro script a reviewer can run themselves     │
└─────────────────────────────────────────────────────────┘
```

### Why investigate before reproducing

Because that is what a good engineer does. The ticket says "it spins." The telemetry
says `IntegrityError at checkout.py:94` in that customer's real session. Walking into
the browser with a hypothesis makes reproduction a *confirmation*, not a search.

It also means a bug that is hard to trigger by hand — a race, a specific cart state —
is still tractable, because the recorded session tells you the conditions.

### Why the exploration emits a script

browser-use is the right tool for finding the path from a vague description. It is the
wrong tool for proving a fix, because an LLM re-navigating may take a different route,
and "it worked the second time" proves very little.

So exploration is a *compiler*: vague English in, deterministic Playwright out. That
script is then used three times — to confirm the bug, to prove the fix, and as the
regression test in the PR. A reviewer can run it themselves. That is what makes the
PR credible.

---

## 6. The rules the skill must enforce

These are non-negotiable, and they are the difference between a triage agent and a
patch generator.

1. **No fix without a stated root cause.** A written sentence naming a file and a
   line. "Adding a try/except makes the error go away" is not a root cause.
2. **No fix without a reproduction.** If you cannot reproduce it, you do not
   understand it. Escalate instead.
3. **The failing test comes first.** A test written after the fix proves nothing about
   the bug; it only proves the code does what it currently does.
4. **Verification is three checks, not one.** New test passes, suite passes, and the
   original browser reproduction is re-run and clean.
5. **"Cannot reproduce" is a successful outcome.** Escalate with everything learned:
   timeline, attempted variations, best hypothesis.
6. **"Working as intended" is a successful outcome.** Close the ticket, explain to the
   customer, change no code.
7. **Budgets are hard.** 3 reproduction attempts, 2 fix attempts. Then stop and
   escalate. An agent that keeps going is an agent that starts guessing.
8. **Never edit tests to make them pass.** If the suite breaks after the fix, the fix
   is wrong.
9. **Fix the root cause, not the symptom.** Do not catch the exception the race
   produces; remove the race.
10. **Report secondary findings, do not silently fix them.** If the investigation turns
    up a second defect (the frontend swallowing the 500), name it in the PR. Fix it
    only if it is part of the customer's symptom.

---

## 7. CLI surface

One entrypoint, `bf`. Every command emits JSON on stdout by default (`--pretty` for
humans). Exit code 0 = success, 1 = failure, 2 = inconclusive.

```
bf ticket list
bf ticket get <id>

bf telemetry search --user <email> --since <when> [--level] [--kind] [--text]
bf telemetry session <session_id>
bf telemetry trace <trace_id>
bf telemetry bundle <trace_id>          # the front door: timeline + frames + files + summary

bf code show <file> --anchor <text> [--context N]
bf code grep <pattern>

bf flags list
bf flags set <KEY> <on|off>
bf app reset

bf repro explore --ticket <id> --goal "<what to try>" [--viewport WxH] [--persona <email>]
                                        # browser-use → writes repro.py
bf repro run <script> --label <before|after>
                                        # Playwright → video, trace, HAR, console, verdict

bf test run [--only <path>]
bf verify --ticket <id>                 # the three checks

bf state get <id>
bf state set <id> --phase <p> --key <k> --value <v>

bf report <id>                          # assemble the evidence bundle markdown
bf pr open <id>                         # branch, commit, push, open PR with evidence
```

**`bf repro run` is the load-bearing command.** It returns a verdict object:

```json
{
  "label": "before",
  "symptom_detected": true,
  "checks": [
    {"name": "checkout_returns_500", "passed": true},
    {"name": "button_stuck_loading", "passed": true}
  ],
  "trace_ids": ["t_9f3a", "t_9f3b"],
  "artifacts": {"video": "before/video.webm", "trace": "before/trace.zip",
                "har": "before/network.har", "console": "before/console.log"},
  "duration_ms": 8412
}
```

The symptom checks are declared in `repro.py` itself, so the same script can assert
"broken" before and "fixed" after. That is what makes (c) in the verification step
mechanical rather than a judgement call.

---

## 8. Skill layout

```
skills/bug-triage/
  SKILL.md                    the loop, the rules, the budgets — kept short
  references/
    investigating.md          reading telemetry, forming a hypothesis
    reproducing.md            browser-use → deterministic script, viewport/persona/timing
    verifying.md              the three checks, what counts as proof
    writing-the-pr.md         root cause language, evidence, severity, secondary findings
    adapters.md               pointing the skill at Sentry/Linear/GitHub instead
  scripts/bf                  the CLI (thin shim → agent/ package)
  bugforge.example.yaml
```

`SKILL.md` stays short on purpose — it is always in context. The references are loaded
only when that step is reached. A harness with a small context window must still be
able to run this.

---

## 9. Success criteria

Run all five tickets. The skill must:

| Ticket | Required outcome |
|---|---|
| 1042 coupon race | Root cause = missing row lock in `checkout.py`. PR with a passing concurrency test. Secondary finding (swallowed 500) reported |
| 1043 mobile | Diagnosed from **web telemetry alone** — must notice the click's real hit target was the overlay. Reproduced at <768px |
| 1044 NaN | Identifies the field rename across the boundary, not "add a null check" |
| 1045 order leak | Recognises it as an authorization flaw from an innocent ticket, and says so in the PR |
| 1046 expired coupon | **Closes with no patch.** Any code change here is a failure |

Ticket 1046 is the one that matters most. An agent that fixes it has learned to produce
patches, not to triage.

---

## 10. Deliberately deferred

- Multi-bug tickets (one ticket, two root causes)
- Bugs requiring a schema migration
- Anything needing production data access
- Autonomous merge — a human approves the PR
- Fully parallel triage of all five tickets at once
