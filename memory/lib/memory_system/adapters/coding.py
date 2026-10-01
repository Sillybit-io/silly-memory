"""Coding-project work-state adapter — branch + dirty-file snapshot from git.

Read this first if you are new to the codebase:
  - The snapshot returns a tiny markdown block: current branch, up to 20
    dirty files (or "Working tree: clean"), or "Git: unavailable".
  - All ``git`` calls run with a 5-second timeout so a hung repo never
    stalls the snapshot. ``FileNotFoundError`` (no git binary) and
    ``TimeoutExpired`` both degrade to "Git: unavailable" silently.
  - The dirty-file list is hard-capped at 20 entries to keep the context
    pack from drowning in a noisy ``git status``.

Public interface (imported elsewhere): ``CodingAdapter``.
Depends on: adapters.base.
Used by: adapters package init.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .base import WorkStateAdapter


class CodingAdapter(WorkStateAdapter):
    name = "coding"

    def snapshot(self, workspace_root: Path) -> str:
        lines = ["# Work State (coding)", ""]
        try:
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if branch.returncode == 0:
                lines.append(f"- Branch: `{branch.stdout.strip()}`")
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=workspace_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if status.returncode == 0 and status.stdout.strip():
                dirty = status.stdout.strip().splitlines()[:20]
                lines.append("- Dirty files:")
                for d in dirty:
                    lines.append(f"  - `{d}`")
            else:
                lines.append("- Working tree: clean")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            lines.append("- Git: unavailable")
        return "\n".join(lines) + "\n"
