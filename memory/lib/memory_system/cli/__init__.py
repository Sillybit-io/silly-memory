"""CLI subpackage — real implementations of the ``mem*`` commands.

Read this first if you are new to the codebase:
  - Each file in this folder is a thin command runner: ``cli_delete``,
    ``cli_inspect``, ``cli_learn_status``, and ``doctor``.
  - All CLIs are read-only EXCEPT ``cli_delete``, which mutates bank files,
    sidecars, and the vector store under a single advisory lock.

Public interface (imported elsewhere): ``cli_delete``, ``cli_inspect``,
    ``cli_learn_status``, ``doctor`` submodules.
Depends on: none directly — submodules import their own dependencies.
Used by: ``bin/memory`` and ``status.doctor_cache``.
"""
