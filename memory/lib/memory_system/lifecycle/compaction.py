"""Compaction proposer — cluster near-duplicate bank entries (propose only).

Read this first if you are new to the codebase:
  - This module is propose-only. It NEVER deletes bank entries; the
    user-approval gate that folds duplicates lives in ``cli.cli_delete``
    and the ``memprune-review`` flow.
  - Algorithm: read every bullet line under ``<store>/memory-bank/*.md``,
    encode it via the configured embedding backend, build pairwise
    cosine similarity, union pairs at or above ``threshold`` (default
    0.92) with a stdlib union-find, drop singletons, and pick the
    highest-scoring entry per cluster as the keep_id (default score 0.5
    when no sidecar entry exists).
  - numpy is lazy-imported inside method bodies so this file can import
    on hosts without numpy installed (mirrors ``storage.vector_store``).

Public interface (imported elsewhere): ``Cluster``, ``DEFAULT_THRESHOLD``,
    the cluster-proposing entry points.
Depends on: lifecycle.scoring; numpy is lazy-imported at call time only.
Used by: ``memprune-review`` and other duplicate-detection workflows that
    surface compaction candidates to the user.
"""

from __future__ import annotations

# Why: dataclass + field are used so cluster results can be returned as small immutable-ish records with default lists.
from dataclasses import dataclass, field
from pathlib import Path
# Why: import the shape-based interface helper so any object with `encode` can plug in — no inheritance required.
from typing import Any, Protocol

from .scoring import DEFAULT_SCORE, load_scores

DEFAULT_THRESHOLD: float = 0.92


