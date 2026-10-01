"""Compaction handoff notes — one per conversation, restored once after compaction.

Read this first if you are new to the codebase:
  - A note belongs to one conversation of one tool: its file is
    ``<store>/handoffs/<sha256 of "source\\0conversation_id">.md`` and its first
    line repeats that identity, the creation time, and a generation token.
  - ``write_handoff`` replaces a conversation's note atomically under that
    note's own lock. ``consume_handoff`` reads and deletes the matching note
    under the same lock, so of two consumers only one restores it, and notes
    of other conversations are never read.
  - A note older than 24 hours is never restored; consuming it deletes it.
    Expired notes of other conversations are pruned when their lock is free.
  - When a token is required (OpenCode) or supplied, the note is restored only
    if its token matches, so a note left from an earlier compaction attempt is
    not mistaken for the current one. A mismatch leaves the note in place.

Public interface (imported elsewhere): ``session_key``, ``handoff_path``,
    ``write_handoff``, ``consume_handoff``, ``RESTORE_HEADER``,
    ``RESTORE_MAX_CHARS``, ``HANDOFF_MAX_AGE_SECONDS``.
Depends on: safety.
Used by: recall.context_pack (``session_start_top_up``).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from memory_system.safety import LockTimeout, atomic_write, file_lock

HANDOFF_DIR = "handoffs"
HANDOFF_MAX_AGE_SECONDS = 24 * 60 * 60
HANDOFF_LOCK_TIMEOUT = 1.0
RESTORE_HEADER = "Context restored after compaction"
RESTORE_MAX_CHARS = 9000
_TRUNCATED = "\n…"
_META_LINE = re.compile(r"^<!-- silly-memory handoff (?P<meta>\{.*\}) -->$")


def session_key(source: str, conversation_id: str) -> str:
    return f"{source}\0{conversation_id}"


def handoff_path(store: Path, source: str, conversation_id: str) -> Path:
    digest = hashlib.sha256(session_key(source, conversation_id).encode("utf-8")).hexdigest()
    return Path(store) / HANDOFF_DIR / f"{digest}.md"


def _lock_for(note: Path) -> Path:
    return note.with_suffix(".lock")


def _parse(text: str) -> tuple[dict[str, object] | None, str]:
    first, _, body = text.partition("\n")
    match = _META_LINE.match(first)
    if not match:
        return None, text
    try:
        meta = json.loads(match.group("meta"))
    except json.JSONDecodeError:
        return None, text
    return (meta if isinstance(meta, dict) else None), body


def _expired(meta: dict[str, object], now: float) -> bool:
    created = meta.get("created")
    return not isinstance(created, (int, float)) or now - float(created) > HANDOFF_MAX_AGE_SECONDS


def write_handoff(
    store: Path,
    source: str,
    conversation_id: str,
    body: str,
    *,
    token: str | None = None,
    now: float | None = None,
) -> str:
    """Atomically replace the conversation's note; return its generation token."""
    note = handoff_path(store, source, conversation_id)
    note.parent.mkdir(parents=True, exist_ok=True)
    token = token or uuid.uuid4().hex
    meta = {
        "session_key": session_key(source, conversation_id),
        "source": source,
        "conversation_id": conversation_id,
        "created": time.time() if now is None else now,
        "token": token,
    }
    with file_lock(_lock_for(note), timeout=HANDOFF_LOCK_TIMEOUT):
        atomic_write(note, f"<!-- silly-memory handoff {json.dumps(meta, sort_keys=True)} -->\n{body}", mode=0o600)
    return token


def _restoration(body: str) -> str:
    text = f"{RESTORE_HEADER}\n\n{body.strip()}"
    if len(text) <= RESTORE_MAX_CHARS:
        return text
    return text[: RESTORE_MAX_CHARS - len(_TRUNCATED)].rstrip() + _TRUNCATED


def _prune_expired(directory: Path, keep: Path, now: float) -> None:
    for note in directory.glob("*.md"):
        if note == keep:
            continue
        try:
            with file_lock(_lock_for(note), timeout=0.05):
                meta, _ = _parse(note.read_text(encoding="utf-8"))
                if meta is not None and _expired(meta, now):
                    note.unlink()
        except (LockTimeout, OSError):
            continue


def consume_handoff(
    store: Path,
    source: str,
    conversation_id: str,
    *,
    token: str | None = None,
    require_token: bool = False,
    now: float | None = None,
) -> str | None:
    """Read and delete the conversation's note; return the restoration text, or None.

    Returns None, leaving the note in place, when there is no valid note for
    this conversation or its token does not match. Raises ``LockTimeout`` if
    another process holds the note's lock for too long.
    """
    now = time.time() if now is None else now
    note = handoff_path(store, source, conversation_id)
    if not note.parent.is_dir():
        return None
    _prune_expired(note.parent, note, now)
    with file_lock(_lock_for(note), timeout=HANDOFF_LOCK_TIMEOUT):
        if not note.exists():
            return None
        meta, body = _parse(note.read_text(encoding="utf-8"))
        if meta is None or meta.get("session_key") != session_key(source, conversation_id):
            return None
        if _expired(meta, now):
            note.unlink()
            return None
        if (require_token or token is not None) and (not token or meta.get("token") != token):
            return None
        note.unlink()
    return _restoration(body)
