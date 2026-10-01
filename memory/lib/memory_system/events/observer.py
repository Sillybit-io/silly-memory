"""Observation rendering — turn raw events into dated bullets in observations.md.

Read this first if you are new to the codebase:
  - The observer reads pending events from ``events.jsonl``, classifies each
    line as a decision/action/observation/etc., dedupes against a content
    hash so the same prompt isn't recorded twice, and appends a short bullet
    to ``observations.md``.
  - The full AI reply text branches off to ``learning.ai_text_log`` via
    ``append_ai_text`` so the correction detector has a faithful copy of
    the response to diff against — observations.md keeps only the short
    bullets for the distiller.
  - Explicit "remember that …" prompts are stored first, from every unobserved
    event and regardless of the threshold below, through
    ``learning.explicit``. That pass never moves the observer cursor, so if it
    fails the prompts are retried on the next run; writes are idempotent, so a
    replay does not duplicate a fact.
  - Passive observation is rate-limited by an unobserved-token estimate; if the
    delta is below the configured threshold it returns without rewriting,
    which is how hook overhead stays bounded. Explicit prompts it has already
    handled do not also become passive prompt bullets.

Public interface (imported elsewhere): ``run_observer``, ``_classify_line``
    (consumed by ``reflection.reflector_v2``).
Depends on: events.events, system.config, learning.ai_text_log,
    learning.explicit, index, paths, safety.
Used by: events.worker (calls ``run_observer``), reflection.reflector_v2
    (re-uses ``_classify_line``).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import cast

from .events import content_hash, estimate_unobserved_tokens, rotate_events
from memory_system.system.config import load_config
from memory_system.index import rebuild_index
from memory_system.learning.ai_text_log import append_ai_text
from memory_system.learning.explicit import extract_explicit_fact, store_explicit_fact
from memory_system.paths import ensure_layout, lock_path, workspace_store
from memory_system.safety import LockTimeout, atomic_write, file_lock

PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _safe_id(line: str) -> str:
    try:
        return json.loads(line).get("id", "")
    except json.JSONDecodeError:
        return ""


def _extract_prompts(events: list[dict]) -> list[str]:
    out = []
    for ev in events:
        p = ev.get("payload", {})
        if "prompt" in p and p["prompt"]:
            out.append(str(p["prompt"])[:500])
        if "text" in p and p.get("hook") == "afterAgentResponse":
            out.append(str(p["text"])[:300])
    return out


def _classify_line(line: str) -> tuple[str, str]:
    lower = line.lower()
    if any(w in lower for w in ("decided", "decision", "agreed", "mandate")):
        return "decision", "medium"
    if any(w in lower for w in ("action", "owner:", "due:", "follow up", "follow-up")):
        return "action-item", "high"
    if any(w in lower for w in ("prefer", "always", "never", "hard rule")):
        return "preference", "medium"
    if re.search(r"\b(is|are)\s+(the|a)\s+", lower) and any(w in lower for w in ("pm", "lead", "director", "engineer", "tl", "el")):
        return "stakeholder", "medium"
    if any(w in lower for w in ("milestone", "delivered", "completed")):
        return "milestone", "low"
    if any(w in lower for w in ("focus", "active", "current")):
        return "active-focus", "medium"
    return "observation", "low"


def _unobserved_events(store: Path, meta_path: Path) -> list[dict]:
    last_id = ""
    if meta_path.exists():
        last_id = json.loads(meta_path.read_text(encoding="utf-8")).get("last_event_id", "")

    events_path = store / "events.jsonl"
    if not events_path.exists():
        return []
    raw_lines = [ln for ln in events_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # Defensive: if a marker is set but no longer present (e.g. trimmed by event
    # rotation), don't get stuck skipping everything — treat all lines as new.
    if last_id and not any(_safe_id(ln) == last_id for ln in raw_lines):
        last_id = ""

    events: list[dict] = []
    seen = not last_id
    for line in raw_lines:
        rec = json.loads(line)
        if not seen:
            if rec.get("id") == last_id:
                seen = True
            continue
        events.append(rec)
    return events


def _store_explicit_prompts(workspace_root: Path, events: list[dict]) -> tuple[set[str], bool, bool]:
    """Store every explicit prompt in ``events``.

    Returns the ids of the explicit prompt events, whether any fact was
    written, and whether every write succeeded.
    """
    handled: set[str] = set()
    touched: dict[Path, None] = {}
    all_saved = True
    for rec in events:
        if rec.get("hook") != "beforeSubmitPrompt":
            continue
        fact = extract_explicit_fact(str(rec.get("payload", {}).get("prompt", "")))
        if fact is None:
            continue
        handled.add(str(rec.get("id", "")))
        try:
            bank_file = store_explicit_fact(workspace_root, fact, reindex=False)
        except (LockTimeout, OSError) as exc:
            all_saved = False
            print(f"observer: explicit fact not saved yet, will retry: {exc}", file=sys.stderr)
            continue
        if bank_file is not None:
            touched[bank_file.parent.parent] = None
    for store in touched:
        try:
            _ = rebuild_index(store, store.name)
        except (sqlite3.Error, OSError) as exc:
            print(f"observer: search index not updated for {store}: {exc}", file=sys.stderr)
    return handled, bool(touched), all_saved


def run_observer(workspace_root: Path, force: bool = False) -> bool:
    cfg = load_config()
    store = workspace_store(workspace_root)
    ensure_layout(store)
    meta_path = store / ".observer_state.json"
    events = _unobserved_events(store, meta_path)
    explicit_ids, explicit_written, explicit_saved = _store_explicit_prompts(workspace_root, events)
    # A failed explicit write keeps the cursor where it is, so its prompt is retried.
    if not explicit_saved:
        return explicit_written

    threshold = int(cast(int, cfg.get("observer_token_threshold", 6000)))
    if not force and estimate_unobserved_tokens(store) < threshold:
        return explicit_written
    if not events:
        return explicit_written

    # File-edit / shell activity is low-signal noise that the distiller already
    # skips; only record it when explicitly enabled (e.g. for code-state debugging).
    observe_activity = bool(cfg.get("observe_activity", False))
    # Agent responses are recorded by default (they often summarize confirmed
    # facts); set observe_agent_responses=false to drop them as noise.
    observe_agent_responses = bool(cfg.get("observe_agent_responses", True))

    today = date.today().isoformat()
    bullets: list[str] = []
    last_prompts: dict[str, str] = {}
    for rec in events[-50:]:
        hook = rec.get("hook", "")
        payload = rec.get("payload", {})
        conv_id = str(rec.get("conversation_id") or payload.get("conversation_id") or "")
        if hook == "beforeSubmitPrompt":
            last_prompts[conv_id] = str(payload.get("prompt", ""))
            if str(rec.get("id", "")) in explicit_ids:
                continue
            text = str(payload.get("prompt", ""))[:400]
            cat, pri = _classify_line(text)
            emoji = PRIORITY_EMOJI.get(pri, "🟢")
            bullets.append(f"- {emoji} [{today}] #{cat}: User prompt snippet — {text}")
        elif hook == "afterAgentResponse":
            if observe_agent_responses:
                text = str(payload.get("text", ""))[:300]
                cat, pri = _classify_line(text)
                emoji = PRIORITY_EMOJI.get(pri, "🟢")
                bullets.append(f"- {emoji} [{today}] #{cat}: Agent response — {text}")
            # ai-text-log captures the full reply for the correction detector
            # regardless of observe_agent_responses (the flag only gates the
            # short observations.md bullet, not downstream learning input).
            append_ai_text(
                store,
                conversation_id=conv_id,
                prompt=last_prompts.get(conv_id, ""),
                response_text=str(payload.get("text", "")),
                ts_iso=str(rec.get("ts", "")),
            )
        elif hook == "afterFileEdit" and observe_activity:
            fp = payload.get("file_path", "")
            bullets.append(f"- 🟡 [{today}] #file-edit: `{fp}`")
        elif hook == "afterShellExecution" and observe_activity:
            cmd = str(payload.get("command", ""))[:120]
            bullets.append(f"- 🟢 [{today}] #shell: `{cmd}`")

    # Nothing worth recording in this batch (e.g. it was only activity events
    # while observe_activity is off) — advance the marker but write no block, so
    # we never emit an empty dated section.
    if not bullets:
        with file_lock(lock_path(store)):
            atomic_write(meta_path, json.dumps({"last_event_id": events[-1]["id"]}, indent=2))
        rotate_events(store, events[-1]["id"], int(cast(int, cfg.get("events_max_lines", 5000))))
        return True

    block = "\n".join([f"## {today}", ""] + bullets) + "\n"
    obs_path = store / "observations.md"
    with file_lock(lock_path(store)):
        existing = obs_path.read_text(encoding="utf-8") if obs_path.exists() else "# Observations\n\n"
        if content_hash(block) not in {content_hash(b) for b in existing.split("\n## ")}:
            atomic_write(obs_path, existing.rstrip() + "\n\n" + block)
        atomic_write(meta_path, json.dumps({"last_event_id": events[-1]["id"]}, indent=2))

    rotate_events(store, events[-1]["id"], int(cast(int, cfg.get("events_max_lines", 5000))))
    return True
