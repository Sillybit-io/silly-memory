# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnusedCallResult=false

"""Correction detector — spot when the user edited right after an AI reply.

Read this first if you are new to the codebase:
  - Reads ``ai-text-log.jsonl`` (full AI replies) and pairs each reply
    with subsequent ``afterFileEdit`` events inside a configurable time
    window (default 300 seconds). If the diff overlap is high enough
    relative to the reply text, that's a correction.
  - The match-ratio threshold (``_MATCH_RATIO_NO_CORRECTION = 0.985``) is
    the inverse — if the user's edit is nearly identical to the AI's
    suggestion, NO correction is recorded (we accepted the suggestion).
  - Detected events append to ``corrections.jsonl`` with a stable id and
    a redacted excerpt; downstream ``reinforcement`` consumes that file.

Public interface (imported elsewhere): ``CorrectionEvent``,
    ``detect_corrections``.
Depends on: system.config, paths, redact, safety.
Used by: callers that consume ``corrections.jsonl`` such as
    ``cli.cli_learn_status``; the worker pipeline triggers detection.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..system.config import load_config
from ..paths import lock_path
from ..redact import sanitize_payload
from ..safety import LockTimeout, file_lock

_AI_TEXT_LOG_FILENAME = "ai-text-log.jsonl"
_CORRECTIONS_FILENAME = "corrections.jsonl"
_DEFAULT_WINDOW_SECONDS = 300
_MIN_OVERLAP = 0.25
_MATCH_RATIO_NO_CORRECTION = 0.985


@dataclass(frozen=True)
class CorrectionEvent:
    id: str
    ts: str
    conversation_id: str
    ai_response_excerpt: str
    edit_diff_summary: str
    confidence: float

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def detect_corrections(store: Path, window_seconds: int | None = None) -> list[CorrectionEvent]:
    effective_window = _configured_window_seconds() if window_seconds is None else window_seconds
    ai_records = _read_jsonl(store / _AI_TEXT_LOG_FILENAME)
    edit_records = [rec for rec in _read_jsonl(store / "events.jsonl") if rec.get("hook") == "afterFileEdit"]
    existing_ids = _existing_correction_ids(store)
    corrections: list[CorrectionEvent] = []
    for ai in ai_records:
        for edit in _candidate_edits(ai, edit_records, effective_window):
            correction = _classify(ai, edit)
            if correction is None or correction.id in existing_ids:
                continue
            corrections.append(correction)
            existing_ids.add(correction.id)
    if corrections:
        _append_corrections(store, corrections)
    return corrections


def read_corrections(store: Path) -> list[dict[str, Any]]:
    return _read_jsonl(store / _CORRECTIONS_FILENAME)


def _configured_window_seconds() -> int:
    raw = load_config().get("correction_window_seconds", _DEFAULT_WINDOW_SECONDS)
    if not isinstance(raw, (int, float, str)):
        return _DEFAULT_WINDOW_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_SECONDS
    return value if value > 0 else _DEFAULT_WINDOW_SECONDS


def _candidate_edits(ai: dict[str, Any], edits: list[dict[str, Any]], window_seconds: int) -> list[dict[str, Any]]:
    ai_ts = _parse_ts(str(ai.get("ts", "")))
    if ai_ts is None:
        return []
    ai_conv = str(ai.get("conversation_id") or "")
    out: list[dict[str, Any]] = []
    for edit in edits:
        edit_ts = _parse_ts(str(edit.get("ts", "")))
        if edit_ts is None:
            continue
        delta = (edit_ts - ai_ts).total_seconds()
        if delta < 0 or delta > window_seconds:
            continue
        edit_conv = str(edit.get("conversation_id") or "")
        if ai_conv and edit_conv and edit_conv != ai_conv:
            continue
        out.append(edit)
    return out


def _classify(ai: dict[str, Any], edit: dict[str, Any]) -> CorrectionEvent | None:
    ai_text = str(ai.get("response_text") or "")
    payload = edit.get("payload") if isinstance(edit.get("payload"), dict) else {}
    edit_text = _edit_text(payload if isinstance(payload, dict) else {})
    if not ai_text.strip() or not edit_text.strip():
        return None
    ai_norm = _normalize(ai_text)
    edit_norm = _normalize(edit_text)
    if ai_norm == edit_norm:
        return None
    overlap = _token_overlap(ai_norm, edit_norm)
    if overlap < _MIN_OVERLAP:
        return None
    ratio = difflib.SequenceMatcher(None, ai_norm, edit_norm, autojunk=False).ratio()
    if ratio >= _MATCH_RATIO_NO_CORRECTION:
        return None
    confidence = _confidence(overlap, ratio)
    if confidence <= 0.0:
        return None
    summary = _edit_summary(edit, edit_text)
    event_id = _correction_id(ai, edit, summary)
    sanitized = sanitize_payload({"text": ai_text, "summary": summary})
    return CorrectionEvent(
        id=event_id,
        ts=str(edit.get("ts") or ai.get("ts") or ""),
        conversation_id=str(ai.get("conversation_id") or edit.get("conversation_id") or ""),
        ai_response_excerpt=_excerpt(str(sanitized.get("text", ""))),
        edit_diff_summary=_excerpt(str(sanitized.get("summary", summary)), limit=500),
        confidence=confidence,
    )


def _edit_text(payload: dict[str, Any]) -> str:
    for key in ("diff", "patch", "text", "content", "new_text", "after", "after_text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _added_lines(value) if key in {"diff", "patch"} else value
    changes = payload.get("changes")
    if isinstance(changes, list):
        parts: list[str] = []
        for item in changes:
            if isinstance(item, dict):
                parts.append(_edit_text(item))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part.strip())
    return ""


def _added_lines(text: str) -> str:
    lines = text.splitlines()
    has_patch_markers = any(line.startswith(("+++", "---", "@@", "+", "-")) for line in lines)
    if not has_patch_markers:
        return text
    added: list[str] = []
    for line in lines:
        if line.startswith("+++") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
    return "\n".join(added) if added else text


def _edit_summary(edit: dict[str, Any], edit_text: str) -> str:
    payload = edit.get("payload") if isinstance(edit.get("payload"), dict) else {}
    path = ""
    if isinstance(payload, dict):
        path = str(payload.get("file_path") or payload.get("path") or "")
    prefix = f"{path}: " if path else ""
    return f"{prefix}{_excerpt(edit_text, limit=420)}"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _token_overlap(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[a-z0-9_]{2,}", left))
    right_tokens = set(re.findall(r"[a-z0-9_]{2,}", right))
    if not left_tokens or not right_tokens:
        return 0.0
    shared = len(left_tokens & right_tokens)
    return max(shared / len(left_tokens), shared / len(right_tokens))


def _confidence(overlap: float, ratio: float) -> float:
    raw = (overlap * 0.55) + ((1.0 - ratio) * 0.45)
    return round(max(0.0, min(1.0, raw)), 3)


def _excerpt(text: str, limit: int = 300) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _correction_id(ai: dict[str, Any], edit: dict[str, Any], summary: str) -> str:
    seed = "\0".join([str(ai.get("id", "")), str(edit.get("id", "")), summary])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _existing_correction_ids(store: Path) -> set[str]:
    return {str(rec.get("id")) for rec in _read_jsonl(store / _CORRECTIONS_FILENAME) if rec.get("id")}


def _append_corrections(store: Path, corrections: list[CorrectionEvent]) -> None:
    path = store / _CORRECTIONS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(event.to_record(), ensure_ascii=False) + "\n" for event in corrections]
    try:
        with file_lock(lock_path(store), timeout=2.0):
            with path.open("a", encoding="utf-8") as f:
                for line in lines:
                    f.write(line)
    except LockTimeout:
        with path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(line)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
