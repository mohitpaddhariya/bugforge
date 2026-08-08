"""`bf` — the deterministic half of the triage skill.

Everything two competent engineers would do identically lives here. Judgment lives
in SKILL.md. The model should never hand-write an HTTP request, parse a log format,
or drive Playwright by hand.

JSON on stdout by default; --pretty for humans. Exit codes: 0 success,
1 failure, 2 inconclusive.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from . import adapters, codeview, config, evidence, report, repro, verify
from .state import State

EXIT_OK, EXIT_FAIL, EXIT_INCONCLUSIVE = 0, 1, 2


# --------------------------------------------------------------------------
# output


def emit(data: Any, pretty: bool, human: str | None = None) -> None:
    if pretty and human is not None:
        print(human)
    elif pretty:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(json.dumps(data, default=str))


def _viewport(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    try:
        w, h = s.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise SystemExit(f"bad --viewport {s!r}; expected e.g. 390x844")


# --------------------------------------------------------------------------
# commands


def cmd_doctor(cfg, args) -> int:
    checks = []
    for name, cap in (("tickets", cfg.tickets), ("telemetry", cfg.telemetry),
                      ("vcs", cfg.vcs)):
        try:
            ok, detail = adapters.build(name, cap).health()
        except SystemExit as exc:
            ok, detail = False, str(exc)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        checks.append({"capability": name, "driver": cap.driver,
                       "url": cap.url, "ok": ok, "detail": detail})

    for name, url in (("app", cfg.app.url), ("api", cfg.app.api_url)):
        try:
            r = httpx.get(url, timeout=8, follow_redirects=True)
            checks.append({"capability": name, "driver": "http", "url": url,
                           "ok": r.status_code < 500, "detail": f"http {r.status_code}"})
        except Exception as exc:  # noqa: BLE001
            checks.append({"capability": name, "driver": "http", "url": url,
                           "ok": False, "detail": str(exc)})

    try:
        import playwright  # noqa: F401
        pw_ok, pw_detail = True, "installed"
    except ImportError:
        pw_ok, pw_detail = False, "not installed — `pip install playwright && playwright install chromium`"
    checks.append({"capability": "playwright", "driver": "-", "url": "",
                   "ok": pw_ok, "detail": pw_detail})

    try:
        import browser_use  # noqa: F401
        bu = "installed"
    except ImportError:
        bu = "not installed — `bf repro explore` falls back to scaffolding a script"
    checks.append({"capability": "browser-use", "driver": "-", "url": "",
                   "ok": True, "detail": bu})

    from . import evidence as _ev
    checks.append({"capability": "ffmpeg", "driver": "-", "url": "", "ok": True,
                   "detail": "installed" if _ev.have_ffmpeg()
                   else "not installed — PRs will have no before/after GIFs "
                        "(brew install ffmpeg)"})

    all_ok = all(c["ok"] for c in checks)
    result = {"ok": all_ok, "config_root": str(cfg.root), "checks": checks}
    human = "\n".join(
        f"{'✓' if c['ok'] else '✗'} {c['capability']:<12} {c['driver']:<14} "
        f"{c['url']:<32} {c['detail']}" for c in checks
    ) + ("\n\nAll good." if all_ok else "\n\nSomething is down — do not run the loop "
                                        "against a half-up system.")
    emit(result, args.pretty, human)
    return EXIT_OK if all_ok else EXIT_FAIL


def cmd_ticket(cfg, args) -> int:
    a = adapters.build("tickets", cfg.tickets)
    if args.ticket_cmd == "list":
        data = a.list()
        human = "\n".join(f"#{t['id']:<6} {t.get('status', ''):<8} "
                          f"{t.get('customer_email', ''):<26} {t['subject']}"
                          for t in data)
        emit(data, args.pretty, human)
    else:
        t = a.get(args.id)
        cfg.run_dir(str(args.id)).joinpath("ticket.json").write_text(
            json.dumps(t, indent=2))
        human = (f"#{t['id']}  {t['subject']}\n"
                 f"from: {t.get('customer_name', '')} <{t.get('customer_email', '')}>\n"
                 f"opened: {t.get('opened_at', '')}   "
                 f"device: {t.get('device', '?')} / {t.get('browser', '?')}\n"
                 f"\n{t.get('body', '')}\n")
        emit(t, args.pretty, human)
    return EXIT_OK


def cmd_telemetry(cfg, args) -> int:
    a = adapters.build("telemetry", cfg.telemetry)
    sub = args.telemetry_cmd

    if sub == "search":
        data = a.search(user=args.user, since=args.since, until=args.until,
                        level=args.level, kind=args.kind, name=args.name,
                        text=args.text, limit=args.limit)
        sessions = data.get("sessions", data if isinstance(data, list) else [])
        empty = ("(nothing found — widen the search before concluding anything; "
                 "frontend-only bugs produce no errors at all)")
        human = "\n".join(
            f"{s.get('session_id', '?'):<14} "
            f"{str(s.get('started_at', s.get('last_seen', '')))[:23]:<25} "
            f"errors={s.get('error_count', 0):<4} {s.get('summary', '')}"
            for s in sessions) or empty
        emit(data, args.pretty, human)
        return EXIT_OK if sessions else EXIT_INCONCLUSIVE

    if sub == "session":
        data = a.session(args.id)
    elif sub == "trace":
        data = a.trace(args.id)
    else:
        data = a.bundle(args.id)
        if args.ticket:
            cfg.run_dir(args.ticket).joinpath("bundle.json").write_text(
                json.dumps(data, indent=2))
            lines = data.get("rendered") or []
            cfg.run_dir(args.ticket).joinpath("timeline.txt").write_text("\n".join(lines))

    rendered = data.get("rendered") or []
    human = "\n".join(rendered)
    if data.get("summary"):
        human = f"{data['summary']}\n\n{human}"
    if data.get("stack_frames"):
        human += "\n\nstack frames:\n" + "\n".join(
            f"  {f.get('file')}:{f.get('line')}  {f.get('function', '')}"
            for f in data["stack_frames"])
    if data.get("implicated_files"):
        human += "\n\nimplicated files:\n" + "\n".join(
            f"  {f}" for f in data["implicated_files"])
    if data.get("_degraded"):
        human += f"\n\n⚠️  {data['_degraded']}"
    emit(data, args.pretty, human or None)
    return EXIT_OK


def cmd_code(cfg, args) -> int:
    if args.code_cmd == "show":
        data = codeview.show(cfg.repo, args.file, anchor=args.anchor,
                             line=args.line, context=args.context)
        human = data.get("text") or data.get("hint", "")
        if data.get("found"):
            human = f"{data['file']}  (anchor at line {data.get('anchor_line')})\n\n{human}"
        emit(data, args.pretty, human)
        return EXIT_OK if data.get("found") else EXIT_INCONCLUSIVE
    data = codeview.grep(cfg.repo, args.pattern, glob=args.glob)
    human = "\n".join(f"{r['file']}:{r['line']}  {r['text']}" for r in data["results"])
    emit(data, args.pretty, human or "(no matches)")
    return EXIT_OK


def _control_plane(cfg) -> str:
    if not cfg.app.control_plane:
        raise SystemExit(
            "no control_plane configured. Flags and reset are sandbox-only — real "
            "systems have no bug switches, and toggling flags in production is not "
            "triage. See references/adapters.md")
    return cfg.app.control_plane.rstrip("/")


def cmd_flags(cfg, args) -> int:
    base = _control_plane(cfg)
    if args.flags_cmd == "list":
        r = httpx.get(f"{base}/flags", timeout=15)
        r.raise_for_status()
        data = r.json()
        items = data.get("flags", data)
        human = "\n".join(f"{'ON ' if v else 'off'}  {k}" for k, v in sorted(
            (items.items() if isinstance(items, dict)
             else ((i["key"], i["enabled"]) for i in items))))
        emit(data, args.pretty, human)
        return EXIT_OK
    enabled = args.value.lower() in ("on", "true", "1", "yes")
    r = httpx.post(f"{base}/flags", json={"key": args.key, "enabled": enabled}, timeout=15)
    r.raise_for_status()
    emit({"key": args.key, "enabled": enabled}, args.pretty,
         f"{args.key} -> {'ON' if enabled else 'off'}")
    return EXIT_OK


def cmd_app(cfg, args) -> int:
    base = _control_plane(cfg)
    r = httpx.post(f"{base}/reset", timeout=300)
    r.raise_for_status()
    emit(r.json() if r.content else {"ok": True}, args.pretty, "app reset")
    return EXIT_OK


def cmd_repro(cfg, args) -> int:
    if args.repro_cmd == "explore":
        data = asyncio.run(repro.explore(
            cfg, str(args.ticket), args.goal, args.persona,
            _viewport(args.viewport) or (1440, 900), headed=args.headed))
        st = State(cfg.run_dir(str(args.ticket)), str(args.ticket))
        st.record_repro({"description": args.goal, "mode": data["mode"],
                         "outcome": "explored"})
        human = (f"{data['mode']}: {data['script']}\n\n"
                 f"{data.get('findings', data.get('reason', ''))}\n\n{data['next']}")
        emit(data, args.pretty, human)
        return EXIT_OK

    if args.repro_cmd == "scaffold":
        path = repro.scaffold(cfg.run_dir(str(args.ticket)), str(args.ticket),
                              args.goal or "", args.persona or "",
                              _viewport(args.viewport) or (1440, 900))
        emit({"script": str(path)}, args.pretty, f"wrote {path}")
        return EXIT_OK

    verdict = asyncio.run(repro.run_script(
        cfg, Path(args.script), args.label, ticket=args.ticket,
        headed=args.headed, viewport=_viewport(args.viewport), persona=args.persona))

    ticket = args.ticket or Path(args.script).parent.name
    st = State(cfg.run_dir(ticket), ticket)
    st.record_repro({"description": f"run {args.label}",
                     "outcome": "symptom present" if verdict["symptom_detected"]
                                else "no symptom"})
    if args.label == "before" and verdict["symptom_detected"]:
        verify.snapshot_script(cfg, ticket)
        st.phase("reproduced")

    lines = [f"{'PRESENT' if verdict['symptom_detected'] else 'ABSENT '}  "
             f"symptom ({args.label})"]
    for c in verdict["checks"]:
        lines.append(f"  {'✓' if c['present'] else '·'} {c['name']}"
                     + (f"  [error: {c['error']}]" if c.get("error") else ""))
    if verdict.get("script_error"):
        lines.append(f"  script error: {verdict['script_error']}")
    if verdict.get("warning"):
        lines.append(f"  ⚠️  {verdict['warning']}")
    lines.append(f"  traces: {', '.join(verdict['trace_ids']) or '(none)'}")
    lines.append(f"  video:  {verdict['artifacts']['video']}")
    lines.append("")
    lines.append("Compare your timeline against the customer's — if it differs you "
                 "reproduced a problem, not their problem:")
    for t in verdict["trace_ids"][:2]:
        lines.append(f"  bf telemetry bundle {t} --pretty")
    emit(verdict, args.pretty, "\n".join(lines))

    if args.label == "before":
        return EXIT_OK if verdict["symptom_detected"] else EXIT_INCONCLUSIVE
    return EXIT_OK if not verdict["symptom_detected"] else EXIT_FAIL


def cmd_test(cfg, args) -> int:
    data = verify.run_tests(cfg, only=args.only)
    human = (f"{data['cmd']}\nexit {data['exit_code']}  "
             f"{data['counts']}\n\n{data['output_tail'][-2000:]}")
    emit(data, args.pretty, human)
    return EXIT_OK if data["passed"] else EXIT_FAIL


def cmd_verify(cfg, args) -> int:
    data = asyncio.run(verify.verify(cfg, str(args.ticket), only=args.only,
                                     headed=args.headed))
    c = data["checks"]
    human = [f"verdict: {data['verdict']}", ""]
    for name in ("new_test", "full_suite", "repro"):
        ch = c.get(name, {})
        human.append(f"  {'✓' if ch.get('passed') else '✗'} {name}: "
                     f"{ {k: v for k, v in ch.items() if k not in ('output_tail', 'checks_after')} }")
    for p in data["problems"]:
        human.append(f"\n  ⚠️  {p}")
    emit(data, args.pretty, "\n".join(human))
    return EXIT_OK if data["verdict"] == "VERIFIED" else EXIT_FAIL


def cmd_state(cfg, args) -> int:
    ticket = str(args.ticket)
    st = State(cfg.run_dir(ticket), ticket)
    if args.state_cmd == "get":
        emit(st.data, True)
        return EXIT_OK
    if args.phase:
        st.phase(args.phase)
    if args.key:
        val: Any = args.value
        if val is not None and val.strip().startswith(("{", "[")):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                pass
        st.set(args.key, val)
    emit({"ticket": ticket, "phase": st.data["phase"],
          "repro_budget_left": st.repro_budget_left,
          "fix_budget_left": st.fix_budget_left}, args.pretty,
         f"{ticket}: phase={st.data['phase']} "
         f"repro_left={st.repro_budget_left} fix_left={st.fix_budget_left}")
    return EXIT_OK


def cmd_report(cfg, args) -> int:
    ticket = str(args.ticket)
    mode = "escalate" if args.escalate else (
        "working-as-intended" if args.working_as_intended else "fix")
    tj = cfg.run_dir(ticket) / "ticket.json"
    ticket_data = json.loads(tj.read_text()) if tj.exists() else None
    data = report.build(cfg, ticket, mode=mode, ticket_data=ticket_data)
    emit(data, args.pretty, data["markdown"])
    return EXIT_OK


def cmd_pr(cfg, args) -> int:
    ticket = str(args.ticket)
    run_dir = cfg.run_dir(ticket)
    st = State(run_dir, ticket)

    vpath = run_dir / "verify.json"
    if not args.force:
        if not vpath.exists():
            raise SystemExit("no verification on record. Run `bf verify --ticket "
                             f"{ticket}` first — a PR without it is a suggestion.")
        v = json.loads(vpath.read_text())
        if v["verdict"] != "VERIFIED":
            raise SystemExit(f"verification says {v['verdict']}. Do not open a PR.\n"
                             + "\n".join(f"  - {p}" for p in v.get("problems", [])))

    rpath = run_dir / "report.md"
    if not rpath.exists():
        report.build(cfg, ticket, mode="fix")
    body = rpath.read_text()
    title = body.splitlines()[0].lstrip("# ").strip()

    vcs = adapters.build("vcs", cfg.vcs)
    branch = args.branch or f"fix/ticket-{ticket}"

    # Turn the recordings into something a reviewer can actually look at.
    gifs: dict[str, Any] = {"gifs": {}, "problems": []}
    if not args.no_evidence:
        gifs = evidence.collect(run_dir)

    vcs.branch(cfg.repo, branch)
    sha = vcs.commit_all(cfg.repo, f"{title}\n\nCloses #{ticket}")

    # Drivers that can host an attachment upload after the PR exists; the rest
    # need the files on the branch before it is pushed.
    can_host = type(vcs).upload_asset is not adapters.vcs.VcsAdapter.upload_asset
    committed: dict[str, str] = {}
    if gifs["gifs"] and not can_host:
        committed = vcs.commit_assets(cfg.repo, branch,
                                      [Path(p) for p in gifs["gifs"].values()])

    vcs.push(cfg.repo, branch)
    pr = vcs.open_pr(cfg.repo, branch, title, body)

    urls: dict[str, str] = dict(committed)
    if gifs["gifs"] and can_host:
        for label, path in gifs["gifs"].items():
            url = vcs.upload_asset(pr, Path(path))
            if url:
                urls[label] = url
            else:
                gifs["problems"].append(f"{label}: upload failed")

    embedded = False
    if urls:
        block = evidence.embed_block(urls)
        marker = "\n**Verification**"
        new_body = (body.replace(marker, block + marker, 1)
                    if marker in body else body + block)
        embedded = vcs.update_pr_body(pr, new_body)
        if embedded:
            rpath.write_text(new_body)

    (run_dir / "pr.json").write_text(json.dumps(
        {**pr, "sha": sha, "branch": branch, "evidence": urls}, indent=2))
    st.phase("pr_opened")
    st.set("pr", pr)

    human = [f"opened: {pr.get('url')}", f"branch: {branch}", f"commit: {sha[:10]}"]
    if urls:
        human.append(f"evidence: {', '.join(urls)} "
                     f"({'embedded in the PR body' if embedded else 'uploaded, body not updated'})")
    else:
        human.append("evidence: none attached")
    for p in gifs["problems"]:
        human.append(f"  ⚠️  {p}")
    emit({**pr, "branch": branch, "sha": sha, "evidence": urls,
          "problems": gifs["problems"]}, args.pretty, "\n".join(human))
    return EXIT_OK


# --------------------------------------------------------------------------
# parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bf", description="bug triage: ticket → verified PR")
    p.add_argument("--config", help="path to bugforge.yaml")
    p.add_argument("--pretty", action="store_true", help="human-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check config and that every service is reachable")

    t = sub.add_parser("ticket").add_subparsers(dest="ticket_cmd", required=True)
    t.add_parser("list")
    tg = t.add_parser("get")
    tg.add_argument("id")

    tel = sub.add_parser("telemetry").add_subparsers(dest="telemetry_cmd", required=True)
    ts = tel.add_parser("search")
    ts.add_argument("--user")
    ts.add_argument("--since", default="7d")
    ts.add_argument("--until")
    ts.add_argument("--level")
    ts.add_argument("--kind")
    ts.add_argument("--name")
    ts.add_argument("--text")
    ts.add_argument("--limit", type=int, default=20)
    for name in ("session", "trace", "bundle"):
        s = tel.add_parser(name)
        s.add_argument("id")
        s.add_argument("--ticket", help="also save into the run directory")

    c = sub.add_parser("code").add_subparsers(dest="code_cmd", required=True)
    cs = c.add_parser("show")
    cs.add_argument("file")
    cs.add_argument("--anchor", help="distinctive substring; survives line drift")
    cs.add_argument("--line", type=int)
    cs.add_argument("--context", type=int, default=25)
    cg = c.add_parser("grep")
    cg.add_argument("pattern")
    cg.add_argument("--glob")

    f = sub.add_parser("flags").add_subparsers(dest="flags_cmd", required=True)
    f.add_parser("list")
    fs = f.add_parser("set")
    fs.add_argument("key")
    fs.add_argument("value", help="on|off")

    ap = sub.add_parser("app").add_subparsers(dest="app_cmd", required=True)
    ap.add_parser("reset")

    r = sub.add_parser("repro").add_subparsers(dest="repro_cmd", required=True)
    re_ = r.add_parser("explore")
    re_.add_argument("--ticket", required=True)
    re_.add_argument("--goal", required=True)
    re_.add_argument("--persona", default="")
    re_.add_argument("--viewport")
    re_.add_argument("--headed", action="store_true")
    rsc = r.add_parser("scaffold")
    rsc.add_argument("--ticket", required=True)
    rsc.add_argument("--goal")
    rsc.add_argument("--persona")
    rsc.add_argument("--viewport")
    rr = r.add_parser("run")
    rr.add_argument("script")
    rr.add_argument("--label", required=True, choices=["before", "after", "check"])
    rr.add_argument("--ticket")
    rr.add_argument("--persona")
    rr.add_argument("--viewport")
    rr.add_argument("--headed", action="store_true")

    te = sub.add_parser("test").add_subparsers(dest="test_cmd", required=True)
    ter = te.add_parser("run")
    ter.add_argument("--only")

    v = sub.add_parser("verify")
    v.add_argument("--ticket", required=True)
    v.add_argument("--only", help="path of the regression test")
    v.add_argument("--headed", action="store_true")

    st = sub.add_parser("state").add_subparsers(dest="state_cmd", required=True)
    sg = st.add_parser("get")
    sg.add_argument("ticket")
    ss = st.add_parser("set")
    ss.add_argument("ticket")
    ss.add_argument("--phase")
    ss.add_argument("--key")
    ss.add_argument("--value")

    rp = sub.add_parser("report")
    rp.add_argument("ticket")
    rp.add_argument("--escalate", action="store_true",
                    help="cannot reproduce — a successful outcome")
    rp.add_argument("--working-as-intended", action="store_true",
                    help="not a bug — a successful outcome")

    pr = sub.add_parser("pr").add_subparsers(dest="pr_cmd", required=True)
    pro = pr.add_parser("open")
    pro.add_argument("ticket")
    pro.add_argument("--branch")
    pro.add_argument("--force", action="store_true",
                     help="skip the verification gate (you should not)")
    pro.add_argument("--no-evidence", action="store_true",
                     help="skip video->GIF conversion and attachment")

    return p


DISPATCH = {
    "doctor": cmd_doctor, "ticket": cmd_ticket, "telemetry": cmd_telemetry,
    "code": cmd_code, "flags": cmd_flags, "app": cmd_app, "repro": cmd_repro,
    "test": cmd_test, "verify": cmd_verify, "state": cmd_state,
    "report": cmd_report, "pr": cmd_pr,
}


def _hoist_globals(argv: list[str]) -> tuple[list[str], list[str]]:
    """Let --pretty/--config appear anywhere.

    `bf ticket get 1042 --pretty` is how people actually type it; argparse only
    accepts global flags before the subcommand. Strip them out first.
    """
    globals_: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--pretty":
            globals_.append(a)
        elif a == "--config":
            globals_.extend(argv[i:i + 2])
            i += 1
        elif a.startswith("--config="):
            globals_.append(a)
        else:
            rest.append(a)
        i += 1
    return globals_, rest


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    g, rest = _hoist_globals(raw)
    args = build_parser().parse_args([*g, *rest])
    try:
        cfg = config.load(args.config)
        return DISPATCH[args.cmd](cfg, args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return EXIT_FAIL
        raise
    except httpx.HTTPError as exc:
        print(f"http error: {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
