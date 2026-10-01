# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnusedCallResult=false

"""Reinforcement engine — turn correction events into score deltas on bank entries.

Read this first if you are new to the codebase:
  - Consumes the ``corrections.jsonl`` stream and demotes bank entries
    whose text overlaps the AI excerpt (a hint that we got that fact
    wrong). ``apply_reinforcement`` is the matching promote path for facts
    that backed a successful answer.
  - Idempotent: the last-processed correction id is persisted to
    ``<store>/.reinforcement_state.json`` via atomic write and used as the
    cursor for the next batch — replaying the same file is safe.
  - ``lifecycle.scoring.update_score`` is the ONLY side-effecting API used
    here; never mutate sidecars directly from this module, or you break
    the lock ordering with deletes and decay.

Public interface (imported elsewhere): ``apply_correction``,
    ``apply_reinforcement``.
Depends on: lifecycle.scoring, safety.
Used by: the worker pipeline (invoked after correction detection).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..lifecycle.scoring import update_score
from ..safety import atomic_write, file_lock

_CORRECTIONS_FILENAME = "corrections.jsonl"
_STATE_FILENAME = ".reinforcement_state.json"
_STATE_LOCK_FILENAME = ".reinforcement_state.json.lock"
_CORRECTION_DELTA = -0.1
_DEFAULT_REINFORCEMENT_MAGNITUDE = 0.05
_MATCH_OVERLAP = 0.3
_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")


def apply_correction(store: Path, correction_event: dict[str, Any]) -> list[str]:
    """Demote bank entries whose text overlaps the correction excerpt.

    The correction's ``ai_response_excerpt`` is tokenised and compared against
    every bullet entry under ``<store>/memory-bank/*.md``. Entries with token
    overlap >= :data:`_MATCH_OVERLAP` are demoted by ``-0.1`` via
    :func:`scoring.update_score` (reason ``"correction"``). The floor/ceiling
    enforced by ``update_score`` is therefore inherited unchanged.

    Returns the list of entry ids that were demoted, in scan order.
    """
    store = Path(store)
    excerpt = str(correction_event.get("ai_response_excerpt") or "")
    if not excerpt.strip():
        return []
    excerpt_tokens = _tokenize(excerpt)
    if not excerpt_tokens:
        return []
    demoted: list[str] = []
    for entry_id, text, bank_file in _bank_entries(store):
        entry_tokens = _tokenize(text)
        if not entry_tokens:
            continue
        if _overlap(excerpt_tokens, entry_tokens) < _MATCH_OVERLAP:
            continue
        update_score(bank_file, entry_id, delta=_CORRECTION_DELTA, reason="correction")
        demoted.append(entry_id)
    return demoted


def apply_reinforcement(
    store: Path,
    entry_id: str,
    *,
    magnitude: float = _DEFAULT_REINFORCEMENT_MAGNITUDE,
) -> None:
    """Boost an entry's score when recall is followed by no correction.

    Silently no-ops when the entry's bank file no longer exists so a stale
    recall log cannot resurrect a deleted bank file's sidecar.
    """
    store = Path(store)
    bank_file = _bank_file_for_entry(store, entry_id)
    if bank_file is None:
        return
    update_score(bank_file, entry_id, delta=float(magnitude), reason="reinforcement")


def replay_corrections(store: Path, since_id: str | None = None) -> int:
    """Batch-process ``corrections.jsonl`` past the cursor.

    When ``since_id`` is ``None`` the persisted cursor in
    ``<store>/.reinforcement_state.json`` is used; otherwise ``since_id``
    overrides the persisted cursor for this call only. Events with id equal
    to the cursor are skipped along with everything before them. Returns the
    number of events that were applied.
    """
    store = Path(store)
    cursor = since_id if since_id is not None else _read_cursor(store)
    events = _read_corrections(store)
    if not events:
        return 0
    start = 0
    if cursor is not None:
        cursor_index = _index_of(events, cursor)
        if cursor_index is None:
            return 0
        start = cursor_index + 1
    processed = 0
    last_id: str | None = None
    for event in events[start:]:
        event_id = str(event.get("id") or "")
        if not event_id:
            continue
        _ = apply_correction(store, event)
        processed += 1
        last_id = event_id
    if last_id is not None:
        _write_cursor(store, last_id)
    return processed


def _bank_entries(store: Path) -> list[tuple[str, str, Path]]:
    """Return ``(entry_id, text, bank_file)`` per bullet in ``memory-bank/*.md``.

    Mirrors :func:`memory_system.lifecycle.compaction._bank_entries` so a
    correction-demoted id matches the id surfaced by compaction/recall.
    """
    bank_dir = store / "memory-bank"
    if not bank_dir.exists():
        return []
    entries: list[tuple[str, str, Path]] = []
    for md in sorted(bank_dir.glob("*.md")):
        for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- "):
                entries.append((f"{md.name}:{lineno}", stripped[2:].strip(), md))
    return entries


def _bank_file_for_entry(store: Path, entry_id: str) -> Path | None:
    bank_name = entry_id.split(":", 1)[0]
    bank_file = store / "memory-bank" / bank_name
    return bank_file if bank_file.exists() else None


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    shared = len(left & right)
    return max(shared / len(left), shared / len(right))


def _read_corrections(store: Path) -> list[dict[str, Any]]:
    path = store / _CORRECTIONS_FILENAME
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _index_of(events: list[dict[str, Any]], target_id: str) -> int | None:
    for i, event in enumerate(events):
        if str(event.get("id") or "") == target_id:
            return i
    return None


def _state_path(store: Path) -> Path:
    return store / _STATE_FILENAME


def _state_lock(store: Path) -> Path:
    return store / _STATE_LOCK_FILENAME


def _read_cursor(store: Path) -> str | None:
    path = _state_path(store)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    cursor = parsed.get("last_event_id")
    if not cursor:
        return None
    return str(cursor)


def _write_cursor(store: Path, event_id: str) -> None:
    path = _state_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(_state_lock(store)):
        atomic_write(path, json.dumps({"last_event_id": event_id}, indent=2) + "\n")
