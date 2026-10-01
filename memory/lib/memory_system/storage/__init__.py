"""Storage subpackage — on-disk artifacts the rest of the system reads/writes.

Read this first if you are new to the codebase:
  - Two files: ``vector_store.py`` (numpy-backed cosine search persisted
    as ``vectors.npy`` + ``vectors.index.json``) and
    ``observations_archive.py`` (append-only monthly archive of every
    line ever written to ``observations.md``).
  - The vector store deliberately lazy-imports numpy so this package can
    be imported on systems without numpy installed — the embedding
    backend falls back to noop and FTS5 keeps working.
  - Every write goes through ``safety.atomic_write`` + ``safety.file_lock``;
    a direct ``open(..., "w")`` inside storage is a bug.

Public interface (imported elsewhere): the ``vector_store`` and
    ``observations_archive`` submodules.
Depends on: none directly — submodules import their own dependencies.
Used by: cli.cli_delete, cli.cli_inspect, recall.recall_hybrid,
    reflection.reflector_v2.
"""
