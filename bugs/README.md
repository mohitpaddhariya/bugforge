# `bugs/` — the answer sheets

Five manifests, one per planted scenario. Each is written **before** the code it
describes, which is the whole point: an answer sheet authored after the fact
tends to describe whatever the code happens to do, and then every diagnosis
looks correct.

These files are the grading key. They are what makes it possible to say the
robot was **right** rather than merely plausible.

| File | Scenario | Ticket | Layer | Flags |
|---|---|---|---|---|
| `BUG-001.yaml` | Coupon race (TOCTOU) at checkout | 1042 | backend | `BUG_COUPON_TOCTOU`, `BUG_CHECKOUT_SWALLOWS_ERROR` |
| `BUG-002.yaml` | Invisible promo overlay eats the click | 1043 | frontend | `BUG_PROMO_OVERLAY` |
| `BUG-003.yaml` | `total_cents` → `total` contract drift | 1044 | contract | `BUG_TOTAL_FIELD_RENAME` |
| `BUG-004.yaml` | Order leak (IDOR) on `/api/orders/{id}` | 1045 | backend | `BUG_ORDER_IDOR` |
| `BUG-005.yaml` | **Not a bug** — `EXPIRED15` really expired | 1046 | — | *(none)* |

> The robot must never read these files. They are the examiner's copy. Keep them
> out of any context the agent is given, out of ghost-run scripts, and out of
> anything the agent's repo checkout can see.

---

## Manifest format

Top-level keys, in the order they appear in every file.

| Key | Meaning |
|---|---|
| `id` | `BUG-00N`. Matches the filename. |
| `title` | One line, engineer voice. The ticket is the customer voice; this is the truth. |
| `ticket` | The `supportdesk` ticket id the customer filed. Entry point for a run. |
| `layer` | `backend` · `frontend` · `contract` · `none` |
| `class` | Defect family: `concurrency`, `layout / hit-testing`, `contract-drift`, `authorization / security`, `not-a-bug`. |
| `severity` | What the *fix* should be filed at, not what the ticket implies. See BUG-004. |
| `flags` | Map of feature-flag key → desired value to activate the scenario. `{}` for BUG-005. |
| `symptom` | `user_visible`, `http`, `backend_logs` (bool), `frontend_logs` (bool), `notes`. The bools say where evidence will and will not be found — BUG-002 has `backend_logs: false` on purpose. |
| `answer_sheet` | The graded part. Detailed below. |
| `telemetry_signature` | What the trace should look like, and the one detail that distinguishes this bug from a plausible-but-wrong story. |
| `repro` | Persona, password, viewport, concurrency, preconditions, steps, expected observation. Everything needed to reproduce deterministically. |
| `regression_test` | Path, kind (`pytest` / `browser`), `must_fail_before_fix`, and the assertions it has to make. |
| `grading` | The pass bar: which file must be named, which root cause must be stated, plus `bonus` credit. |

BUG-005 additionally carries `expected_outcome: "working_as_intended, no patch"`,
a `reality` block, and a `close_criteria` block with numbered evidence items.

### The `answer_sheet` block

```yaml
answer_sheet:
  files:                       # every site that is part of the defect
    - path: api/app/routers/checkout.py
      anchor: "flags.is_enabled(flags.BUG_COUPON_TOCTOU)"
      anchor_alternatives: ["def _redeem_coupon", "coupon.uses + 1"]
      lines: null              # resolved later, from the anchor
      role: primary            # primary | victim
      why: >
        Why this exact site is the defect.
  root_cause: >                # mechanism, in prose, precise enough to grade against
  correct_fix: >               # what a correct patch does
  incorrect_fixes: []          # plausible patches that must NOT score as correct
  secondary: []                # separate defects surfaced by the same ticket
```

### Why `lines` is `null`

The manifests were written while the code was still being written, so there are
no stable line numbers yet. Guessing them would produce a grading key that is
confidently wrong.

Instead every file entry carries an **`anchor`**: a distinctive substring or
function name that pins the site regardless of how the file shifts. A later pass
resolves anchors to line numbers:

```bash
# resolve one anchor
grep -n 'flags.is_enabled(flags.BUG_COUPON_TOCTOU)' api/app/routers/checkout.py
```

