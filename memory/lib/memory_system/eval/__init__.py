"""Evaluation subpackage — recall-quality baselines for the memory store.

Read this first if you are new to the codebase:
  - ``recall_baseline`` measures recall quality (MRR@10, Hit@3) and p50
    latency using either a seeded synthetic store or a read-only sweep of
    the user's real store — never writes to the real store.
  - It is stdlib-only and safe to run repeatedly in CI.

Public interface (imported elsewhere): the ``recall_baseline`` submodule.
Depends on: none directly — the submodule imports its own dependencies.
Used by: ``python3 -m memory_system.eval.recall_baseline`` (run by hand).
"""
