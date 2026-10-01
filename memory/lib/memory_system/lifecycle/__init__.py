"""Memory lifecycle subpackage — scoring sidecars, decay, compaction, backup, export.

Read this first if you are new to the codebase:
  - The core data model is the per-bank-file ``.score.json`` sidecar
    written by ``scoring.py``. Decay (``decay.py``) reads/writes that
    sidecar via a single lock; compaction (``compaction.py``) only
    PROPOSES merges (it never deletes); ``backup.py`` takes on-disk store
    snapshots, and ``export_bundle.py`` exports and imports whole homes.
  - All sidecar writes go through ``safety.atomic_write`` + ``file_lock``.
    Direct sidecar mutation outside ``scoring.update_score`` is a bug.
  - The markdown bank format is sacred — none of these modules change
    bullet text; they only add metadata next to it.

Public interface (imported elsewhere): the submodules ``scoring``,
    ``decay``, ``compaction``, ``backup``, ``export_bundle``.
Depends on: none directly — submodules import their own dependencies.
Used by: cli.cli_delete, cli.cli_inspect, learning.reinforcement,
    recall.context_pack_v2, recall.recall_hybrid, bin/memory.
"""
