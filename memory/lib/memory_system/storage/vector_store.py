"""Numpy-backed vector store with cosine similarity search.

Read this first if you are new to the codebase:
  - Per-workspace persistence at ``<store>/vectors.npy`` and
    ``<store>/vectors.index.json``. The JSON sidecar maps row positions
    to stable entry ids so vectors can be added, removed, and searched
    without loading sister metadata from SQLite.
  - numpy is allowlisted but lazy-imported inside method bodies so
    ``memory_system`` can import on systems where numpy is not installed
    and fall back to the noop embedding backend.
  - All file writes go through ``safety.atomic_write`` + ``safety.file_lock``
    using a dedicated ``vectors.lock`` so vector edits cannot interleave
    with each other or with concurrent recall reads.

Public interface (imported elsewhere): ``VectorStore``,
    ``VECTORS_FILENAME``, ``INDEX_FILENAME``, ``LOCK_FILENAME``.
Depends on: safety; numpy is lazy-imported at call time only.
Used by: cli.cli_delete, cli.cli_inspect, recall.recall_hybrid.
"""

from __future__ import annotations

import json
import importlib
import os
from pathlib import Path
# Why: Protocol lets us describe what we need from an embedding backend without forcing tests to subclass anything.
from typing import Any, Protocol

from memory_system.safety import atomic_write, file_lock

VECTORS_FILENAME = "vectors.npy"
INDEX_FILENAME = "vectors.index.json"
LOCK_FILENAME = "vectors.lock"


def _numpy() -> Any:
    return importlib.import_module("numpy")


# Why: pin down the exact attributes the vector store reaches into so backend swaps stay safe.
class _EmbeddingBackendLike(Protocol):
    dim: int

    def encode(self, texts: list[str]) -> list[list[float]]: ...


def _vectors_path(store: Path) -> Path:
    return Path(store) / VECTORS_FILENAME


def _index_path(store: Path) -> Path:
    return Path(store) / INDEX_FILENAME


def _lock_path(store: Path) -> Path:
    return Path(store) / LOCK_FILENAME


def _write_npy_atomic(path: Path, arr: Any) -> None:
    numpy = _numpy()

    tmp = path.with_name(path.name + ".partial")
    with tmp.open("wb") as fh:
        numpy.save(fh, arr)
    os.rename(tmp, path)


def _write_index_atomic(path: Path, dim: int | None, ids: list[str]) -> None:
    payload: dict[str, Any] = {"dim": dim, "ids": ids}
    atomic_write(path, json.dumps(payload, indent=2) + "\n")


class VectorStore:
    """Cosine-similarity vector store rooted at a workspace store directory."""

    def __init__(self, store: Path) -> None:
        self._store: Path = Path(store)
        self._store.mkdir(parents=True, exist_ok=True)

    def _load(self) -> tuple[list[str], Any | None, int | None]:
        idx_p = _index_path(self._store)
        vec_p = _vectors_path(self._store)
        if not idx_p.exists() or not vec_p.exists():
            return [], None, None
        numpy = _numpy()
        with idx_p.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        ids: list[str] = list(payload.get("ids", []))
        dim_raw = payload.get("dim")
        dim: int | None = int(dim_raw) if isinstance(dim_raw, int) else None
        with vec_p.open("rb") as fh:
            arr = numpy.load(fh)
        return ids, arr.astype(numpy.float32, copy=False), dim

    def _persist(self, ids: list[str], arr: Any, dim: int | None) -> None:
        _write_npy_atomic(_vectors_path(self._store), arr)
        _write_index_atomic(_index_path(self._store), dim, ids)

    def add(self, entry_id: str, vector: list[float]) -> None:
        numpy = _numpy()

        with file_lock(_lock_path(self._store)):
            ids, arr, dim = self._load()
            vec = numpy.asarray(vector, dtype=numpy.float32)
            if vec.ndim != 1:
                raise ValueError(f"vector must be 1-D, got shape {vec.shape}")
            new_dim = int(vec.shape[0])
            if dim is None:
                dim = new_dim
                arr = numpy.zeros((0, dim), dtype=numpy.float32)
            if new_dim != dim:
                raise ValueError(
                    f"vector dim {new_dim} does not match store dim {dim}"
                )
            if arr is None:
                arr = numpy.zeros((0, dim), dtype=numpy.float32)

            if entry_id in ids:
                row = ids.index(entry_id)
                arr = arr.copy()
                arr[row] = vec
            else:
                ids = ids + [entry_id]
                arr = numpy.vstack([arr, vec.reshape(1, -1)])

            self._persist(ids, arr, dim)

    def remove(self, entry_id: str) -> None:
        with file_lock(_lock_path(self._store)):
            ids, arr, dim = self._load()
            if entry_id not in ids or arr is None:
                return
            numpy = _numpy()
            row = ids.index(entry_id)
            ids = ids[:row] + ids[row + 1 :]
            arr = numpy.delete(arr, row, axis=0)
            self._persist(ids, arr, dim)

    def search(self, query_vec: list[float], k: int) -> list[tuple[str, float]]:
        numpy = _numpy()

        ids, arr, dim = self._load()
        if not ids or arr is None or arr.size == 0:
            return []
        q = numpy.asarray(query_vec, dtype=numpy.float32)
        if q.ndim != 1 or (dim is not None and q.shape[0] != dim):
            return []
        q_norm = float(numpy.linalg.norm(q))
        if q_norm == 0.0:
            return []

        v_norms = numpy.linalg.norm(arr, axis=1)
        safe = v_norms > 0
        if not bool(safe.any()):
            return []

        sims = numpy.zeros(arr.shape[0], dtype=numpy.float32)
        sims[safe] = (arr[safe] @ q) / (v_norms[safe] * q_norm)
        order = numpy.argsort(-sims)

        out: list[tuple[str, float]] = []
        for i in order:
            idx = int(i)
            if not bool(safe[idx]):
                continue
            out.append((ids[idx], float(sims[idx])))
            if len(out) >= k:
                break
        return out


def _bank_entries(store: Path) -> list[tuple[str, str]]:
    bank_dir = Path(store) / "memory-bank"
    if not bank_dir.exists():
        return []
    entries: list[tuple[str, str]] = []
    for md in sorted(bank_dir.glob("*.md")):
        for lineno, line in enumerate(
            md.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- "):
                entries.append((f"{md.name}:{lineno}", stripped[2:].strip()))
    return entries


def rebuild_from_bank(store: Path, embedding_backend: _EmbeddingBackendLike) -> int:
    """Re-encode all bullet lines under ``store/memory-bank`` via the backend.

    Replaces any existing vector store contents. Returns the number of entries
    indexed.
    """
    numpy = _numpy()

    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)

    backend_dim = int(embedding_backend.dim)
    entries = _bank_entries(store)
    ids = [eid for eid, _ in entries]
    texts = [text for _, text in entries]

    if texts:
        raw = embedding_backend.encode(texts)
        arr = numpy.asarray(raw, dtype=numpy.float32)
        if arr.ndim != 2 or arr.shape[1] != backend_dim:
            raise ValueError(
                f"embedding backend returned shape {arr.shape}, "
                f"expected (*, {backend_dim})"
            )
    else:
        arr = numpy.zeros((0, backend_dim), dtype=numpy.float32)

    with file_lock(_lock_path(store)):
        _write_npy_atomic(_vectors_path(store), arr)
        _write_index_atomic(_index_path(store), backend_dim, ids)

    return len(ids)
