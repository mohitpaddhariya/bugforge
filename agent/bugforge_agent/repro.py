"""Reproduction: explore with browser-use, emit a deterministic script, run it.

Exploration and proof need opposite properties. Exploration needs flexibility —
the ticket has no steps and no selectors. Proof needs determinism — "it worked the
second time" only means something if *it* was the same both times.

So exploration is a compiler: vague English in, deterministic Playwright out. The
emitted script is the artifact. It runs three times — to confirm the bug, to prove
the fix, and by the reviewer.
"""
from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path
from typing import Any

TEMPLATE = '''"""Reproduction for ticket {ticket}.

Goal: {goal}

Run it:
    bf repro run {path} --label before
    bf repro run {path} --label after

Symptom checks below must be TRUE while the bug exists and FALSE once it is fixed.
Do not edit this script to make the "after" run pass — that voids the verification.
"""
from bugforge_agent.reprolib import check

VIEWPORT = ({vw}, {vh})
PERSONA = "{persona}"

SYMPTOM_CHECKS = [
    # Specific and observable. 2-4 of them, together describing what the
    # customer experienced. Avoid "any console error" — it will pass after the
    # fix for unrelated reasons and tell you nothing.
{checks}
]


async def run(ctx):
{steps}
'''

DEFAULT_CHECKS = '''    # check("checkout_returns_500", lambda r: r.any_status(500)),
    # check("no_request_fired",     lambda r: r.no_request_to(r"/api/checkout")),
    # check("button_stuck",         lambda r: r.attr("place-order", "data-loading") == "true"),'''

DEFAULT_STEPS = '''    await ctx.login()
    await ctx.goto("/")
    # ... drive the flow with ctx.click / ctx.fill / ctx.goto
    # For a race, use raw parallel API calls — step-by-step UI actions cannot
    # hit one:
    #     await ctx.parallel(
    #         ctx.api("POST", "/api/checkout"),
    #         ctx.api("POST", "/api/checkout"),
    #     )
    await ctx.wait(500)'''


def scaffold(run_dir: Path, ticket: str, goal: str, persona: str,
             viewport: tuple[int, int], steps: str | None = None,
             checks: str | None = None) -> Path:
    path = run_dir / "repro.py"
    path.write_text(TEMPLATE.format(
        ticket=ticket, goal=goal, path=path, persona=persona,
        vw=viewport[0], vh=viewport[1],
        checks=checks or DEFAULT_CHECKS,
        steps=steps or DEFAULT_STEPS,
    ))
    return path


def _steps_from_history(history: Any) -> str | None:
    """Best-effort translation of a browser-use action history into ctx calls.

    Deliberately conservative: anything it cannot map confidently is emitted as
    a comment for the model to finish, rather than guessed at. A wrong step that
    looks right is worse than an obvious gap.
    """
    try:
        actions = history.model_actions()
    except Exception:  # noqa: BLE001
        return None

    lines: list[str] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        for name, params in a.items():
            p = params if isinstance(params, dict) else {}
            if name == "go_to_url":
                url = p.get("url", "")
                path = "/" + url.split("/", 3)[-1] if "://" in url else url
                lines.append(f'    await ctx.goto("{path}")')
            elif name in ("click_element", "click_element_by_index"):
                tid = p.get("data_testid") or p.get("testid")
                lines.append(f'    await ctx.click("{tid}")' if tid
                             else f'    # TODO click: {json.dumps(p)[:120]}')
            elif name in ("input_text", "type_text"):
                tid = p.get("data_testid") or p.get("testid")
                val = p.get("text", "")
                lines.append(f'    await ctx.fill("{tid}", "{val}")' if tid
                             else f'    # TODO fill: {json.dumps(p)[:120]}')
            elif name == "wait":
                lines.append(f'    await ctx.wait({int(p.get("seconds", 1) * 1000)})')
            elif name in ("done", "extract_content", "scroll_down", "scroll_up"):
                continue
            else:
                lines.append(f"    # unmapped browser-use action: {name}")
    return "\n".join(lines) if lines else None


async def explore(cfg, ticket: str, goal: str, persona: str,
                  viewport: tuple[int, int], headed: bool = False) -> dict[str, Any]:
    """Drive the app from a plain-English goal and emit a draft repro script.

    browser-use is optional. If it is not installed or has no model configured,
    this writes the scaffold and tells the caller to fill it in — the harness's
    own model can do that, which keeps the CLI free of an LLM dependency.
    """
    run_dir = cfg.run_dir(ticket)
    try:
        from browser_use import Agent, Browser  # type: ignore
        from browser_use.llm import ChatAnthropic  # type: ignore
    except Exception as exc:  # noqa: BLE001
        path = scaffold(run_dir, ticket, goal, persona, viewport)
        return {
            "mode": "scaffold",
            "reason": f"browser-use unavailable ({type(exc).__name__})",
            "script": str(path),
            "next": ("Fill in run() and SYMPTOM_CHECKS yourself, then "
                     f"`bf repro run {path} --label before`. See "
                     "skills/bug-triage/references/reproducing.md"),
        }

    task = textwrap.dedent(f"""
        You are reproducing a customer bug on a store at {cfg.app.url}.

        Goal: {goal}

        Log in as {persona} with password "password123" if login is needed.
        Interact using data-testid attributes where they exist.
        Work through the flow and report exactly what went wrong: what you clicked,
        what you expected, what actually happened, any error shown, and whether a
        network request appeared to fire at all.
        Do not attempt to fix anything.
    """).strip()

    browser = Browser(headless=not headed,
                      window_size={"width": viewport[0], "height": viewport[1]})
    agent = Agent(task=task, llm=ChatAnthropic(model="claude-sonnet-5"), browser=browser)
    history = await agent.run(max_steps=30)

    findings = history.final_result() if hasattr(history, "final_result") else str(history)
    steps = _steps_from_history(history)
    path = scaffold(run_dir, ticket, goal, persona, viewport, steps=steps)
    (run_dir / "exploration.md").write_text(f"# Exploration — ticket {ticket}\n\n"
                                            f"**Goal:** {goal}\n\n{findings}\n")
    return {
        "mode": "explored",
        "script": str(path),
        "findings": findings,
        "steps_translated": bool(steps),
        "next": ("Review the emitted script — the exploration is scaffolding, the "
                 "script is the artifact. Add SYMPTOM_CHECKS, then "
                 f"`bf repro run {path} --label before`"),
    }


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("repro_script", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load repro script: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def run_script(cfg, script: Path, label: str, ticket: str | None = None,
                     headed: bool = False, viewport: tuple[int, int] | None = None,
                     persona: str | None = None) -> dict[str, Any]:
    from .reprolib import execute

    script = Path(script)
    if not script.exists():
        raise SystemExit(f"no repro script at {script}")
    mod = load_module(script)
    out_dir = script.parent
    verdict = await execute(mod, label=label, out_dir=out_dir,
                            base_url=cfg.app.url, api_url=cfg.app.api_url,
                            headed=headed, viewport=viewport, persona=persona)
    verdict["script"] = str(script)
    if not getattr(mod, "SYMPTOM_CHECKS", []):
        verdict["warning"] = ("no SYMPTOM_CHECKS declared — this script performs "
                              "actions but proves nothing. Add checks before relying "
                              "on it for verification.")
    (out_dir / f"verdict-{label}.json").write_text(json.dumps(verdict, indent=2))
    return verdict
