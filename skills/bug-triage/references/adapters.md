# Adapters — pointing this skill at a real stack

The skill never talks to bugforge directly. It talks to four capabilities, each behind
a driver. Swap the drivers and the same loop triages real tickets against real
services.

```yaml
# bugforge.yaml
tickets:   { driver: supportdesk, url: http://localhost:3001 }
telemetry: { driver: bugforge,    url: http://localhost:8001 }
vcs:       { driver: gitea, url: http://localhost:3002, repo: bugforge/shopforge }
app:
  url: http://localhost:3000
  api_url: http://localhost:8000
  repo_path: .
  test_cmd: "docker compose exec -T api pytest -q"
  control_plane: http://localhost:8000/api/debug   # sandbox only
```

## The four capabilities

| Capability | Must provide | Drivers |
|---|---|---|
| `tickets` | list, get(id) → {subject, body, customer, opened_at, device} | `supportdesk`, `linear`, `zendesk`, `github-issues` |
| `telemetry` | search, session(id), trace(id), bundle(id) | `bugforge`, `sentry`, `otel`, `datadog` |
| `vcs` | branch, commit, push, open_pr | `gitea`, `github` |
| `app` | a URL to drive, a repo to edit, a test command | — |

## Writing a driver

Subclass the base in `agent/bugforge_agent/adapters/` and register it. The contract is
narrow on purpose — the only hard requirement is **`bundle(trace_id)`**, because that
is what the investigation step depends on.

If your telemetry backend has no native equivalent, compose it: fetch the event, its
stack frames, and the surrounding session, and assemble the bundle shape yourself. The
skill only cares about the shape.

## What degrades gracefully, and what does not

**Degrades fine:**

- *No frontend telemetry* (Sentry backend-only). You lose the ability to diagnose
  pure-frontend bugs from history — those become reproduce-first instead of
  investigate-first. Everything else works.
- *No control plane.* `bf flags` and `bf app reset` are bugforge-specific. Real systems
  have no bug switches. The skill only uses them to set up a scenario; the loop does
  not depend on them.
- *No video.* Playwright trace files alone are adequate evidence.

**Does not degrade:**

- *No trace correlation between frontend and backend.* If you cannot join a click to a
  server log, the investigation step collapses to reading stack traces, and the skill
  is not doing anything a stack-trace-to-patch tool does not already do. **This is the
  capability worth adding to a real system before adopting this.**

## Going to production: what changes

**Read-only telemetry.** Never write to a production observability backend.

**No control plane.** Delete the `control_plane` config. Toggling flags in production
is not triage.

**Reproduce in staging, not production.** `app.url` points at a staging environment
seeded to resemble the customer's state. The skill will happily place orders — make
sure they are not real ones.

**Personas, not real accounts.** Never authenticate as the customer. Use a test
account configured to match the relevant state (locale, order history, tier).

**PII.** Real telemetry contains it. Redact at the adapter boundary, before it reaches
the model — do not rely on the model to ignore what it can see.

**Human approval.** The skill opens a PR. It never merges. Keep it that way.

## Sanity check after swapping drivers

```bash
bf doctor
bf ticket get <a real ticket id> --pretty
bf telemetry search --user <a real user> --since 7d --pretty
bf telemetry bundle <a real trace id> --pretty
```

If `bundle` returns a timeline with stack frames and file:line, the skill will work. If
it does not, fix the adapter before running the loop — everything downstream inherits
the quality of that one call.
