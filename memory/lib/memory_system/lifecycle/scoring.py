"""Score sidecar — the per-bank ``.score.json`` file that drives lifecycle policy.

Read this first if you are new to the codebase:
  - One sidecar per markdown bank file, sitting next to it as
    ``<filename>.score.json``. Each entry tracks ``score``,
    ``access_count``, ``reinforcements_count``, ``corrections_count``,
    ``created_iso``, and ``last_accessed_iso``.
  - Scores clamp to ``[SCORE_FLOOR=0.05, SCORE_CEILING=1.0]``. Default
    score for a freshly tracked entry is ``DEFAULT_SCORE=0.5``. Reason
    strings are normalized into reinforcement vs. correction buckets so
    callers don't have to spell ``"positive"`` exactly.
  - Every mutation is wrapped in ``file_lock`` + ``atomic_write`` so the
    sidecar can be safely updated from decay, reinforcement, and delete
    paths in parallel without losing writes.

Public interface (imported elsewhere): ``DEFAULT_SCORE``, ``SCORE_FLOOR``,
    ``SCORE_CEILING``, ``SIDECAR_SUFFIX``, ``load_scores``, ``save_scores``,
    ``update_score``, ``upsert_entry`` (and the related access-bump/mutators).
Depends on: safety.
Used by: cli.cli_delete, cli.cli_inspect, learning.reinforcement,
    lifecycle.compaction, lifecycle.decay, recall.context_pack_v2,
    recall.recall_hybrid.
"""
from __future__ import annotations

import datetime as _datetime
import json
from pathlib import Path
from typing import Any

from ..safety import atomic_write, file_lock

DEFAULT_SCORE = 0.5
SCORE_FLOOR = 0.05
SCORE_CEILING = 1.0
SIDECAR_SUFFIX = ".score.json"

_REINFORCEMENT_REASONS = frozenset({"reinforcement", "reinforce", "positive"})
_CORRECTION_REASONS = frozenset({"correction", "correct", "demote", "negative"})


def _utc_now_iso() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sidecar_path(bank_file: Path) -> Path:
    return bank_file.parent / f"{bank_file.name}{SIDECAR_SUFFIX}"


def _lock_path(bank_file: Path) -> Path:
    return bank_file.parent / f"{bank_file.name}{SIDECAR_SUFFIX}.lock"


def _clamp(value: float) -> float:
    if value < SCORE_FLOOR:
        return SCORE_FLOOR
    if value > SCORE_CEILING:
        return SCORE_CEILING
    return value


def _new_entry(entry_id: str) -> dict[str, Any]:
    now = _utc_now_iso()
    return {
        "id": entry_id,
        "score": DEFAULT_SCORE,
        "last_accessed_iso": now,
        "created_iso": now,
        "access_count": 0,
        "corrections_count": 0,
        "reinforcements_count": 0,
    }


def _read_sidecar(bank_file: Path) -> dict[str, dict[str, Any]]:
    sidecar = _sidecar_path(bank_file)
    if not sidecar.exists():
        return {}
    try:
        raw = sidecar.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _write_sidecar(bank_file: Path, scores: dict[str, dict[str, Any]]) -> None:
    sidecar = _sidecar_path(bank_file)
    atomic_write(sidecar, json.dumps(scores, indent=2, sort_keys=True) + "\n")


def load_scores(bank_file: Path) -> dict[str, dict[str, Any]]:
    """Load the score sidecar for ``bank_file``.

    Returns an empty dict when no sidecar exists yet. Never raises on a missing
    or empty file; only an absent path is silently treated as "no scores".
    """
    bank_file = Path(bank_file)
    return _read_sidecar(bank_file)


def save_scores(bank_file: Path, scores: dict[str, dict[str, Any]]) -> None:
    """Atomically persist ``scores`` to ``<bank_file>.score.json``."""
    bank_file = Path(bank_file)
    _write_sidecar(bank_file, scores)


def update_score(
    bank_file: Path,
    entry_id: str,
    *,
    delta: float,
    reason: str,
) -> dict[str, Any]:
    """Apply ``delta`` to ``entry_id``'s score, clamped to [0.05, 1.0].

    Never deletes an entry. Increments ``reinforcements_count`` when ``reason``
    indicates positive reinforcement and ``corrections_count`` when it indicates
    a correction. Concurrent calls are serialized via a filelock on
    ``<bank_file>.score.json.lock``.
    """
    bank_file = Path(bank_file)
    bank_file.parent.mkdir(parents=True, exist_ok=True)

    with file_lock(_lock_path(bank_file)):
        scores = _read_sidecar(bank_file)
        entry = scores.get(entry_id) or _new_entry(entry_id)
        entry["id"] = entry_id
        current_score = float(entry.get("score", DEFAULT_SCORE))
        entry["score"] = _clamp(current_score + float(delta))

        normalized = reason.strip().lower()
        if normalized in _REINFORCEMENT_REASONS:
            entry["reinforcements_count"] = int(entry.get("reinforcements_count", 0)) + 1
        elif normalized in _CORRECTION_REASONS:
            entry["corrections_count"] = int(entry.get("corrections_count", 0)) + 1

        scores[entry_id] = entry
        _write_sidecar(bank_file, scores)
        return entry


def bump_access(bank_file: Path, entry_id: str) -> dict[str, Any]:
    """Touch ``last_accessed_iso`` and increment ``access_count`` for ``entry_id``.

    Creates the entry with defaults if it does not yet exist. Score is left
    untouched.
    """
    bank_file = Path(bank_file)
    bank_file.parent.mkdir(parents=True, exist_ok=True)

    with file_lock(_lock_path(bank_file)):
        scores = _read_sidecar(bank_file)
        entry = scores.get(entry_id) or _new_entry(entry_id)
        entry["id"] = entry_id
        entry["last_accessed_iso"] = _utc_now_iso()
        entry["access_count"] = int(entry.get("access_count", 0)) + 1
        scores[entry_id] = entry
        _write_sidecar(bank_file, scores)
        return entry


def upsert_entry(
    bank_file: Path,
    entry_id: str,
    *,
    score: float | None = None,
    tags: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Create or update ``entry_id``: set ``score`` (clamped) and add ``tags``.

    Every other field of an existing entry is preserved. Runs under the same
    sidecar lock as ``update_score``.
    """
    bank_file = Path(bank_file)
    bank_file.parent.mkdir(parents=True, exist_ok=True)

    with file_lock(_lock_path(bank_file)):
        scores = _read_sidecar(bank_file)
        entry = scores.get(entry_id) or _new_entry(entry_id)
        entry["id"] = entry_id
        if score is not None:
            entry["score"] = _clamp(float(score))
        if tags:
            existing = entry.get("tags")
            merged = list(existing) if isinstance(existing, list) else []
            merged.extend(tag for tag in tags if tag not in merged)
            entry["tags"] = merged
        scores[entry_id] = entry
        _write_sidecar(bank_file, scores)
        return entry
