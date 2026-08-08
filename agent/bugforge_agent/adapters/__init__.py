"""Adapter registry.

The skill talks to four capabilities, never to a concrete service. Add a driver
here and the same triage loop works against a different stack.
"""
from __future__ import annotations

from typing import Any, Callable

_REGISTRY: dict[tuple[str, str], Callable[..., Any]] = {}


def register(capability: str, driver: str):
    def deco(cls):
        _REGISTRY[(capability, driver)] = cls
        return cls
    return deco


def build(capability: str, cap_cfg, *args, **kwargs):
    key = (capability, cap_cfg.driver)
    if key not in _REGISTRY:
        known = sorted(d for (c, d) in _REGISTRY if c == capability)
        raise SystemExit(
            f"no {capability} driver named {cap_cfg.driver!r}. Known: {known}. "
            f"See skills/bug-triage/references/adapters.md"
        )
    return _REGISTRY[key](cap_cfg, *args, **kwargs)


from . import tickets, telemetry, vcs  # noqa: E402,F401  (populates the registry)