Anchors are chosen to be stable: the primary anchor for each planted bug is the
feature-flag branch itself, which must exist for the bug to be toggleable at
all. `anchor_alternatives` are fallbacks if the primary drifts.

`role: primary` is the site to patch. `role: victim` is a site that *exhibits*
the symptom but must not be edited — patching a victim is a classic false fix
(BUG-002's Place Order button is the example).

### `incorrect_fixes` is load-bearing

Most of these bugs have a patch that makes the symptom disappear without fixing
anything: catch the `IntegrityError` and return a 400; read `total` on the
client; raise the button's `z-index`; extend the expiry. Each of those is listed
explicitly so a grader can mark them wrong rather than argue about them.

---

## Toggling flags at runtime

Bugs are **DB rows**, not build-time constants. No rebuild, no restart.

```
GET  /api/debug/flags              → [{key, enabled, description}, ...]
POST /api/debug/flags {key, enabled}
POST /api/debug/reset              → all flags off
```

The `/api/debug/*` routes are the harness control plane and are **excluded from
telemetry**, so setup calls never pollute the timeline the robot reads.

```bash
# see the switchboard
curl -s http://localhost:8000/api/debug/flags | jq

# turn one bug on
curl -s -X POST http://localhost:8000/api/debug/flags \
  -H 'content-type: application/json' \
  -d '{"key":"BUG_ORDER_IDOR","enabled":true}'

# turn everything off
curl -s -X POST http://localhost:8000/api/debug/reset
```

The api caches the flag table for ~2s (`FLAGS_CACHE_TTL`), so a flip takes effect
within two seconds. The web app re-reads `GET /api/debug/flags` on a 5s interval,
so frontend-side switches (`BUG_PROMO_OVERLAY`,
`BUG_CHECKOUT_SWALLOWS_ERROR`) land without a page reload.

The authoritative flag registry lives in `api/app/flags.py`
(`FLAG_DESCRIPTIONS`). If a key here is not there, it is a typo.

---

## Activating a single scenario

One scenario at a time. Running two bugs at once makes attribution ambiguous and
teaches the robot to guess.

```bash
# 1. clean state: data + telemetry + ghost runs
make reset

# 2. all switches off
curl -s -X POST http://localhost:8000/api/debug/reset

# 3. turn on exactly the flags in the manifest's `flags:` block
#    BUG-001 needs both of its flags — the ticket describes both defects
curl -s -X POST http://localhost:8000/api/debug/flags \
  -H 'content-type: application/json' -d '{"key":"BUG_COUPON_TOCTOU","enabled":true}'
curl -s -X POST http://localhost:8000/api/debug/flags \
  -H 'content-type: application/json' -d '{"key":"BUG_CHECKOUT_SWALLOWS_ERROR","enabled":true}'

# 4. verify
curl -s http://localhost:8000/api/debug/flags | jq '.[] | select(.enabled)'

# 5. point the robot at the manifest's ticket
open http://localhost:3001/tickets/1042
```

**BUG-005 activation** is the same procedure with step 3 omitted: `flags: {}`
means run with everything off. The scenario is the ticket, not a switch.

### Preconditions that are not flags

Some manifests need seed state as well as a flag. `make reset` restores it; if
you have already burned it, reset again rather than patching the row by hand.

- **BUG-001** needs `SAVE20` at `uses=4, max_uses=5`. One successful checkout
  consumes the boundary and the race stops reproducing.
- **BUG-002** needs the promo banner not yet dismissed in the browser session,
  and a viewport narrower than 768px.
- **BUG-005** needs `EXPIRED15` with `expires_at` in the past.

---

## Maintaining these files

- Change the code, re-check the anchor. A manifest whose anchor no longer
  matches anything is silently broken and will mis-grade every run.
- If a bug's mechanism changes, update `root_cause` and `incorrect_fixes` in the
  same commit. A stale answer sheet is worse than no answer sheet.
- Adding a scenario: new `BUG-00N.yaml` in this format, a new flag constant in
  `api/app/flags.py`, a new ticket in `supportdesk/app/tickets.py`, and a ghost
  run in `scripts/ghosts/`. All four, or the scenario is not real.
