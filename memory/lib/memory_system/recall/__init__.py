"""Recall subpackage — what gets injected into Cursor as the memory context.

Read this first if you are new to the codebase:
  - Two generations live here in parallel. ``context_pack.py`` is the v1
    renderer that writes the ``_memory-context.mdc`` Cursor rule from
    bank + work-state. ``context_pack_v2.py`` is the topic-aware,
    score-weighted renderer; both ship while the v2 rollout completes.
  - ``recall_hybrid.py`` is the search engine behind ``memrecall``: it
    blends FTS5 keyword hits, dense vector hits, and sidecar scores into
    a single ranking. ``distiller.py`` turns observation bullets into
    durable bank lines.
  - All recall paths are read-mostly. Writes that DO happen
    (``context-pack.md``, ``work-state.md``, the generated rule file)
    go through ``safety.atomic_write``.

Public interface (imported elsewhere): the submodules ``context_pack``,
    ``context_pack_v2``, ``distiller``, ``recall_hybrid``.
Depends on: none directly — submodules import their own dependencies.
Used by: events.worker, bin/memory.
"""
