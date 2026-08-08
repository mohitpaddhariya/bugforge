"""The three checks. Anything less is a suggestion, not a fix."""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .state import State


def run_tests(cfg, only: str | None = None, timeout: int = 900) -> dict[str, Any]:
    cmd = cfg.app.test_cmd + (f" {only}" if only else "")
    p = subprocess.run(cmd, shell=True, cwd=cfg.repo, capture_output=True,
                       text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"(\d+) passed", out)
    f = re.search(r"(\d+) failed", out)
    e = re.search(r"(\d+) error", out)
    return {
        "cmd": cmd,
        "exit_code": p.returncode,
        "passed": p.returncode == 0,
        "counts": {
            "passed": int(m.group(1)) if m else None,
            "failed": int(f.group(1)) if f else None,
            "errors": int(e.group(1)) if e else None,
        },
        "output_tail": out[-4000:],
    }


async def verify(cfg, ticket: str, only: str | None = None,
                 headed: bool = False) -> dict[str, Any]:
    from .repro import run_script

    run_dir = cfg.run_dir(ticket)
    st = State(run_dir, ticket)
    checks: dict[str, Any] = {}
    problems: list[str] = []

    # (a) the new test — must have failed before the patch
    only = only or st.data.get("regression_test")
    if only:
        after = run_tests(cfg, only=only)
        before = st.data.get("regression_test_before")
        checks["new_test"] = {
            "path": only,
            "before": before or "NOT RECORDED",
            "after": "PASSED" if after["passed"] else "FAILED",
            "passed": after["passed"] and before == "FAILED",
            "output_tail": after["output_tail"][-1500:],
        }
        if before != "FAILED":
            problems.append(
                "the regression test was never observed failing before the fix, so it "
                "proves only that the code does what it currently does. Run it on the "
                "pre-fix code and record the result with "
                "`bf state set <id> --key regression_test_before --value FAILED`."
            )
    else:
        checks["new_test"] = {"passed": False, "reason": "no regression test recorded"}
        problems.append("no regression test — write one, and see it fail, before fixing.")

    # (b) the full suite
    suite = run_tests(cfg)
    checks["full_suite"] = {
        "passed": suite["passed"],
        "counts": suite["counts"],
        "output_tail": suite["output_tail"][-1500:],
    }
    if not suite["passed"]:
        problems.append(
            "the suite is red. Either the fix is wrong, or a test encoded the buggy "
            "behaviour as correct — that is a conversation with the reviewer, not "
            "something to rewrite quietly. Never edit a test to make it pass."
        )

    # (c) the browser reproduction, same script, unmodified
    script = run_dir / "repro.py"
    before_verdict_path = run_dir / "verdict-before.json"
    if script.exists() and before_verdict_path.exists():
        before_v = json.loads(before_verdict_path.read_text())
        if _script_changed(st, script):
            problems.append(
                "repro.py changed since the 'before' run. Changing the test to match "
                "the code looks identical to success — this verification is void."
            )
        after_v = await run_script(cfg, script, "after", ticket=ticket, headed=headed)
        checks["repro"] = {
            "symptom_before": before_v.get("symptom_detected"),
            "symptom_after": after_v.get("symptom_detected"),
            "passed": bool(before_v.get("symptom_detected")) and not after_v.get("symptom_detected"),
            "checks_after": after_v.get("checks"),
            "trace_ids_after": after_v.get("trace_ids"),
            "video_after": (after_v.get("artifacts") or {}).get("video"),
        }
        if not checks["repro"]["passed"]:
            problems.append(
                "the symptom is still present after the fix, or was never present "
                "before. Do not open a PR."
            )
    else:
        checks["repro"] = {"passed": False,
                           "reason": "no repro.py or no recorded 'before' run"}
        problems.append("no confirmed reproduction — you cannot claim the symptom is gone.")

    verified = all(c.get("passed") for c in checks.values())
    result = {
        "ticket": ticket,
        "checks": checks,
        "verdict": "VERIFIED" if verified else "NOT VERIFIED",
        "problems": problems,
        "fix_attempts_used": len(st.data.get("fix_attempts", [])),
        "fix_budget_left": st.fix_budget_left,
    }
    (run_dir / "verify.json").write_text(json.dumps(result, indent=2))
    if verified:
        st.phase("verified")
    return result


def _script_changed(st: State, script: Path) -> bool:
    import hashlib

    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    recorded = st.data.get("repro_script_sha")
    if recorded is None:
        st.set("repro_script_sha", digest)
        return False
    return recorded != digest


def snapshot_script(cfg, ticket: str) -> str:
    """Pin the repro script hash at 'before' time so tampering is detectable."""
    import hashlib

    run_dir = cfg.run_dir(ticket)
    script = run_dir / "repro.py"
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    State(run_dir, ticket).set("repro_script_sha", digest)
    return digest
