"""Turning run artifacts into things a reviewer can actually look at.

A PR that *references* a video on someone else's disk is not evidence. The
before/after pair has to be embedded in the page, which means a small, portable,
inline-renderable file — a GIF, not a webm.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

# GitHub/Gitea both choke well before this; keep the pair comfortably under it.
MAX_BYTES = 9_000_000


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _duration_seconds(src: Path) -> float | None:
    """Real duration, decoded rather than read from the header.

    Playwright's webm carries no reliable duration metadata, which makes
    ``-sseof`` seek to the wrong place — it silently produced a GIF of the
    login page instead of the ending. Counting packets is slower and correct.
    """
    if not shutil.which("ffprobe"):
        return None
    for args in (
        ["-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(src)],
        ["-v", "error", "-count_packets", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(src)],
    ):
        try:
            out = subprocess.run(["ffprobe", *args], capture_output=True,
                                 text=True, timeout=60).stdout.strip().splitlines()
            for line in out:
                value = float(line)
                if value > 0:
                    return value
        except (ValueError, subprocess.SubprocessError):
            continue
    return None


def webm_to_gif(src: Path, dst: Path, *, width: int = 720, fps: int = 10,
                max_seconds: int | None = 24, tail: bool = True) -> dict[str, Any]:
    """Two-pass palette conversion — a one-pass GIF of a UI recording looks like mud.

    ``tail=True`` takes the LAST ``max_seconds`` rather than the first. A
    reproduction spends most of its runtime on setup; the frames worth showing
    are the ones at the end, where the symptom either appears or does not.
    Trimming from the front produces a GIF of the login page.

    Retries at progressively lower fidelity if the result is too large to embed.
    """
    src, dst = Path(src), Path(dst)
    if not src.exists():
        return {"ok": False, "reason": f"no such video: {src}"}
    if not have_ffmpeg():
        return {"ok": False, "reason": "ffmpeg not installed (brew install ffmpeg)"}

    dst.parent.mkdir(parents=True, exist_ok=True)
    palette = dst.with_suffix(".palette.png")

    ladder = [(width, fps), (int(width * 0.75), fps), (int(width * 0.6), max(6, fps - 4))]
    last_err = ""

    for w, f in ladder:
        vf = f"fps={f},scale={w}:-1:flags=lanczos"
        if max_seconds and tail:
            duration = _duration_seconds(src)
            start = max(0.0, (duration or 0.0) - max_seconds)
            trim = ["-ss", f"{start:.2f}", "-t", str(max_seconds)]
        elif max_seconds:
            trim = ["-t", str(max_seconds)]
        else:
            trim = []
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", *trim, "-i", str(src),
                 "-vf", f"{vf},palettegen=stats_mode=diff", str(palette)],
                check=True, capture_output=True, timeout=180)
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", *trim, "-i", str(src),
                 "-i", str(palette), "-lavfi",
                 f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",
                 "-loop", "0", str(dst)],
                check=True, capture_output=True, timeout=180)
        except subprocess.CalledProcessError as exc:
            last_err = (exc.stderr or b"").decode(errors="replace")[-300:]
            continue
        except subprocess.TimeoutExpired:
            last_err = "ffmpeg timed out"
            continue

        size = dst.stat().st_size
        if size <= MAX_BYTES:
            palette.unlink(missing_ok=True)
            return {"ok": True, "path": str(dst), "bytes": size, "width": w, "fps": f}

    palette.unlink(missing_ok=True)
    if dst.exists():
        return {"ok": True, "path": str(dst), "bytes": dst.stat().st_size,
                "warning": "still large after downscaling; it may not render inline"}
    return {"ok": False, "reason": last_err or "ffmpeg produced nothing"}


def collect(run_dir: Path) -> dict[str, Any]:
    """Convert whatever the before/after runs recorded into embeddable GIFs."""
    run_dir = Path(run_dir)
    out: dict[str, Any] = {"gifs": {}, "problems": []}
    for label in ("before", "after"):
        videos = sorted((run_dir / label / "video").glob("*.webm"),
                        key=lambda v: v.stat().st_mtime, reverse=True)
        if not videos:
            out["problems"].append(f"no {label} video — was `bf repro run --label {label}` run?")
            continue
        res = webm_to_gif(videos[0], run_dir / "evidence" / f"{label}.gif")
        if res.get("ok"):
            out["gifs"][label] = res["path"]
            if res.get("warning"):
                out["problems"].append(f"{label}: {res['warning']}")
        else:
            out["problems"].append(f"{label}: {res['reason']}")
    return out


def embed_block(urls: dict[str, str]) -> str:
    """The before/after pair, side by side where the host renders HTML."""
    if not urls:
        return ""
    if "before" in urls and "after" in urls:
        return (
            "\n**Before / after**\n\n"
            "<table><tr>"
            "<td width=\"50%\"><b>Before</b> — symptom present<br>"
            f"<img src=\"{urls['before']}\" alt=\"before the fix\"></td>"
            "<td width=\"50%\"><b>After</b> — symptom gone<br>"
            f"<img src=\"{urls['after']}\" alt=\"after the fix\"></td>"
            "</tr></table>\n"
        )
    label, url = next(iter(urls.items()))
    return f"\n**{label.capitalize()}**\n\n![{label}]({url})\n"
