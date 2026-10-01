"""Full-text capture of `afterAgentResponse` events — source for the correction loop.

Read this first if you are new to the codebase:
  - ``observations.md`` only keeps short dated bullets (distiller input).
    The correction detector needs the COMPLETE AI reply so it can diff
    against what the user typed next — that complete copy lives here, as
    one append-only JSONL per workspace store.
  - Record schema: ``{id, ts, conversation_id, prompt, response_text}``.
    The response field is NEVER lossy-truncated by this module — the only
    cap is the 8000-char policy ceiling inside ``sanitize_payload``, which
    is privacy policy, not opportunistic trimming.
  - Writes go through ``redact.sanitize_payload`` just like every other
    on-disk write; never bypass it, or you risk leaking secrets into a
    file that explicitly stores user prompts.

Public interface (imported elsewhere): ``append_ai_text``.
Depends on: paths, redact, safety.
Used by: events.observer (calls ``append_ai_text``).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..paths import lock_path
from ..redact import sanitize_payload
from ..safety import LockTimeout, file_lock

_AI_TEXT_LOG_FILENAME = "ai-text-log.jsonl"


def _log_path(store: Path) -> Path:
    return store / _AI_TEXT_LOG_FILENAME


def append_ai_text(
    store: Path,
    conversation_id: str,
    prompt: str,
    response_text: str,
    ts_iso: str,
) -> None:
    sanitized = sanitize_payload({"prompt": prompt, "text": response_text})
    record: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "ts": ts_iso,
        "conversation_id": conversation_id,
        "prompt": sanitized.get("prompt", ""),
        "response_text": sanitized.get("text", ""),
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    path = _log_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(lock_path(store), timeout=2.0):
            with path.open("a", encoding="utf-8") as f:
                _ = f.write(line)
    except LockTimeout:
        # Fail-open: matches events._append_jsonl. The observer runs on the hook
        # hot path; never hang the shell for a contended lock.
        with path.open("a", encoding="utf-8") as f:
            _ = f.write(line)


def read_ai_text_since(store: Path, last_id: str | None) -> list[dict]:
    path = _log_path(store)
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8").splitlines()
    parsed: list[dict] = []
    for line in raw:
        if not line.strip():
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not last_id:
        return parsed
    # Defensive replay-from-id: if the marker isn't in the file (e.g. archived
    # away in a future rotation), return everything rather than nothing.
    if not any(r.get("id") == last_id for r in parsed):
        return parsed
    out: list[dict] = []
    seen = False
    for rec in parsed:
        if not seen:
            if rec.get("id") == last_id:
                seen = True
            continue
        out.append(rec)
    return out
