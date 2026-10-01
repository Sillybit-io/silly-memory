"""Hybrid recall — blends FTS5 keyword, dense vector, and sidecar score into one rank.

Read this first if you are new to the codebase:
  - The ranker is a weighted sum, ``DEFAULT_WEIGHTS = {fts5: 0.4,
    dense: 0.4, score: 0.2}``. Weights are tunable per call so tests or
    benchmarks can isolate any single signal.
  - Two candidate pools are merged. FTS5 ``recall(query)`` returns the
    keyword hits; the dense pool comes from the vector store's cosine
    search against the embedded query. We over-fetch
    ``CANDIDATE_POOL_SIZE=50`` from each before re-ranking, so a single
    weak signal cannot starve the final list.
  - Embedding backends are described by a tiny ``_EmbeddingBackendLike``
    shape (``is_available``, ``encode``); tests inject a stub there
    instead of standing up sentence-transformers.
  - ``search_all`` searches every tracked workspace store plus ``_global``
    with one shared backend, labels each hit with its workspace, and only
    then applies ``limit``. It never skips a store: one that cannot be read
    raises ``StoreSearchError`` naming it.

Public interface (imported elsewhere): ``hybrid_recall``, ``search_all``,
    ``StoreSearchError``, ``DEFAULT_WEIGHTS``, ``CANDIDATE_POOL_SIZE``, ``LOG``.
Depends on: system.config, index, lifecycle.scoring, storage.vector_store.
Used by: bin/memory (``recall --all``) and mcp_server (``search_all``);
    cli.cli_inspect re-uses ``DEFAULT_WEIGHTS`` for ``memwhy``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
# Why: Protocol/TypedDict/cast keep hybrid recall pluggable — any embedding object that quacks right is accepted, and rows from FTS5 are shaped before scoring.
from typing import Protocol, TypedDict, cast

from memory_system.system.config import load_config, memory_home
from memory_system.index import db_path, rebuild_index, recall
from memory_system.lifecycle.scoring import DEFAULT_SCORE, load_scores
from memory_system.storage.vector_store import VectorStore

LOG = logging.getLogger("memory_system.recall.recall_hybrid")

DEFAULT_WEIGHTS = {"fts5": 0.4, "dense": 0.4, "score": 0.2}
CANDIDATE_POOL_SIZE = 50


class _Entry(TypedDict):
    entry_id: str
    scope: str
    path: str
    section: str
    snippet: str


# Why: only the two methods recall actually uses are declared, so a tiny stub object works in tests.
class _EmbeddingBackendLike(Protocol):
    def is_available(self) -> bool: ...

    def encode(self, texts: list[str]) -> list[list[float]]: ...


def hybrid_recall(
    store: Path,
    query: str,
    *,
    backend: _EmbeddingBackendLike,
    limit: int = 10,
) -> list[dict[str, object]]:
    store = Path(store)
    top_k = max(0, int(limit))
    if top_k == 0:
        return []

    entries = _load_bank_entries(store)
    sidecar_scores = _load_sidecar_scores(store)
    weights = _load_weights(store)

    fts_rows = recall(store, query, limit=CANDIDATE_POOL_SIZE)
    candidates: dict[str, dict[str, object]] = {}
    fts_raw: dict[str, float] = {}

    for rank, row in enumerate(fts_rows, start=1):
        score = 1.0 / float(rank)
        ids = _entry_ids_for_fts_row(row, entries)
        if not ids:
            ids = [_synthetic_fts_id(row)]
        for entry_id in ids:
            candidate = _candidate_from_entry_or_row(entry_id, entries, row)
            _ = candidates.setdefault(entry_id, candidate)
            fts_raw[entry_id] = max(fts_raw.get(entry_id, 0.0), score)

    dense_raw: dict[str, float] = {}
    if not backend.is_available():
        LOG.warning("Hybrid recall falling back to FTS5-only: embedding backend unavailable")
        weights = {"fts5": 1.0, "dense": 0.0, "score": 0.0}
    else:
        try:
            encoded = backend.encode([query])
        except Exception as exc:
            LOG.warning("Hybrid recall falling back to FTS5-only: query embedding failed: %s", exc)
            weights = {"fts5": 1.0, "dense": 0.0, "score": 0.0}
        else:
            query_vec = encoded[0] if encoded else []
            dense_hits = VectorStore(store).search(query_vec, k=CANDIDATE_POOL_SIZE)
            if not dense_hits:
                LOG.warning("Hybrid recall falling back to FTS5-only: vector store empty or incompatible")
                weights = {"fts5": 1.0, "dense": 0.0, "score": 0.0}
            else:
                for entry_id, sim in dense_hits:
                    if entry_id not in entries:
                        continue
                    _ = candidates.setdefault(entry_id, _candidate_from_entry(entry_id, entries))
                    dense_raw[entry_id] = float(sim)

    score_raw = {
        entry_id: _entry_score(entry_id, sidecar_scores)
        for entry_id in candidates
    }

    fts_norm = _minmax({entry_id: fts_raw.get(entry_id, 0.0) for entry_id in candidates})
    dense_norm = _minmax({entry_id: dense_raw.get(entry_id, 0.0) for entry_id in candidates})
    sidecar_norm = _minmax(score_raw)
    active_weights = _renormalize(weights)

    ranked: list[dict[str, object]] = []
    for entry_id, candidate in candidates.items():
        signals = {
            "fts5": fts_norm.get(entry_id, 0.0),
            "dense": dense_norm.get(entry_id, 0.0),
            "score": sidecar_norm.get(entry_id, 0.0),
        }
        composite = sum(signals[name] * active_weights.get(name, 0.0) for name in signals)
        item: dict[str, object] = dict(candidate)
        item["score"] = round(float(composite), 6)
        item["signals"] = signals
        item["weights"] = active_weights
        ranked.append(item)

    ranked.sort(key=_rank_sort_key)
    return ranked[:top_k]


class StoreSearchError(RuntimeError):
    """A store that ``search_all`` must search could not be read."""


def _searchable(store: Path) -> bool:
    return db_path(store).exists() or (store / "memory-bank").is_dir() or (store / "observations.md").exists()


def _fts_hits(store: Path, query: str, limit: int) -> list[dict[str, object]]:
    rows = recall(store, query, limit=limit)
    return [{**row, "score": round(1.0 / rank, 6)} for rank, row in enumerate(rows, start=1)]


def _search_store(
    store: Path,
    query: str,
    backend: _EmbeddingBackendLike,
    dense: bool,
    limit: int,
) -> list[dict[str, object]]:
    def run() -> list[dict[str, object]]:
        if dense:
            return hybrid_recall(store, query, backend=backend, limit=limit)
        return _fts_hits(store, query, limit)

    try:
        try:
            return run()
        except sqlite3.OperationalError:
            # A damaged index is rebuilt once and searched again, as index.recall_all does.
            _ = rebuild_index(store, store.name)
            return run()
    except (sqlite3.Error, OSError) as exc:
        raise StoreSearchError(f"cannot search store {store.name} at {store}: {exc}") from exc


def _all_sort_key(hit: dict[str, object]) -> tuple[float, str, str, str]:
    score = hit.get("score", 0.0)
    numeric = float(score) if isinstance(score, (int, float)) else 0.0
    entry = hit.get("entry_id") or f"{hit.get('path', '')}:{hit.get('section', '')}"
    return (-numeric, str(hit["workspace_id"]), str(hit.get("path", "")), str(entry))


def search_all(query: str, limit: int = 10) -> list[dict[str, object]]:
    """Search every tracked workspace store plus ``_global``; best ``limit`` hits overall.

    Each hit keeps its recall fields and gains ``workspace_id``,
    ``workspace_root`` (None when unknown), and ``workspace_label`` (the root,
    else the store id; ``_global`` for the global store). Stores holding no
    bank, index, or observations have nothing to search and are passed over.
    """
    from memory_system.backends import factory
    from memory_system.paths import GLOBAL_ID
    from memory_system.status.main import iter_workspaces

    top_k = max(0, int(limit))
    home = memory_home()
    try:
        rows = iter_workspaces()
    except OSError as exc:
        raise StoreSearchError(f"cannot list the workspace stores in {home}: {exc}") from exc
    targets: list[tuple[Path, str, str | None]] = []
    for row in rows:
        root = str(row.get("root") or "")
        targets.append((home / str(row["id"]), str(row["id"]), root if root and root != "(unknown)" else None))
    targets.append((home / GLOBAL_ID, GLOBAL_ID, None))
    targets = [target for target in targets if _searchable(target[0])]
    if top_k == 0 or not targets:
        return []

    backend = cast(_EmbeddingBackendLike, factory.get_embedding_backend(load_config()))
    # The noop backend reports itself available but has no vectors, so it gets keyword-only search.
    dense = getattr(backend, "name", "") != "noop" and backend.is_available()
    hits: list[dict[str, object]] = []
    for store, workspace_id, root in targets:
        label = GLOBAL_ID if workspace_id == GLOBAL_ID else (root or workspace_id)
        for hit in _search_store(store, query, backend, dense, top_k):
            hits.append({**hit, "workspace_id": workspace_id, "workspace_root": root, "workspace_label": label})
    hits.sort(key=_all_sort_key)
    return hits[:top_k]


def _load_weights(store: Path) -> dict[str, float]:
    cfg = cast(dict[str, object], dict(load_config()))
    store_cfg = store / "config.json"
    if store_cfg.exists():
        try:
            loaded = cast(object, json.loads(store_cfg.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            store_values = cast(dict[object, object], loaded)
            for key, value in store_values.items():
                cfg[str(key)] = value
    raw_obj = cfg.get("hybrid_recall_weights", DEFAULT_WEIGHTS)
    if not isinstance(raw_obj, dict):
        return dict(DEFAULT_WEIGHTS)
    raw = cast(dict[object, object], raw_obj)
    out = dict(DEFAULT_WEIGHTS)
    for key in out:
        value = raw.get(key)
        if isinstance(value, (int, float)):
            out[key] = max(0.0, float(value))
    return out


def _renormalize(weights: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(v)) for v in weights.values())
    if total <= 0.0:
        return dict(DEFAULT_WEIGHTS)
    return {k: max(0.0, float(v)) / total for k, v in weights.items()}


def _minmax(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi == lo:
        fill = 1.0 if hi > 0.0 else 0.0
        return {k: fill for k in values}
    span = hi - lo
    return {k: (v - lo) / span for k, v in values.items()}


def _load_bank_entries(store: Path) -> dict[str, _Entry]:
    bank = store / "memory-bank"
    entries: dict[str, _Entry] = {}
    if not bank.exists():
        return entries
    for md in sorted(bank.glob("*.md")):
        section = "root"
        for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("## "):
                section = stripped[3:].strip() or "root"
                continue
            if not stripped or stripped.startswith("#") or not stripped.startswith("- "):
                continue
            entry_id = f"{md.name}:{lineno}"
            entries[entry_id] = {
                "entry_id": entry_id,
                "scope": store.name,
                "path": md.name,
                "section": section,
                "snippet": stripped[2:].strip(),
            }
    return entries


def _load_sidecar_scores(store: Path) -> dict[str, float]:
    bank = store / "memory-bank"
    scores: dict[str, float] = {}
    if not bank.exists():
        return scores
    for md in sorted(bank.glob("*.md")):
        loaded = cast(dict[str, dict[str, object]], load_scores(md))
        for entry_id, payload in loaded.items():
            raw = payload.get("score", DEFAULT_SCORE)
            if isinstance(raw, (int, float)):
                scores[entry_id] = float(raw)
    return scores


def _entry_score(entry_id: str, scores: dict[str, float]) -> float:
    return scores.get(entry_id, DEFAULT_SCORE)


def _entry_ids_for_fts_row(row: dict[str, str], entries: dict[str, _Entry]) -> list[str]:
    path = str(row.get("path", ""))
    section = str(row.get("section", ""))
    exact = [
        entry_id
        for entry_id, entry in entries.items()
        if entry.get("path") == path and entry.get("section") == section
    ]
    if exact:
        return exact
    return [entry_id for entry_id, entry in entries.items() if entry.get("path") == path]


def _candidate_from_entry(entry_id: str, entries: dict[str, _Entry]) -> dict[str, object]:
    return dict(entries[entry_id])


def _candidate_from_entry_or_row(
    entry_id: str,
    entries: dict[str, _Entry],
    row: dict[str, str],
) -> dict[str, object]:
    if entry_id in entries:
        return _candidate_from_entry(entry_id, entries)
    return {
        "entry_id": entry_id,
        "scope": str(row.get("scope", "")),
        "path": str(row.get("path", "")),
        "section": str(row.get("section", "")),
        "snippet": str(row.get("snippet", "")),
    }


def _synthetic_fts_id(row: dict[str, str]) -> str:
    return f"fts:{row.get('scope', '')}:{row.get('path', '')}:{row.get('section', '')}"


def _rank_sort_key(item: dict[str, object]) -> tuple[float, str, str]:
    score = item.get("score", 0.0)
    numeric_score = score if isinstance(score, (int, float)) else 0.0
    return (-float(numeric_score), str(item.get("path", "")), str(item.get("entry_id", "")))
