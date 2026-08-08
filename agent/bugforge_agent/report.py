"""Assembling the evidence bundle a reviewer reads.

Three terminal outcomes, all legitimate: a fix, working-as-intended, and cannot
reproduce. Each gets a finished document — closing without a patch should not look
like a shrug.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .state import State


def _timeline(run_dir: Path, limit: int = 14) -> str:
    bundle_path = run_dir / "bundle.json"
    if not bundle_path.exists():
        return "_(no telemetry bundle captured)_"
    try:
        b = json.loads(bundle_path.read_text())
    except json.JSONDecodeError:
        return "_(bundle.json unreadable)_"
    rendered = b.get("rendered") or []
    if not rendered:
        return "_(bundle contained no rendered timeline)_"
    lines = rendered[:limit]
    body = "\n".join(f"    {ln}" for ln in lines)
    if len(rendered) > limit:
        body += f"\n    ... {len(rendered) - limit} more events"
    return body


def _verify_block(run_dir: Path) -> str:
    p = run_dir / "verify.json"
    if not p.exists():
        return "- _not run_"
    v = json.loads(p.read_text())
    c = v["checks"]
    out = []
    nt = c.get("new_test", {})
    if nt.get("path"):
        out.append(f"- `{nt['path']}` — {nt.get('before', '?')} before, "
                   f"{nt.get('after', '?')} after")
    fs = c.get("full_suite", {}).get("counts") or {}
    if fs.get("passed") is not None:
        out.append(f"- Full suite: {fs.get('passed')} passed, {fs.get('failed') or 0} failed")
    rp = c.get("repro", {})
    if rp.get("symptom_before") is not None:
        out.append(f"- Browser reproduction re-run: symptom "
                   f"{'absent' if not rp.get('symptom_after') else 'STILL PRESENT'}")
    out.append(f"- Verdict: **{v['verdict']}**")
    for prob in v.get("problems", []):
        out.append(f"- ⚠️ {prob}")
    return "\n".join(out)


def build(cfg, ticket: str, mode: str = "fix",
          ticket_data: dict | None = None) -> dict[str, Any]:
    run_dir = cfg.run_dir(ticket)
    st = State(run_dir, ticket)
    d = st.data
    subject = (ticket_data or {}).get("subject", "")
    customer = (ticket_data or {}).get("customer_email", "")
    hypothesis = d.get("hypothesis") or "_(none recorded — this is a problem)_"

    if mode == "escalate":
        attempts = d.get("repro_attempts", [])
        tried = "\n".join(
            f"{i + 1}. {a.get('description', 'attempt')} — {a.get('outcome', 'no symptom')}"
            f"{'  → ruled out: ' + a['ruled_out'] if a.get('ruled_out') else ''}"
            for i, a in enumerate(attempts)
        ) or "_(none recorded)_"
        md = f"""# #{ticket} — cannot reproduce

**Ticket:** {subject}
**Customer:** {customer}

## What the telemetry shows

{_timeline(run_dir)}

## Best remaining hypothesis

{hypothesis}

## Reproduction attempts ({len(attempts)}/{st.MAX_REPRO_ATTEMPTS})

{tried}

## What would confirm it

{d.get('confirmation_needed', '_(not recorded)_')}

## Where to pick up

{d.get('next_step', 'Start from the timeline above, not from the beginning.')}
"""
        st.phase("escalated")

    elif mode == "working-as-intended":
        md = f"""# #{ticket} — working as intended, no code change

**Ticket:** {subject}
**Customer:** {customer}

## What happened

{d.get('explanation', hypothesis)}

## Evidence

{_timeline(run_dir)}

## Reproduced

{d.get('repro_summary', 'Yes — behaviour is correct at every step.')}

## Suggested reply to the customer

{d.get('customer_reply', '_(write this — the ticket is not finished without it)_')}

## Worth considering

{d.get('secondary', '_(none)_')}
"""
        st.phase("closed_no_change")

    else:
        # A reviewer may be opening this cold — from a link, with no idea what the
        # project is or that an agent wrote it. Lead with orientation, not with the
        # root cause, or the first thing they read is a line number for a file they
        # have never seen.
        preamble = d.get("context_preamble", "")
        secondary = d.get("secondary_findings") or []
        sec_md = "\n\n".join(
            f"**{s.get('file', '?')}** — {s.get('issue', '')}" for s in secondary
        ) or "_(none)_"
        md = f"""{preamble}# fix: {d.get('fix_summary', subject)} (#{ticket})

**Root cause**
{hypothesis}

**Customer impact**
{d.get('impact', '_(describe who is affected and why the symptom looks the way it does)_')}

**Evidence**
Session `{d.get('session_id', '?')}`, trace `{d.get('trace_id', '?')}`:

{_timeline(run_dir)}

**The fix**
{d.get('fix_description', '_(what changed and why it addresses the cause, not the symptom)_')}

**Verification**
{_verify_block(run_dir)}
- Reviewers can run it: `bf repro run {run_dir}/repro.py --label check`

**Also found — not fixed here**
{sec_md}

Closes #{ticket}
"""

    out = run_dir / "report.md"
    out.write_text(md)
    st.artifact("report", out)
    return {"ticket": ticket, "mode": mode, "path": str(out), "markdown": md}
