"""Config loader and home-directory resolver for the memory system.

Read this first if you are new to the codebase:
  - ``memory_home()`` returns the store root. The ``SILLY_MEMORY_HOME`` env
    var overrides it; the test suite uses it to isolate from the real store.
    Otherwise an installed engine is its own home (``~/.silly-memory`` after a
    normal install), and a source checkout defaults to ``~/.silly-memory``.
  - ``DEFAULT_CONFIG`` is the canonical set of tunables (token budgets,
    backend names, hook toggles). ``load_config()`` merges
    ``<memory_home>/config.json`` over the defaults — missing keys fall
    back, never raise.
  - The privacy invariant is ``MEMORY_ALLOW_NETWORK=0`` by default.
    ``memory_allow_network()`` exposes the current value so callers
    (doctor, backends, etc.) can refuse network operations consistently.

Public interface (imported elsewhere): ``DEFAULT_CONFIG``,
    ``MEMORY_ALLOW_NETWORK_ENV``, ``HOME_ENV``, ``memory_home``,
    ``load_config``, ``memory_allow_network``.
Depends on: stdlib only (json, os, pathlib, typing).
Used by: nearly every other module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

DEFAULT_CONFIG: dict[str, object] = {
    "memory_home": "~/.silly-memory",
    "observer_token_threshold": 6000,
    "reflector_token_threshold": 20000,
    "context_pack_token_cap": 16000,
    "daily_token_budget": 500000,
    "observe_model": "composer-2.5-fast",
    "reflect_model": "composer-2.5-fast",
    "auto_promote_confidence": 0.85,
    "git_snapshot_enabled": False,
    "git_snapshot_auto_commit": False,
    "observe_activity": False,
    "observe_agent_responses": True,
    "events_max_lines": 5000,
    "allow_network": "0",
    "mcp": False,
}

MEMORY_ALLOW_NETWORK_ENV = "MEMORY_ALLOW_NETWORK"
HOME_ENV = "SILLY_MEMORY_HOME"


def memory_home() -> Path:
    # SILLY_MEMORY_HOME overrides the default; the regression suite uses it to
    # fully isolate itself from the real store.
    env = os.environ.get(HOME_ENV)
    if env:
        return Path(env).expanduser().resolve()
    engine = Path(__file__).resolve().parents[3]
    # Why: an installed engine (VERSION beside bin/ and lib/) is its own home, so a hook
    # launched without the env var can never split the code from its data.
    if (engine / "VERSION").is_file():
        return engine
    return (Path.home() / ".silly-memory").resolve()


def load_config() -> dict[str, object]:
    path = memory_home() / "config.json"
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    with path.open(encoding="utf-8") as f:
        data = cast(object, json.load(f))
    if not isinstance(data, dict):
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update({str(key): value for key, value in cast(dict[object, object], data).items()})
    return merged


def memory_allow_network() -> str:
    return os.environ.get(MEMORY_ALLOW_NETWORK_ENV, str(DEFAULT_CONFIG["allow_network"]))
