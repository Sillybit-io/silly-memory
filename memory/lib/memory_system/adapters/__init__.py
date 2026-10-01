"""Workspace adapters — auto-detect coding vs. management projects.

Read this first if you are new to the codebase:
  - The adapter decides what a "work state" snapshot looks like. Coding
    workspaces show git branch + dirty files; management workspaces show
    active focus + open action items.
  - ``pick_adapter(root)`` is the only entry point most callers need; it
    picks ``CodingAdapter`` when the workspace has a ``.git`` directory and
    no ``memory-bank``/``meetingNotes`` folders, else ``ManagementAdapter``.
  - This package is import-cycle safe: ``base.py`` defines the abstract
    class, and the two concrete subclasses are imported lazily inside
    ``pick_adapter`` so adding a new adapter doesn't change init order.

Public interface (imported elsewhere): ``WorkStateAdapter``, ``pick_adapter``,
    ``CodingAdapter``, ``ManagementAdapter``.
Depends on: adapters.base, adapters.coding, adapters.management.
Used by: recall.context_pack (via ``pick_adapter``).
"""
from .base import WorkStateAdapter, pick_adapter
from .coding import CodingAdapter
from .management import ManagementAdapter

__all__ = ["WorkStateAdapter", "pick_adapter", "CodingAdapter", "ManagementAdapter"]