# Why: declare the minimum surface compaction needs from an embedding backend, so tests can pass a tiny stub.
class _EmbeddingBackendLike(Protocol):
    """Structural protocol for embedding backends used by compaction.

    Only ``encode`` is required by this module. The canonical
    :class:`memory_system.backends.embedding.base.EmbeddingBackend` also
    exposes ``name`` / ``dim`` / ``is_available``, but compaction inspects
    the encoded array shape directly rather than trusting ``dim``.
    """

    def encode(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class Cluster:
    """A proposed near-duplicate cluster.

    ``keep_id`` is the entry the user is suggested to retain (highest score
    in the cluster); ``fold_in_ids`` are the other near-duplicate entry ids
    in the same cluster. ``similarity_min`` is the lowest cosine similarity
    observed between any kept-or-folded pair in the cluster.
    """

    keep_id: str
    fold_in_ids: list[str] = field(default_factory=list)
    similarity_min: float = 0.0


def cluster_to_dict(cluster: Cluster) -> dict[str, Any]:
    """JSON-serializable view of a :class:`Cluster`."""
    return {
        "keep_id": cluster.keep_id,
        "fold_in_ids": list(cluster.fold_in_ids),
        "similarity_min": float(cluster.similarity_min),
    }


def _bank_entries(store: Path) -> list[tuple[str, str, Path]]:
    """Return ``(entry_id, text, bank_file)`` for every bullet line in the bank.

    Mirrors :func:`memory_system.storage.vector_store._bank_entries` so vector store
    ids and compaction ids stay aligned. Returns the bank file path alongside
    each entry so score sidecars can be loaded per-file.
    """
    bank_dir = Path(store) / "memory-bank"
    if not bank_dir.exists():
        return []
    entries: list[tuple[str, str, Path]] = []
    for md in sorted(bank_dir.glob("*.md")):
        for lineno, line in enumerate(
            md.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- "):
                entries.append((f"{md.name}:{lineno}", stripped[2:].strip(), md))
    return entries


class _UnionFind:
    """Minimal stdlib union-find over integer indices."""

    def __init__(self, n: int) -> None:
        self._parent: list[int] = list(range(n))
        self._rank: list[int] = [0] * n

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression
        cur = x
        while self._parent[cur] != root:
            nxt = self._parent[cur]
            self._parent[cur] = root
            cur = nxt
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for i in range(len(self._parent)):
            root = self.find(i)
            out.setdefault(root, []).append(i)
        return out


def _score_for_entry(
    entry_id: str,
    scores_by_file: dict[str, dict[str, dict[str, Any]]],
) -> float:
    bank_name = entry_id.split(":", 1)[0]
    file_scores = scores_by_file.get(bank_name, {})
    entry = file_scores.get(entry_id)
    if not isinstance(entry, dict):
        return DEFAULT_SCORE
    raw = entry.get("score", DEFAULT_SCORE)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SCORE


def propose_compaction(
    store: Path,
    backend: _EmbeddingBackendLike,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[Cluster]:
    """Return clusters of near-duplicate entries above ``threshold``.

    The function is read-only with respect to the bank: no files are
    modified, no vectors are removed. An empty list is returned when there
    are no entries, no pairs reach ``threshold``, or all embeddings have
    zero norm (e.g. the no-op embedding backend).

    Within each cluster, the entry id with the highest score sidecar value
    is selected as ``keep_id``. Ties break by ascending entry id (stable).
    """
    import numpy  # pyright: ignore[reportMissingImports]

    store = Path(store)
    entries = _bank_entries(store)
    if len(entries) < 2:
        return []

    ids = [eid for eid, _, _ in entries]
    texts = [text for _, text, _ in entries]

    raw = backend.encode(texts)
    arr = numpy.asarray(raw, dtype=numpy.float32)
    if arr.ndim != 2 or arr.shape[0] != len(ids) or arr.shape[1] == 0:
        return []

    norms = numpy.linalg.norm(arr, axis=1)
    safe = norms > 0
    if not bool(safe.any()):
        return []

    # Normalise rows with non-zero norm; zero-norm rows stay all-zero so any
    # cosine pair they participate in evaluates to 0.0 (well below threshold).
    normed = numpy.zeros_like(arr)
    normed[safe] = arr[safe] / norms[safe][:, None]
    sims = normed @ normed.T

    n = len(ids)
    uf = _UnionFind(n)
    # Track min similarity per cluster representative root (recomputed after
    # all unions complete, so we record observed sims first and reduce later).
    observed_sims: list[tuple[int, int, float]] = []
    for i in range(n):
        if not bool(safe[i]):
            continue
        for j in range(i + 1, n):
            if not bool(safe[j]):
                continue
            sim = float(sims[i, j])
            if sim >= threshold:
                uf.union(i, j)
                observed_sims.append((i, j, sim))

    groups = uf.groups()
    if not groups:
        return []

    # Load score sidecars once per bank file participating in the groups.
    score_cache: dict[str, dict[str, dict[str, Any]]] = {}
    for _, _, bank_file in entries:
        if bank_file.name in score_cache:
            continue
        score_cache[bank_file.name] = load_scores(bank_file)

    # Per-root min similarity across edges that ended up inside the cluster.
    root_min_sim: dict[int, float] = {}
    for i, j, sim in observed_sims:
        root = uf.find(i)
        prev = root_min_sim.get(root)
        root_min_sim[root] = sim if prev is None else min(prev, sim)

    clusters: list[Cluster] = []
    for root, members in groups.items():
        if len(members) < 2:
            continue
        member_ids = [ids[m] for m in members]
        # Highest score wins; ties break by ascending entry id for determinism.
        ranked = sorted(
            member_ids,
            key=lambda eid: (-_score_for_entry(eid, score_cache), eid),
        )
        keep_id = ranked[0]
        fold_in_ids = sorted(eid for eid in member_ids if eid != keep_id)
        clusters.append(
            Cluster(
                keep_id=keep_id,
                fold_in_ids=fold_in_ids,
                similarity_min=root_min_sim.get(root, 0.0),
            )
        )

    # Stable order across runs: sort by keep_id so callers see deterministic output.
    clusters.sort(key=lambda c: c.keep_id)
    return clusters
