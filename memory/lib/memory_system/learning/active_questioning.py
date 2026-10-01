"""Active questioning — ask the user a clarifying question when the system is unsure.

Read this first if you are new to the codebase:
  - ``maybe_active_question`` is called from the session boundary. It
    decides whether to surface a question (low classifier confidence, a
    detected contradiction, or a repeated correction) and uses the LLM
    backend to condense recent observations into the prompt.
  - Per-session state is persisted to ``<store>/.active_q_state.json`` so
    the same question is not asked twice; a draft of the next question is
    parked under ``pending_question.md`` plus its meta sidecar.
  - The LLM is described by a small ``_QuestionBackend`` shape so tests
    can swap in a stub. The production class is
    ``backends.llm.cursor_agent_backend.CursorAgentBackend``, which is
    network-gated by ``MEMORY_ALLOW_NETWORK``.

Public interface (imported elsewhere): ``STATE_FILENAME``,
    ``PENDING_QUESTION_FILENAME``, ``PENDING_QUESTION_META_FILENAME``,
    ``LOW_CONFIDENCE_THRESHOLD``, ``maybe_active_question``.
Depends on: backends.llm.cursor_agent_backend, safety.
Used by: cli.cli_learn_status (for the state filename).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
# Why: import the typing helpers used to describe and narrow the LLM backend without committing to a concrete class.
from typing import Protocol, cast

from ..backends.llm.cursor_agent_backend import CursorAgentBackend
from ..safety import atomic_write

STATE_FILENAME = ".active_q_state.json"
PENDING_QUESTION_FILENAME = "pending_question.md"
PENDING_QUESTION_META_FILENAME = ".pending_question_meta.json"
LOW_CONFIDENCE_THRESHOLD = 0.35


# Why: describe only the two backend methods this module actually calls so we can swap LLM implementations in tests.
class _QuestionBackend(Protocol):
    def is_available(self) -> bool: ...

    def condense(self, observations: list[str]) -> str: ...


def maybe_active_question(
    store: Path,
    *,
    session_id: str,
    llm_backend: _QuestionBackend | None = None,
    classifier_result: tuple[str, float] | None = None,
    contradiction_detected: bool = False,
    repeated_correction: bool = False,
    text: str = "",
) -> str | None:
    store = Path(store)
    trigger = _trigger_reason(
        classifier_result=classifier_result,
        contradiction_detected=contradiction_detected,
        repeated_correction=repeated_correction,
    )
    if trigger is None:
        return None
    if _already_asked_this_session(store, session_id):
        return None
    backend = llm_backend or CursorAgentBackend()
    if not _backend_available(backend):
        fallback = _fallback_question(trigger, text)
        _write_pending_question_artifact(store, trigger, fallback)
        _write_state(store, session_id)
        return fallback
    try:
        question = backend.condense([_question_prompt(trigger, text)]).strip()
    except Exception:
        return None
    if not question:
        return None
    _write_state(store, session_id)
    return question


def _trigger_reason(
    *,
    classifier_result: tuple[str, float] | None,
    contradiction_detected: bool,
    repeated_correction: bool,
) -> str | None:
    if classifier_result is not None:
        label, confidence = classifier_result
        if label == "unknown" or confidence < LOW_CONFIDENCE_THRESHOLD:
            return f"low classifier confidence ({confidence:.2f})"
    if contradiction_detected:
        return "contradiction detected"
    if repeated_correction:
        return "repeated correction"
    return None


def _backend_available(backend: _QuestionBackend) -> bool:
    try:
        return backend.is_available()
    except Exception:
        return False


def _question_prompt(trigger: str, text: str) -> str:
    return (
        "Generate exactly one short clarification question for Cursor to ask the user before saving memory. "
        f"Trigger: {trigger}.\n"
        f"Context: {text.strip()}"
    )


def _already_asked_this_session(store: Path, session_id: str) -> bool:
    state = _read_state(store)
    return str(state.get("last_question_session_id") or "") == session_id


def _read_state(store: Path) -> dict[str, object]:
    path = store / STATE_FILENAME
    if not path.exists():
        return {}
    try:
        data = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in cast(dict[object, object], data).items()}


def _write_state(store: Path, session_id: str) -> None:
    payload = {
        "last_question_session_id": session_id,
        "last_question_ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    atomic_write(store / STATE_FILENAME, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _fallback_question(trigger: str, text: str) -> str:
    snippet = text.strip()
    if len(snippet) > 160:
        snippet = snippet[:157] + "..."
    suffix = f" Context: {snippet}" if snippet else ""
    return (
        f"I noticed an ambiguity ({trigger}) but cannot reach the question backend. "
        f"Could you clarify what I should remember and where it belongs?{suffix}"
    )


def _write_pending_question_artifact(store: Path, trigger: str, question: str) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    body = f"# Pending memory question\n\n_Generated: {ts}_\n_Reason: {trigger}_\n\n{question}\n"
    atomic_write(store / PENDING_QUESTION_FILENAME, body)
    meta = {"ts": ts, "reason": trigger, "question": question}
    atomic_write(
        store / PENDING_QUESTION_META_FILENAME,
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
    )


__all__ = ["maybe_active_question"]
