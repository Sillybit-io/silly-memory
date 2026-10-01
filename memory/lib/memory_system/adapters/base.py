"""Adapter base class and selection logic for workspace work-state snapshots.

Read this first if you are new to the codebase:
  - ``WorkStateAdapter`` is the contract every adapter must fulfil — exactly
    one method (``snapshot``) that returns a markdown work-state string.
  - ``pick_adapter()`` chooses between coding and management adapters by
    inspecting the workspace folder. The rule: ``.git`` AND no memory-bank/
    meetingNotes => coding; else => management.
  - The concrete subclasses are imported inside ``pick_adapter`` (not at the
    top of this file) so importing ``adapters.base`` from another sibling
    cannot trigger an import cycle.

Public interface (imported elsewhere): ``WorkStateAdapter``, ``pick_adapter``.
Depends on: adapters.coding, adapters.management (lazy, inside pick_adapter).
Used by: adapters package init, adapters.coding, adapters.management,
    recall.context_pack.
"""
from __future__ import annotations

# Why: every concrete adapter (coding/management) must promise a snapshot method — abstract base makes "forgot to implement it" a hard error at construction time, not at first call.
from abc import ABC, abstractmethod
from pathlib import Path


class WorkStateAdapter(ABC):
    name: str

    # Why: subclasses MUST override snapshot — leaving the base raising would silently allow no-op adapters.
    @abstractmethod
    def snapshot(self, workspace_root: Path) -> str:
        """Return markdown work-state snapshot."""


def pick_adapter(workspace_root: Path) -> WorkStateAdapter:
    from .coding import CodingAdapter
    from .management import ManagementAdapter

    git_dir = workspace_root / ".git"
    bank = workspace_root / "memory-bank"
    notes = workspace_root / "meetingNotes"
    if git_dir.exists() and not (bank.exists() or notes.exists()):
        return CodingAdapter()
    return ManagementAdapter()
