"""Background worker — the single-runner pipeline that turns events into memory.

Read this first if you are new to the codebase:
  - ``process_queue(workspace)`` is the only entry point. It acquires a
    non-blocking worker lock and runs four passes in order: observe,
    distill from observations, reflect, then refresh the stored pack, every
    configured tool's project rule, and the search index. It returns True
    only when all of that completed; False means another worker was busy
    (nothing was taken off the queue) or a lock timed out mid-pass.
  - The non-blocking lock is the bug-fix: an earlier version spawned
    detached workers that survived restarts and thrashed the store. The
    current rule is strict — if the lock is held, return immediately.
  - Never spawns subprocesses. The work runs inline in whatever process
    calls it (CLI invocation or session-boundary hook); the JSON queue
    file holds the to-do list across calls.
  - After a completed ``preCompact`` pass, the conversation's handoff note is
    written (``recall.handoff``) so the session can pick up where it was
    after compaction. A busy pass leaves any earlier note as it is.

Public interface (imported elsewhere): ``process_queue``, ``handle_hook_job``.
Depends on: events.events, events.observer, recall.context_pack,
    recall.distiller, recall.handoff, index, paths, reflection.reflector,
    safety, status.main.
Used by: ``bin/memory`` (hooks and the ``process`` command).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .events import enqueue_job
from .observer import run_observer
from memory_system.recall.context_pack import render_rule_file
from memory_system.recall.distiller import distill_from_observations
from memory_system.recall.handoff import write_handoff
from memory_system.index import rebuild_index
from memory_system.paths import global_store, workspace_store
from memory_system.reflection.reflector import run_reflector
from memory_system.safety import LockTimeout, file_lock
from memory_system.status.main import render_tasks

HANDOFF_PROMPTS = 5
HANDOFF_PROMPT_CHARS = 600


def _worker_lock(store: Path) -> Path:
    return store / ".worker.lock"


def _session_prompts(store: Path, source: str, conversation_id: str) -> list[str]:
    """The last prompts of one conversation of one tool, already sanitized in the event log."""
    events = store / "events.jsonl"
    if not events.exists():
        return []
    prompts: list[str] = []
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("hook") != "beforeSubmitPrompt" or rec.get("conversation_id") != conversation_id:
            continue
        if rec.get("source") != source:
            continue
        prompt = str(rec.get("payload", {}).get("prompt", "")).strip()
        if prompt:
            prompts.append(prompt[:HANDOFF_PROMPT_CHARS])
    return prompts[-HANDOFF_PROMPTS:]


def _handoff_body(workspace_root: Path, store: Path, source: str, conversation_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sections = [f"## Session\n\n{source} conversation `{conversation_id}`, compacted {stamp}."]
    work_state = store / "work-state.md"
    if work_state.exists() and work_state.read_text(encoding="utf-8").strip():
        sections.append("## Work state\n\n" + work_state.read_text(encoding="utf-8").strip())
    sections.append("## Open tasks\n\n" + render_tasks(workspace_root, status="open").strip())
    prompts = _session_prompts(store, source, conversation_id)
    if prompts:
        numbered = "\n".join(f"{n}. {prompt}" for n, prompt in enumerate(prompts, start=1))
        sections.append("## Recent prompts in this conversation\n\n" + numbered)
    return "\n\n".join(sections) + "\n"


def _write_session_handoff(
    workspace_root: Path, source: str | None, conversation_id: str | None, token: str | None
) -> str | None:
    """Write the conversation's handoff note; return its token, or None when none was written."""
    if not source or not conversation_id:
        return None
    # OpenCode ties each note to one compaction attempt, so its notes need the attempt's token.
    if source == "opencode" and not token:
        return None
    store = workspace_store(workspace_root)
    body = _handoff_body(workspace_root, store, source, conversation_id)
    try:
        return write_handoff(store, source, conversation_id, body, token=token)
    except LockTimeout:
        return None


def process_queue(workspace_root: Path) -> bool:
    """Run the heavy pass once, guarded so only ONE worker ever runs at a time.

    Uses a non-blocking worker lock: if another worker is already processing this
    store, we return immediately instead of piling up (the prior bug spawned
    detached workers that survived restarts and thrashed). Never spawns
    subprocesses — runs inline in whatever process calls it (CLI or session-
    boundary hook). Returns True only when processing and the rule refresh
    completed.
    """
    store = workspace_store(workspace_root)
    try:
        with file_lock(_worker_lock(store), timeout=0.1):
            _process_queue_locked(workspace_root, store)
    except LockTimeout:
        return False
    return True


def _process_queue_locked(workspace_root: Path, store: Path) -> None:
    queue = store / "queues" / "pending.jsonl"
    if queue.exists():
        lines = [ln for ln in queue.read_text(encoding="utf-8").splitlines() if ln.strip()]
        queue.write_text("", encoding="utf-8")
        for line in lines:
            try:
                job = json.loads(line)
            except json.JSONDecodeError:
                continue
            jtype = job.get("type")
            if jtype == "observe":
                run_observer(workspace_root, force=job.get("force", False))
            elif jtype == "reflect":
                run_reflector(workspace_root, force=job.get("force", False))
            elif jtype == "distill":
                distill_from_observations(workspace_root)
            elif jtype == "render":
                render_rule_file(workspace_root)
    run_observer(workspace_root)
    run_reflector(workspace_root)
    distill_from_observations(workspace_root)
    render_rule_file(workspace_root)
    rebuild_index(store, store.name)
    rebuild_index(global_store(), "_global")


def handle_hook_job(
    workspace_root: Path,
    job_type: str,
    *,
    source: str | None = None,
    conversation_id: str | None = None,
    handoff_token: str | None = None,
) -> str | None:
    """Cheap, non-blocking hook handling — NEVER spawns subprocesses.

    Command hooks only enqueue (the event itself is already captured by the
    caller). Heavy work runs inline ONLY at session boundaries (sessionEnd /
    preCompact), guarded so it can't pile up. sessionStart just refreshes the
    rule file from already-persisted state (cheap). ``source`` and
    ``conversation_id`` identify the session that sent the hook, and
    ``handoff_token`` is the OpenCode compaction attempt a preCompact belongs
    to. Returns the token of a newly written handoff note, if any.
    """
    identity = {key: value for key, value in (("source", source), ("conversation_id", conversation_id)) if value}
    enqueue_job(workspace_root, job_type, identity or None)
    if job_type == "sessionStart":
        render_rule_file(workspace_root)
    elif job_type in ("sessionEnd", "preCompact"):
        completed = process_queue(workspace_root)
        # Only a completed pass may write the note; a busy one keeps the last note.
        if job_type == "preCompact" and completed:
            return _write_session_handoff(workspace_root, source, conversation_id, handoff_token)
    # All other (per-turn / per-command) hooks: capture only, no processing.
    return None
