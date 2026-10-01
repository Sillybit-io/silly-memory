"""Management-project work-state adapter — focus + actions + meeting notes.

Read this first if you are new to the codebase:
  - Sources the snapshot from existing memory-bank files
    (``activeContext.md``, ``actionItems.md``) plus the global
    ``audienceContext.md`` for "key people".
  - Lists open action items only — completed ones (``- [x]``) are skipped
    so the agent's context doesn't fill with checked-off noise.
  - Caps each section to a small bullet count (5 for focus/people, 8 for
    open action items) to stay friendly to the context-pack token budget.

Public interface (imported elsewhere): ``ManagementAdapter``.
Depends on: paths (sibling), adapters.base.
Used by: adapters package init.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..paths import bank_path, global_store, workspace_store
from .base import WorkStateAdapter


class ManagementAdapter(WorkStateAdapter):
    name = "management"

    def snapshot(self, workspace_root: Path) -> str:
        store = workspace_store(workspace_root)
        lines = ["# Work State (management)", ""]

        active = bank_path(store, "activeContext.md")
        if active.exists():
            bullets = _top_bullets(active.read_text(encoding="utf-8"), 5)
            if bullets:
                lines.append("## Active focus")
                lines.extend(bullets)

        actions = bank_path(store, "actionItems.md")
        if actions.exists():
            open_items = _open_action_items(actions.read_text(encoding="utf-8"), 8)
            if open_items:
                lines.append("")
                lines.append("## Open action items")
                lines.extend(open_items)

        g_audience = global_store() / "memory-bank" / "audienceContext.md"
        if g_audience.exists():
            people = _top_bullets(g_audience.read_text(encoding="utf-8"), 5)
            if people:
                lines.append("")
                lines.append("## Key people (global)")
                lines.extend(people)

        notes_index = workspace_root / "meetingNotes" / "README.md"
        if notes_index.exists():
            lines.append("")
            lines.append("## Meeting notes index")
            lines.append(f"- See `{notes_index.relative_to(workspace_root)}`")

        return "\n".join(lines) + "\n"


def _top_bullets(text: str, n: int) -> list[str]:
    out = []
    for line in text.splitlines():
        if line.strip().startswith("- "):
            out.append(line.strip())
            if len(out) >= n:
                break
    return out


def _open_action_items(text: str, n: int) -> list[str]:
    out = []
    in_open = False
    for line in text.splitlines():
        if line.strip() == "## Open":
            in_open = True
            continue
        if line.startswith("## ") and in_open:
            break
        if in_open and line.strip().startswith("- [ ]"):
            out.append(line.strip())
            if len(out) >= n:
                break
    return out
