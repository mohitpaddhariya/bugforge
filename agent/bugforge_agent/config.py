"""Configuration loading. The skill never hardcodes an endpoint; everything
resolves through here so drivers can be swapped without touching the loop."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAMES = ("bugforge.yaml", "bugforge.yml", ".bugforge.yaml")


def _find_config(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for d in [cur, *cur.parents]:
        for name in CONFIG_NAMES:
            p = d / name
            if p.is_file():
                return p
        # also look in agent/ so `bf` works from the repo root
        for name in CONFIG_NAMES:
            p = d / "agent" / name
            if p.is_file():
                return p
    return None


@dataclass
class Capability:
    driver: str
    url: str = ""
    opts: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, item: str) -> Any:  # opts accessible as attributes
        try:
            return self.opts[item]
        except KeyError as exc:
            raise AttributeError(item) from exc


@dataclass
class AppConfig:
    url: str = "http://localhost:3000"
    api_url: str = "http://localhost:8000"
    repo_path: str = "."
    test_cmd: str = "docker compose exec -T api pytest -q"
    control_plane: str | None = None
    personas_file: str | None = None


@dataclass
class Config:
    root: Path
    tickets: Capability
    telemetry: Capability
    vcs: Capability
    app: AppConfig
    runs_dir: Path

    @property
    def repo(self) -> Path:
        p = Path(self.app.repo_path)
        return p if p.is_absolute() else (self.root / p).resolve()

    def run_dir(self, ticket: str) -> Path:
        d = self.runs_dir / str(ticket)
        d.mkdir(parents=True, exist_ok=True)
        return d


def _cap(raw: dict[str, Any] | None, default_driver: str) -> Capability:
    raw = dict(raw or {})
    driver = raw.pop("driver", default_driver)
    url = raw.pop("url", "")
    return Capability(driver=driver, url=url.rstrip("/"), opts=raw)


def load(path: str | None = None) -> Config:
    cfg_path = Path(path) if path else _find_config()
    if cfg_path is None:
        raise SystemExit(
            "no bugforge.yaml found. Copy agent/bugforge.example.yaml to "
            "bugforge.yaml at the project root and edit it."
        )
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    root = cfg_path.parent if cfg_path.parent.name != "agent" else cfg_path.parent.parent

    app_raw = dict(raw.get("app") or {})
    app = AppConfig(
        url=app_raw.get("url", "http://localhost:3000"),
        api_url=app_raw.get("api_url", "http://localhost:8000"),
        repo_path=app_raw.get("repo_path", "."),
        test_cmd=app_raw.get("test_cmd", "docker compose exec -T api pytest -q"),
        control_plane=app_raw.get("control_plane"),
        personas_file=app_raw.get("personas_file"),
    )

    runs = raw.get("runs_dir") or ".bugforge/runs"
    runs_dir = Path(runs)
    if not runs_dir.is_absolute():
        runs_dir = root / runs_dir

    return Config(
        root=root,
        tickets=_cap(raw.get("tickets"), "supportdesk"),
        telemetry=_cap(raw.get("telemetry"), "bugforge"),
        vcs=_cap(raw.get("vcs"), "gitea"),
        app=app,
        runs_dir=runs_dir,
    )


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)
