"""Session timeline — the "Recent sessions" section of the context pack.

Read this first if you are new to the codebase:
  - ``<store>/sessions.jsonl`` holds one JSON line per conversation, keyed by
    ``(source, conversation_id)`` so two tools reusing an id stay separate.
    ``events.jsonl`` rotates at 5,000 lines; this file keeps session
    boundaries after the events are gone.
  - ``record_session_event`` is called by ``events.append_event`` for every
    captured hook. It records the start, the first prompt (sanitized, cut to
    120 characters), the last activity (``stop``), and a real end
    (``sessionEnd``). Repeated deliveries keep the first start and prompt.
    Events without a conversation id are not recorded: they cannot be told
    apart from other sessions.
  - Every write reads, merges, prunes to the 200 most recently active
    conversations, and atomically replaces the file under its own lock with
    a short timeout. A busy lock skips the update rather than delaying the hook.
  - ``render_recent_sessions`` lists the newest sessions with their tool,
    known duration, first prompt, and edit/command counts from one pass over
    the event log.

Public interface (imported elsewhere): ``record_session_event``,
    ``read_sessions``, ``render_recent_sessions``, ``MAX_SESSIONS``,
    ``SESSIONS_FILE``.
Depends on: redact, safety.
Used by: events.events (recording), scope (rendering).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from memory_system.redact import redact_text
from memory_system.safety import LockTimeout, atomic_write, file_lock

SESSIONS_FILE = "sessions.jsonl"
MAX_SESSIONS = 200
PROMPT_CHARS = 120
LOCK_TIMEOUT = 0.5
_RECORDED_HOOKS = frozenset({"sessionStart", "beforeSubmitPrompt", "stop", "sessionEnd"})
_COUNTED_HOOKS = {"afterFileEdit": "edits", "afterShellExecution": "commands"}


def _path(store: Path) -> Path:
    return Path(store) / SESSIONS_FILE


def _load(store: Path) -> list[dict[str, Any]]:
    path = _path(store)
    if not path.exists():
        return []
    sessions: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("conversation_id"), str):
            sessions.append(entry)
    return sessions


def _activity(entry: dict[str, Any]) -> str:
    return str(entry.get("last_activity") or entry.get("started") or "")


def _snippet(prompt: str) -> str:
    text = " ".join(redact_text(prompt).split())
    return text if len(text) <= PROMPT_CHARS else text[: PROMPT_CHARS - 1].rstrip() + "…"


def record_session_event(
    store: Path,
    hook: str,
    conversation_id: str | None,
    source: str | None,
    ts_iso: str,
    prompt: str | None = None,
) -> None:
    """Merge one hook into its conversation's timeline entry (no-op without an id)."""
    if hook not in _RECORDED_HOOKS or not conversation_id:
        return
    source = source or "unknown"
    try:
        with file_lock(Path(store) / ".sessions.lock", timeout=LOCK_TIMEOUT):
            sessions = _load(store)
            entry = next(
                (s for s in sessions if s.get("source") == source and s.get("conversation_id") == conversation_id),
                None,
            )
            if entry is None:
                entry = {"source": source, "conversation_id": conversation_id}
                sessions.append(entry)
            entry["last_activity"] = max(str(entry.get("last_activity") or ""), ts_iso)
            if hook == "sessionStart":
                entry.setdefault("started", ts_iso)
                entry.pop("ended", None)
            elif hook == "beforeSubmitPrompt" and prompt and not entry.get("first_prompt"):
                snippet = _snippet(prompt)
                if snippet:
                    entry["first_prompt"] = snippet
            elif hook == "sessionEnd":
                entry["ended"] = ts_iso
            sessions.sort(key=_activity)
            kept = sessions[-MAX_SESSIONS:]
            atomic_write(_path(store), "".join(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n" for s in kept))
    except (LockTimeout, OSError):
        return


def read_sessions(store: Path, limit: int = 5) -> list[dict[str, Any]]:
    """The ``limit`` most recently active conversations, newest first."""
    return sorted(_load(store), key=_activity, reverse=True)[: max(0, limit)]


def _parse(ts: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def _duration(entry: dict[str, Any]) -> str | None:
    start, end = _parse(entry.get("started")), _parse(entry.get("ended"))
    if start is None or end is None:
        return None
    minutes = max(0, round((end - start).total_seconds() / 60))
    return f"{minutes} min" if minutes < 90 else f"{minutes / 60:.1f} h"


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _activity_counts(store: Path, keys: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, int]]:
    counts: dict[tuple[str, str], dict[str, int]] = {key: {"edits": 0, "commands": 0} for key in keys}
    events = Path(store) / "events.jsonl"
    if not events.exists():
        return counts
    with events.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = _COUNTED_HOOKS.get(str(rec.get("hook")))
            key = (str(rec.get("source") or "unknown"), str(rec.get("conversation_id") or ""))
            if kind is not None and key in counts:
                counts[key][kind] += 1
    return counts


def render_recent_sessions(store: Path, limit: int = 5) -> str:
    """Markdown bullets for the newest sessions; empty when there are none."""
    sessions = read_sessions(store, limit)
    if not sessions:
        return ""
    counts = _activity_counts(store, {(str(s["source"]), str(s["conversation_id"])) for s in sessions})
    lines: list[str] = []
    for entry in sessions:
        when = _parse(entry.get("started") or entry.get("last_activity"))
        parts = [when.strftime("%Y-%m-%d %H:%M UTC") if when else "unknown time", str(entry["source"])]
        duration = _duration(entry)
        if duration:
            parts.append(duration)
        activity = counts[(str(entry["source"]), str(entry["conversation_id"]))]
        parts.append(f"{_count(activity['edits'], 'edit')}, {_count(activity['commands'], 'command')}")
        line = "- " + " · ".join(parts)
        if entry.get("first_prompt"):
            line += f" — \"{entry['first_prompt']}\""
        lines.append(line)
    return "\n".join(lines)
