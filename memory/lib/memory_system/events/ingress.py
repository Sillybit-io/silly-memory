"""Hook ingress — turn each tool's native hook payload into canonical events.

Read this first if you are new to the codebase:
  - The rest of the engine speaks Cursor's hook vocabulary (``sessionStart``,
    ``beforeSubmitPrompt``, ``afterAgentResponse``, ``afterFileEdit``,
    ``afterShellExecution``, ``stop``, ``preCompact``, ``sessionEnd``) with
    ``workspace_roots`` and ``conversation_id``. ``normalize`` maps Cursor,
    Claude Code, and OpenCode payloads onto it and adds ``source``.
  - Claude Code and OpenCode events are rebuilt from an allowlist, so native
    fields such as ``last_assistant_message`` or ``tool_input`` never reach
    the event log beside their sanitized canonical copies. Cursor payloads
    already use the canonical names and pass through.
  - One native hook can become several events: Claude Code's ``Stop`` is the
    reply (``afterAgentResponse``) followed by ``stop``.
  - Every workspace root goes through ``paths.resolve_workspace_root`` so a
    session started in a subdirectory shares the repository's store. A Cursor
    root that already has its own marker keeps it, so existing Cursor
    workspaces keep their stores.
  - ``format_output`` builds the one JSON object each tool expects back.

Public interface (imported elsewhere): ``TOOLS``, ``CANONICAL_HOOKS``,
    ``detect_tool``, ``normalize``, ``format_output``.
Depends on: paths.
Used by: ``bin/memory`` (``memory hook``).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from memory_system.paths import resolve_workspace_root, workspace_marker

TOOLS = ("cursor", "claude-code", "opencode")
CANONICAL_HOOKS = frozenset(
    {
        "sessionStart",
        "beforeSubmitPrompt",
        "afterAgentResponse",
        "afterFileEdit",
        "afterShellExecution",
        "stop",
        "preCompact",
        "sessionEnd",
    }
)
SESSION_SOURCES = frozenset({"startup", "resume", "clear", "compact", "fork"})
_CLAUDE_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_OPENCODE_TEXT_FIELDS = ("prompt", "text", "file_path", "command")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def detect_tool(payload: dict[str, Any]) -> str | None:
    """The tool a payload came from when ``--tool`` was not given."""
    declared = payload.get("tool")
    if declared in TOOLS:
        return str(declared)
    if payload.get("workspace_roots"):
        return "cursor"
    if payload.get("cwd") and payload.get("session_id"):
        return "claude-code"
    return None


def _first_root(roots: object) -> str | None:
    if isinstance(roots, list) and roots and isinstance(roots[0], str) and roots[0]:
        return roots[0]
    return None


def _opaque_id(value: object) -> str | None:
    return value if isinstance(value, str) and _OPAQUE_ID.match(value) else None


def _absolute(path: str, base: str) -> str:
    return path if os.path.isabs(path) else os.path.join(base, path)


def _cursor(payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = _first_root(payload.get("workspace_roots"))
    if root is None or not payload.get("hook_event_name"):
        return []
    given = Path(root).expanduser()
    own_marker = workspace_marker(given).is_file()
    resolved = given.resolve() if own_marker else resolve_workspace_root(given)
    event = dict(payload)
    event["workspace_roots"] = [str(resolved), *payload["workspace_roots"][1:]]
    event["source"] = "cursor"
    if event["hook_event_name"] == "sessionStart":
        event.setdefault("session_source", "startup")
    return [event]


def _claude(payload: dict[str, Any]) -> list[dict[str, Any]]:
    cwd = payload.get("cwd")
    hook = payload.get("hook_event_name")
    if not isinstance(cwd, str) or not cwd or not isinstance(hook, str):
        return []
    session = payload.get("session_id")
    base: dict[str, Any] = {
        "workspace_roots": [str(resolve_workspace_root(Path(cwd)))],
        "conversation_id": session if isinstance(session, str) and session else None,
        "source": "claude-code",
    }
    if hook == "SessionStart":
        native = payload.get("source")
        return [{**base, "hook_event_name": "sessionStart", "session_source": native if native in SESSION_SOURCES else "startup"}]
    if hook == "UserPromptSubmit":
        prompt = payload.get("prompt")
        return [{**base, "hook_event_name": "beforeSubmitPrompt", "prompt": prompt if isinstance(prompt, str) else ""}]
    if hook == "PostToolUse":
        tool_name = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return []
        if tool_name in _CLAUDE_EDIT_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if isinstance(path, str) and path:
                return [{**base, "hook_event_name": "afterFileEdit", "file_path": _absolute(path, cwd)}]
        elif tool_name == "Bash":
            command = tool_input.get("command")
            if isinstance(command, str) and command:
                return [{**base, "hook_event_name": "afterShellExecution", "command": command}]
        return []
    if hook == "Stop":
        events: list[dict[str, Any]] = []
        reply = payload.get("last_assistant_message")
        if isinstance(reply, str) and reply.strip():
            events.append({**base, "hook_event_name": "afterAgentResponse", "text": reply})
        events.append({**base, "hook_event_name": "stop"})
        return events
    if hook == "PreCompact":
        return [{**base, "hook_event_name": "preCompact"}]
    if hook == "SessionEnd":
        return [{**base, "hook_event_name": "sessionEnd"}]
    return []


def _opencode(payload: dict[str, Any]) -> list[dict[str, Any]]:
    hook = payload.get("hook_event_name")
    root = _first_root(payload.get("workspace_roots"))
    if hook not in CANONICAL_HOOKS or root is None:
        return []
    conversation = payload.get("conversation_id")
    event: dict[str, Any] = {
        "hook_event_name": hook,
        "workspace_roots": [str(resolve_workspace_root(Path(root)))],
        "conversation_id": conversation if isinstance(conversation, str) and conversation else None,
        "source": "opencode",
    }
    for field in _OPENCODE_TEXT_FIELDS:
        value = payload.get(field)
        if isinstance(value, str):
            event[field] = value
    message_id = _opaque_id(payload.get("message_id"))
    if message_id:
        event["message_id"] = message_id
    if hook == "sessionStart":
        requested = payload.get("session_source")
        event["session_source"] = requested if requested in SESSION_SOURCES else "startup"
    if hook in ("sessionStart", "preCompact"):
        token = _opaque_id(payload.get("handoff_token"))
        if token:
            event["handoff_token"] = token
    return [event]


def normalize(tool: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Canonical events for one native hook payload; empty when it is not usable."""
    if not isinstance(payload, dict):
        return []
    if tool == "cursor":
        return _cursor(payload)
    if tool == "claude-code":
        return _claude(payload)
    if tool == "opencode":
        return _opencode(payload)
    return []


def format_output(tool: str, hook: str, additional_context: str | None) -> dict[str, Any]:
    """The single JSON object a tool expects back from a hook."""
    if hook != "sessionStart" or not additional_context:
        return {}
    if tool == "claude-code":
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": additional_context}}
    return {"additional_context": additional_context}
