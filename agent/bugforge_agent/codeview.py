"""Reading source at the sites telemetry implicates.

Anchors rather than line numbers: line numbers drift as soon as anyone edits the
file, and the whole point is that the robot is about to edit it.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


def show(repo: Path, file: str, anchor: str | None = None, line: int | None = None,
         context: int = 25) -> dict[str, Any]:
    path = (repo / file).resolve() if not Path(file).is_absolute() else Path(file)
    if not path.exists():
        raise SystemExit(f"no such file: {file}")
    lines = path.read_text(errors="replace").splitlines()

    hit = None
    if anchor:
        for i, text in enumerate(lines):
            if anchor in text:
                hit = i
                break
        if hit is None:
            rx = re.compile(anchor)
            for i, text in enumerate(lines):
                if rx.search(text):
                    hit = i
                    break
        if hit is None:
            return {"file": str(path), "anchor": anchor, "found": False,
                    "hint": "anchor not found — the code may have moved; try `bf code grep`"}
    elif line:
        hit = max(0, line - 1)

    if hit is None:
        start, end = 0, min(len(lines), context * 2)
    else:
        start, end = max(0, hit - context), min(len(lines), hit + context + 1)

    numbered = [f"{i + 1:5d}  {lines[i]}" for i in range(start, end)]
    return {
        "file": str(path.relative_to(repo)) if str(path).startswith(str(repo)) else str(path),
        "found": True,
        "anchor": anchor,
        "anchor_line": (hit + 1) if hit is not None else None,
        "range": [start + 1, end],
        "total_lines": len(lines),
        "text": "\n".join(numbered),
    }


def grep(repo: Path, pattern: str, glob: str | None = None,
         max_results: int = 60) -> dict[str, Any]:
    cmd = ["grep", "-rnI", "--exclude-dir=.git", "--exclude-dir=node_modules",
           "--exclude-dir=.bugforge", "--exclude-dir=__pycache__", "-E", pattern]
    if glob:
        cmd.insert(1, f"--include={glob}")
    cmd.append(".")
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    out = [ln for ln in (p.stdout or "").splitlines() if ln.strip()][:max_results]
    results = []
    for ln in out:
        parts = ln.split(":", 2)
        if len(parts) == 3:
            results.append({"file": parts[0].lstrip("./"), "line": int(parts[1]),
                            "text": parts[2].strip()[:220]})
    return {"pattern": pattern, "count": len(results), "results": results}
