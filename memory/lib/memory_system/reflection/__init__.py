"""Reflection subpackage — condense ``observations.md`` so the file stays small.

Read this first if you are new to the codebase:
  - Two reflectors live in parallel. ``reflector.py`` is the legacy
    47-line condenser. ``reflector_v2.py`` is the archive-then-condense
    successor — it ALWAYS archives the live observations to
    ``observations-archive/YYYY-MM.md`` before rewriting, so no evidence
    is ever lost.
  - Reflection is rate-limited by an unobserved-token estimate; if the
    workspace hasn't accumulated enough new observations, the reflector
    returns early without rewriting the file.
  - Callers explicitly choose v1 vs v2 by which entry point they import;
    a future task plans to retire v1.

Public interface (imported elsewhere): the submodules ``reflector`` and
    ``reflector_v2``.
Depends on: none directly — submodules import their own dependencies.
Used by: events.worker, bin/memory.
"""
