"""Event capture and queue plumbing — the workspace's append-only activity log.

Read this first if you are new to the codebase:
  - Every hook (sessionStart, beforeSubmitPrompt, afterAgentResponse, etc.),
    after ``events.ingress`` has mapped it to the canonical names, calls
    ``append_event`` here, which writes a JSON line to
    ``<store>/events.jsonl`` after sanitizing the payload through
    ``redact.sanitize_payload``. Each line records the ``source`` tool, and
    session boundaries also update ``recall.sessions``' timeline.
  - The denylist filter (``_should_suppress``) drops events that mention
    paths matching glob patterns in ``config.json`` — that's how
    secrets-by-path are kept out of the log.
  - Job enqueueing piggybacks on the same lock and store layout; the heavy
    work runs in ``events.worker`` so the hook stays fast.

Public interface (imported elsewhere): ``content_hash``,
    ``estimate_unobserved_tokens``, ``rotate_events``, ``enqueue_job``,
    ``record_event``, plus the helpers re-exported by ``events/__init__.py``.
Depends on: system.config, paths, recall.sessions, redact, safety.
Used by: events (package init re-export), events.observer, events.worker.
"""
from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatch
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from memory_system.system.config import load_config, memory_home
from memory_system.paths import ensure_layout, lock_path, new_event_id, workspace_store
from memory_system.recall.sessions import record_session_event
from memory_system.redact import sanitize_payload
from memory_system.safety import LockTimeout, atomic_write, file_lock


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_path(payload: dict[str, Any]) -> Path | None:
    raw = payload.get("path") or payload.get("file_path")
    if not raw:
        return None
    return Path(str(raw)).expanduser().resolve()


def _path_is_denylisted(candidate: Path, pattern: str) -> bool:
    expanded = Path(pattern).expanduser()
    pattern_text = str(expanded)
    if any(ch in pattern for ch in "*?[]"):
        candidate_text = str(candidate)
        return fnmatch(candidate_text, pattern_text) or fnmatch(candidate.name, pattern)
    if pattern.startswith("/") or pattern.startswith("~"):
        root = expanded.resolve()
        return candidate == root or root in candidate.parents
    return fnmatch(candidate.name, pattern)


def _should_suppress(payload: dict[str, Any]) -> tuple[bool, str]:
    candidate = _payload_path(payload)
    if candidate is None:
        return False, ""

    home = Path.home().expanduser().resolve()
    roots = [
        memory_home().resolve(),
        home / ".cursor" / "hooks",
        home / ".claude" / "hooks",
        home / ".claude" / "settings.json",
        home / ".claude" / "rules",
        home / ".config" / "opencode" / "plugins",
    ]
    if any(candidate == root or root in candidate.parents for root in roots):
        return True, "workspace_root"

    config = load_config()
    for item in cast(list[object], config.get("path_denylist", [])):
        item_text = str(item)
        if _path_is_denylisted(candidate, item_text):
            return True, item_text
    return False, ""


def _append_jsonl(path: Path, line: str, store: Path) -> None:
    try:
        with file_lock(lock_path(store), timeout=2.0):
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except LockTimeout:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)


def append_event(workspace_root: Path, hook_event: str, payload: dict[str, Any]) -> str:
    store = workspace_store(workspace_root)
    ensure_layout(store)
    event_id = new_event_id()
    record = {
        "id": event_id,
        "ts": _now_iso(),
        "hook": hook_event,
        "conversation_id": payload.get("conversation_id"),
        "source": payload.get("source"),
        "payload": sanitize_payload(payload),
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    events_path = store / "events.jsonl"
    suppressed, reason = _should_suppress(payload)
    if suppressed:
        suppressed_record = dict(record)
        suppressed_record.update({"suppressed": True, "reason": reason})
        _append_jsonl(store / "events.jsonl-suppressed.log", json.dumps(suppressed_record, ensure_ascii=False) + "\n", store)
        return ""
    _append_jsonl(events_path, line, store)
    sanitized = cast(dict[str, Any], record["payload"])
    record_session_event(
        store,
        hook_event,
        record["conversation_id"],
        record["source"],
        str(record["ts"]),
        prompt=str(sanitized.get("prompt") or "") or None,
    )
    return event_id


def enqueue_job(workspace_root: Path, job_type: str, extra: dict[str, Any] | None = None) -> None:
    store = workspace_store(workspace_root)
    ensure_layout(store)
    job = {"ts": _now_iso(), "type": job_type, **(extra or {})}
    queue = store / "queues" / "pending.jsonl"
    try:
        with file_lock(lock_path(store), timeout=2.0):
            with queue.open("a", encoding="utf-8") as f:
                f.write(json.dumps(job) + "\n")
    except LockTimeout:
        with queue.open("a", encoding="utf-8") as f:
            f.write(json.dumps(job) + "\n")


def estimate_unobserved_tokens(store: Path) -> int:
    meta = store / ".observer_state.json"
    last_id = ""
    if meta.exists():
        last_id = json.loads(meta.read_text(encoding="utf-8")).get("last_event_id", "")
    tokens = 0
    seen_last = not last_id
    events_path = store / "events.jsonl"
    if not events_path.exists():
        return 0
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if not seen_last:
            if rec.get("id") == last_id:
                seen_last = True
            continue
        blob = json.dumps(rec.get("payload", {}))
        tokens += max(1, len(blob) // 4)
    return tokens


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def rotate_events(store: Path, keep_from_id: str, max_lines: int = 5000) -> int:
    """Trim the append-only event buffer once it grows past max_lines.

    Events are only a buffer feeding the observer; once observed (captured into
    observations.md), older raw events are disposable. We keep every line at and
    after keep_from_id (the observer's last marker, so its resume logic still
    finds it) and discard strictly-older lines. Returns the number of lines
    dropped (0 if no rotation happened). Safe no-op if the marker isn't found.
    """
    events_path = store / "events.jsonl"
    if not events_path.exists():
        return 0
    lines = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) <= max_lines:
        return 0
    keep_idx = None
    for i, ln in enumerate(lines):
        try:
            if json.loads(ln).get("id") == keep_from_id:
                keep_idx = i
                break
        except json.JSONDecodeError:
            continue
    if keep_idx is None:
        return 0
    kept = lines[keep_idx:]
    dropped = len(lines) - len(kept)
    if dropped <= 0:
        return 0
    with file_lock(lock_path(store)):
        atomic_write(events_path, "\n".join(kept) + "\n")
    return dropped
