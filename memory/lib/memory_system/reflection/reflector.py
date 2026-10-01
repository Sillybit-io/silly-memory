"""Reflector v1 — naive condense of ``observations.md`` when it grows too long.

Read this first if you are new to the codebase:
  - Estimates token count by ``len(text) // 4`` and bails out unless the
    workspace's reflector token threshold is exceeded (or the caller
    passes ``force=True``). Cheap, deterministic, no ML involved.
  - When condensing, keeps the most recent ``max_lines`` bullet lines and
    drops the older ones — an irreversible action. This is the key
    weakness that motivated ``reflector_v2``, which archives the full
    history before discarding lines.
  - Kept in place for backwards compatibility while the v2 rollout
    completes. New callers should prefer ``reflector_v2.run_reflector_v2``.

Public interface (imported elsewhere): ``run_reflector``.
Depends on: system.config, paths, safety.
Used by: events.worker, ``bin/memory reflect``.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from ..paths import bank_path, ensure_layout, global_store, lock_path, workspace_store
from ..safety import atomic_write, file_lock
from ..system.config import load_config


def _estimate_obs_tokens(store: Path) -> int:
    obs = store / "observations.md"
    if not obs.exists():
        return 0
    return len(obs.read_text(encoding="utf-8")) // 4


def _condense_observations(text: str, max_lines: int = 40) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("- ")]
    if len(lines) <= max_lines:
        return text
    header = "# Observations\n\n## Condensed\n\n"
    kept = lines[-max_lines:]
    dropped = len(lines) - max_lines
    note = f"- 🟡 [{date.today().isoformat()}] #meta: Condensed {dropped} older observation lines into reflect pass.\n"
    return header + note + "\n".join(kept) + "\n"


def run_reflector(workspace_root: Path, force: bool = False) -> bool:
    cfg = load_config()
    store = workspace_store(workspace_root)
    ensure_layout(store)
    raw_threshold = cfg.get("reflector_token_threshold", 20000)
    threshold = int(raw_threshold) if isinstance(raw_threshold, (float, int, str)) else 20000
    if not force and _estimate_obs_tokens(store) < threshold:
        return False

    obs_path = store / "observations.md"
    if not obs_path.exists():
        return False
    text = obs_path.read_text(encoding="utf-8")
    condensed = _condense_observations(text)
    with file_lock(lock_path(store)):
        atomic_write(obs_path, condensed)
        atomic_write(store / ".reflect_state.json", json.dumps({"ts": date.today().isoformat()}, indent=2))
    return True
