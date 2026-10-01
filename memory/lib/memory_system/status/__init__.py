"""Status subpackage — backs the ``memstatus`` dashboard CLI.

Read this first if you are new to the codebase:
  - Four files: ``banner.py`` (header + integrity glyph), ``main.py``
    (workspace dashboard, tasks/paths/workspaces views), ``markers.py``
    (first-run + post-upgrade marker files in ``memory_home``), and
    ``doctor_cache.py`` (5-minute TTL cache for the doctor's integrity
    verdict so memstatus stays fast).
  - The ``__init__`` re-exports the public symbols, so callers write
    ``from memory_system.status import …``.
  - ``memstatus`` must stay fast (warm-path p95 < 60ms). The TTL cache in
    ``doctor_cache`` is the lever that makes that possible — touching the
    real doctor on every call would blow the budget.

Public interface (imported elsewhere): everything re-exported in
    ``__all__`` (``render_status``, ``show_view``, ``iter_workspaces``,
    ``render_workspaces``, ``render_paths``, ``collect_tasks``,
    ``render_tasks_json``, ``render_tasks``, ``_parse_action_items``,
    plus the public banner/cache/marker constants).
Depends on: status.banner, status.doctor_cache, status.main, status.markers.
Used by: the ``memstatus`` shell helper and ``bin/memory status``.
"""
from __future__ import annotations

from memory_system.status.banner import _BANNER_BAR, _BANNER_PHRASE, render_status, render_status_json
from memory_system.status.doctor_cache import DOCTOR_CACHE, DOCTOR_CACHE_TTL_SEC, _doctor_cache_path, _read_doctor_cache, _write_doctor_cache
from memory_system.status.main import VIEWS, _field, _filter_items, _parse_action_items, _render_task_block, collect_tasks, iter_workspaces, render_paths, render_tasks, render_tasks_json, render_workspaces, show_view
from memory_system.status.markers import FIRST_RUN_MARKER, UPGRADE_MARKER, _consume_upgrade_marker, _first_run_marker_path, _is_first_run, _read_upgrade_marker, _upgrade_marker_path, _write_first_run_marker

__all__ = ["_BANNER_BAR", "_BANNER_PHRASE", "FIRST_RUN_MARKER", "UPGRADE_MARKER", "DOCTOR_CACHE", "DOCTOR_CACHE_TTL_SEC", "VIEWS", "render_status", "render_status_json", "show_view", "iter_workspaces", "render_workspaces", "render_paths", "collect_tasks", "render_tasks_json", "render_tasks", "_parse_action_items"]
