"""Time-based score decay — lower confidence on memories the user no longer touches.

Read this first if you are new to the codebase:
  - Decay model: each entry's score erodes exponentially with a default
    30-day half-life. Frequently accessed entries earn a small per-hit
    bonus (capped at ``_ACCESS_BONUS_CAP``) so popular facts resist decay.
  - Decay is destructive ONLY to the sidecar value, never to the bullet
    line itself. Below ``SCORE_FLOOR`` (0.05) the entry becomes a
    candidate for ``cli.cli_delete``'s prune review, which still asks
    the user before deleting.
  - Runs under the same per-sidecar lock as ``lifecycle.scoring``, so
    parallel decay + reinforcement runs cannot interleave and lose writes.

Public interface (imported elsewhere): ``DEFAULT_HALF_LIFE_DAYS``, plus
    the decay-application entry points.
Depends on: lifecycle.scoring, safety.
Used by: scheduled decay passes invoked from CLI tooling and tests.
"""
from __future__ import annotations

import datetime as _datetime
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..safety import file_lock
from .scoring import (
    SCORE_CEILING,
    SCORE_FLOOR,
    load_scores,
    save_scores,
)

DEFAULT_HALF_LIFE_DAYS = 30.0
_UNTOUCHED_PENALTY = 1.5
_ACCESS_BONUS_PER_HIT = 0.002
_ACCESS_BONUS_CAP = 0.05


def _utc_now_iso() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(iso: str) -> _datetime.datetime:
    text = iso.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = _datetime.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_datetime.timezone.utc)
    return dt


def _days_between(now: _datetime.datetime, then_iso: str) -> float:
    try:
        then = _parse_iso(then_iso)
    except (ValueError, TypeError):
        return 0.0
    delta = now - then
    days = delta.total_seconds() / 86400.0
    if days < 0.0:
        return 0.0
    return days


def _clamp(value: float) -> float:
    if value < SCORE_FLOOR:
        return SCORE_FLOOR
    if value > SCORE_CEILING:
        return SCORE_CEILING
    return value


def _is_touched(entry: dict[str, Any]) -> bool:
    corrections = int(entry.get("corrections_count", 0) or 0)
    reinforcements = int(entry.get("reinforcements_count", 0) or 0)
    return corrections > 0 or reinforcements > 0


def _lock_path(bank_file: Path) -> Path:
    return bank_file.parent / f"{bank_file.name}.score.json.lock"


def decay_step(
    scores: dict[str, dict[str, Any]],
    *,
    now_iso: str,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> dict[str, dict[str, Any]]:
    """Apply time-based decay to every entry in ``scores`` and return a new dict.

    Pure function: input is not mutated. Each entry's score is reduced by an
    exponential factor of ``exp(-ln(2) * days_since_last_access / half_life_days)``
    and bumped by a small per-access bonus. Untouched entries (no corrections,
    no reinforcements) decay faster by a fixed multiplier so unused memories
    fade without ever crossing the 0.05 floor.
    """
    if half_life_days <= 0.0:
        raise ValueError("half_life_days must be positive")

    now = _parse_iso(now_iso)
    ln2 = math.log(2.0)
    out: dict[str, dict[str, Any]] = {}

    for entry_id, raw in scores.items():
        entry = deepcopy(raw)
        anchor = entry.get("last_accessed_iso") or entry.get("created_iso") or now_iso
        days = _days_between(now, str(anchor))

        penalty = _UNTOUCHED_PENALTY if not _is_touched(entry) else 1.0
        exponent = -ln2 * (days * penalty) / half_life_days
        factor = math.exp(exponent)

        access_count = int(entry.get("access_count", 0) or 0)
        bonus = min(_ACCESS_BONUS_CAP, access_count * _ACCESS_BONUS_PER_HIT)

        old_score = float(entry.get("score", SCORE_FLOOR))
        new_score = _clamp(old_score * factor + bonus)

        entry["score"] = new_score
        out[entry_id] = entry

    return out


def apply_decay(store: Path, now_iso: str | None = None) -> int:
    """Decay scores on disk for the sidecar of ``store``.

    Returns the number of entries that were touched (i.e. present in the
    sidecar). Never deletes entries — this function only adjusts scores and
    respects the 0.05 floor enforced by :func:`decay_step`.
    """
    bank_file = Path(store)
    timestamp = now_iso or _utc_now_iso()

    with file_lock(_lock_path(bank_file)):
        scores = load_scores(bank_file)
        if not scores:
            return 0
        decayed = decay_step(scores, now_iso=timestamp)
        save_scores(bank_file, decayed)
        return len(decayed)
