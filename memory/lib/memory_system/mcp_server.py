"""Stdio MCP server — ``memory_recall``, ``memory_tasks``, and ``memory_add`` for any MCP client.

Read this first if you are new to the codebase:
  - JSON-RPC 2.0 over stdin/stdout, one JSON object per line, MCP revision
    2025-06-18 with the classic ``initialize`` handshake. Stdout carries
    protocol messages only; everything else, including anything an engine
    module prints, goes to stderr. The server exits when stdin closes.
  - A reader thread feeds one dispatcher, so ping and other requests are
    answered while a workspace-bound call waits for Cursor's roots.
  - The workspace comes from ``--workspace`` when given, else per client:
    Claude Code's ``CLAUDE_PROJECT_DIR``, Cursor's negotiated roots
    (``roots/list``), or the current directory for OpenCode (which starts a
    project's servers where it was launched) and a standalone launch. Every
    choice goes through
    ``paths.resolve_workspace_root``. Without a usable workspace, workspace
    calls fail with a tool error; searches across every store still work.
  - Bad requests or arguments are JSON-RPC errors; storage failures are tool
    results with ``isError``. Tool text is capped at 4,000 characters, after
    every requested store has been searched.
  - The tools call the same functions as the CLI (``index.recall_all``,
    ``recall_hybrid.search_all``, ``status.main.render_tasks``,
    ``learning.explicit.store_explicit_fact``, ``render_rule_file``) and hold
    no store lock around them.

Public interface (imported elsewhere): ``main``, ``McpServer``, ``TOOLS``,
    ``PROTOCOL_VERSION``.
Depends on: config, index, learning.explicit, paths, recall.context_pack,
    recall.recall_hybrid, safety, status.main, system.version.
Used by: ``bin/memory-mcp``.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from memory_system.system.config import memory_home
from memory_system.index import recall_all
from memory_system.learning.explicit import store_explicit_fact
from memory_system.paths import GLOBAL_ID, resolve_workspace_root
from memory_system.recall.context_pack import render_rule_file
from memory_system.recall.recall_hybrid import StoreSearchError, search_all
from memory_system.safety import LockTimeout
from memory_system.status.main import render_tasks
from memory_system.system.version import get_code_version

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "silly-memory"
CLIENTS = ("standalone", "cursor", "claude-code", "opencode")
ROOTS_TIMEOUT = 5.0
RESPONSE_CAP = 4000
TRUNCATED = "\n[truncated]"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
NOT_INITIALIZED = -32002

TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory_recall",
        "title": "Recall memory",
        "description": (
            "Search silly-memory for stored facts, decisions, preferences, and notes. "
            "scope 'workspace' (default) searches this project plus global memory; "
            "'all' searches every project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "description": "Keywords to search for."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10, "description": "Maximum hits."},
                "scope": {"type": "string", "enum": ["workspace", "all"], "default": "workspace"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_tasks",
        "title": "List memory tasks",
        "description": "List action items from silly-memory for this project, or every project with all=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "all"], "default": "open"},
                "tag": {"type": "string", "description": "Only tasks with this tag."},
                "owner": {"type": "string", "description": "Only tasks whose owner contains this text."},
                "all": {"type": "boolean", "default": False, "description": "Include every project."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_add",
        "title": "Add to memory",
        "description": (
            "Store one fact in silly-memory at full confidence. scope 'auto' (default) routes it by "
            "category; 'workspace' or 'global' forces the store. Text inside <private>…</private> is never stored."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "description": "The fact to remember."},
                "scope": {"type": "string", "enum": ["auto", "workspace", "global"], "default": "auto"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]
_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}
_JSON_TYPES: dict[str, tuple[type, ...]] = {"string": (str,), "integer": (int,), "boolean": (bool,)}
_ROOTS_ID_PREFIX = "silly-memory-roots-"
_TICK = object()


class ToolError(Exception):
    """A tool call that cannot be carried out; reported as an ``isError`` result."""


def _validate(schema: dict[str, Any], arguments: object) -> dict[str, Any]:
    """Arguments with defaults applied; raises ValueError describing the first problem."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    properties: dict[str, dict[str, Any]] = schema["properties"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise ValueError(f"unknown argument: {unknown[0]}")
    for name in schema.get("required", []):
        if name not in arguments:
            raise ValueError(f"missing required argument: {name}")
    out: dict[str, Any] = {}
    for name, spec in properties.items():
        if name not in arguments:
            if "default" in spec:
                out[name] = spec["default"]
            continue
        value = arguments[name]
        expected = _JSON_TYPES[spec["type"]]
        # bool is an int in Python, but not a JSON integer.
        if not isinstance(value, expected) or (spec["type"] == "integer" and isinstance(value, bool)):
            raise ValueError(f"{name} must be a {spec['type']}")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"{name} must be one of {', '.join(spec['enum'])}")
        if "minLength" in spec and len(value.strip()) < spec["minLength"]:
            raise ValueError(f"{name} must not be empty")
        if "minimum" in spec and value < spec["minimum"]:
            raise ValueError(f"{name} must be at least {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise ValueError(f"{name} must be at most {spec['maximum']}")
        out[name] = value
    return out


_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")


def _percent_decode(text: str) -> str | None:
    raw = text.encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x25:
            escape = raw[i + 1 : i + 3]
            if len(escape) != 2 or not set(escape) <= _HEX_DIGITS:
                return None
            out.append(int(escape, 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def decode_file_uri(uri: object) -> Path | None:
    """The absolute local path of a ``file://`` URI, or None for anything else."""
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return None
    host, slash, rest = uri[len("file://") :].partition("/")
    if not slash or host not in ("", "localhost"):
        return None
    path = _percent_decode("/" + rest.split("?", 1)[0].split("#", 1)[0])
    if not path or not os.path.isabs(path):
        return None
    return Path(path)


def _cap(text: str) -> str:
    if len(text) <= RESPONSE_CAP:
        return text
    return text[: RESPONSE_CAP - len(TRUNCATED)].rstrip() + TRUNCATED


def _format_hits(hits: Sequence[Mapping[str, object]], label_key: str) -> str:
    if not hits:
        return "No matches."
    lines: list[str] = []
    for hit in hits:
        lines.append(f"[{hit.get(label_key)}] {hit.get('path')} / {hit.get('section')}")
        lines.append(f"  {hit.get('snippet')}")
    return "\n".join(lines)


class McpServer:
    def __init__(
        self,
        out: TextIO,
        *,
        client: str = "standalone",
        workspace: str | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.out = out
        self.client = client
        self.explicit_workspace = workspace
        self.env = dict(os.environ if env is None else env)
        self.cwd = (cwd or Path.cwd()).resolve()
        self.initialize_seen = False
        self.initialized = False
        self.client_capabilities: dict[str, Any] = {}
        # Cursor roots: "idle" before negotiation, then "pending", "ready", or "failed".
        self.roots_state = "idle"
        self.roots_error = ""
        self.roots: list[Path] = []
        self.roots_request_id: str | None = None
        self.roots_deadline = 0.0
        self.roots_requests_sent = 0
        self.deferred: list[tuple[object, str, dict[str, Any]]] = []
        self.bound_root: Path | None = None

    # --- transport -------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        self.out.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.out.flush()

    def result(self, request_id: object, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id: object, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def serve(self, stream: TextIO) -> int:
        lines: queue.Queue[object] = queue.Queue()

        def read() -> None:
            for line in stream:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read, name="mcp-stdin", daemon=True).start()
        while True:
            timeout = max(0.0, self.roots_deadline - time.monotonic()) if self.roots_state == "pending" else None
            try:
                item = lines.get(timeout=timeout)
            except queue.Empty:
                item = _TICK
            if item is None:
                return 0
            if isinstance(item, str):
                self.handle_line(item)
            self._expire_roots()

    # --- message routing -------------------------------------------------------

    def handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            self.error(None, PARSE_ERROR, "Parse error")
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            request_id = message.get("id") if isinstance(message, dict) else None
            self.error(request_id if isinstance(request_id, (str, int)) else None, INVALID_REQUEST, "Invalid Request")
            return
        method = message.get("method")
        if isinstance(method, str):
            if "id" not in message:
                self.notification(method, message.get("params"))
            elif isinstance(message["id"], (str, int)) and not isinstance(message["id"], bool):
                self.request(message["id"], method, message.get("params"))
            else:
                self.error(None, INVALID_REQUEST, "Invalid Request: id must be a string or number")
        elif "id" in message and ("result" in message or "error" in message):
            self.response(message)
        elif "id" in message:
            self.error(message["id"] if isinstance(message["id"], (str, int)) else None, INVALID_REQUEST, "Invalid Request")

    def request(self, request_id: object, method: str, params: object) -> None:
        if method == "ping":
            self.result(request_id, {})
        elif method == "initialize":
            self.initialize(request_id, params)
        elif not self.initialized:
            self.error(request_id, NOT_INITIALIZED, "Server not initialized")
        elif method == "tools/list":
            self.result(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            self.tools_call(request_id, params)
        else:
            self.error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def notification(self, method: str, params: object) -> None:
        if method == "notifications/initialized":
            if self.initialize_seen and not self.initialized:
                self.initialized = True
                if self.client == "cursor" and not self.explicit_workspace:
                    self.request_roots()
        elif method == "notifications/roots/list_changed":
            if self.initialized and self.client == "cursor" and not self.explicit_workspace:
                self.request_roots()

    def response(self, message: dict[str, Any]) -> None:
        if message.get("id") != self.roots_request_id or self.roots_state != "pending":
            return
        result = message.get("result")
        roots = result.get("roots") if isinstance(result, dict) else None
        if "error" in message or not isinstance(roots, list):
            self.fail_roots("Cursor could not list the workspace roots")
            return
        decoded = [decode_file_uri(root.get("uri")) for root in roots if isinstance(root, dict)]
        self.roots = [path for path in decoded if path is not None]
        self.roots_state = "ready"
        self.bound_root = None
        self.run_deferred()

    # --- lifecycle ---------------------------------------------------------------

    def initialize(self, request_id: object, params: object) -> None:
        if not isinstance(params, dict):
            self.error(request_id, INVALID_PARAMS, "initialize needs params")
            return
        capabilities = params.get("capabilities")
        self.client_capabilities = capabilities if isinstance(capabilities, dict) else {}
        self.initialize_seen = True
        # Answer with the one revision this server implements; the client decides whether to continue.
        self.result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": get_code_version()},
            },
        )

    def request_roots(self) -> None:
        self.bound_root = None
        if "roots" not in self.client_capabilities:
            self.fail_roots("Cursor did not offer workspace roots")
            return
        self.roots_requests_sent += 1
        self.roots_request_id = f"{_ROOTS_ID_PREFIX}{self.roots_requests_sent}"
        self.roots_state = "pending"
        self.roots_deadline = time.monotonic() + ROOTS_TIMEOUT
        self.send({"jsonrpc": "2.0", "id": self.roots_request_id, "method": "roots/list"})

    def fail_roots(self, reason: str) -> None:
        self.roots_state = "failed"
        self.roots_error = reason
        self.roots = []
        self.run_deferred()

    def _expire_roots(self) -> None:
        if self.roots_state == "pending" and time.monotonic() >= self.roots_deadline:
            self.fail_roots(f"Cursor did not answer the workspace roots request within {ROOTS_TIMEOUT:g} seconds")

    def run_deferred(self) -> None:
        waiting, self.deferred = self.deferred, []
        for request_id, name, arguments in waiting:
            self.execute(request_id, name, arguments)

    # --- workspace binding -------------------------------------------------------

    def _chosen_workspace(self) -> Path:
        if self.explicit_workspace:
            return Path(self.explicit_workspace).expanduser()
        if self.client in ("standalone", "opencode"):
            return self.cwd
        if self.client == "claude-code":
            project = self.env.get("CLAUDE_PROJECT_DIR")
            if project:
                return Path(project).expanduser()
            raise ToolError("No workspace: Claude Code did not set CLAUDE_PROJECT_DIR for this server.")
        if self.roots_state != "ready":
            raise ToolError(f"No workspace: {self.roots_error or 'Cursor has not listed its workspace roots yet'}.")
        if not self.roots:
            raise ToolError("No workspace: Cursor listed no local workspace roots.")
        if len(self.roots) == 1:
            return self.roots[0]
        containing = [root for root in self.roots if root == self.cwd or root in self.cwd.parents]
        if len(containing) == 1:
            return containing[0]
        listed = ", ".join(str(root) for root in self.roots)
        raise ToolError(f"Ambiguous workspace: Cursor listed several roots ({listed}); start the server with --workspace.")

    def workspace(self) -> Path:
        if self.bound_root is None:
            self.bound_root = resolve_workspace_root(self._chosen_workspace())
        return self.bound_root

    # --- tools ---------------------------------------------------------------------

    def tools_call(self, request_id: object, params: object) -> None:
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            self.error(request_id, INVALID_PARAMS, "tools/call needs a tool name")
            return
        name = params["name"]
        tool = _TOOLS_BY_NAME.get(name)
        if tool is None:
            self.error(request_id, INVALID_PARAMS, f"Unknown tool: {name}")
            return
        try:
            arguments = _validate(tool["inputSchema"], params.get("arguments"))
        except ValueError as exc:
            self.error(request_id, INVALID_PARAMS, f"Invalid arguments for {name}: {exc}")
            return
        if self.roots_state == "pending" and self._needs_workspace(name, arguments):
            self.deferred.append((request_id, name, arguments))
            return
        self.execute(request_id, name, arguments)

    @staticmethod
    def _needs_workspace(name: str, arguments: dict[str, Any]) -> bool:
        if name == "memory_recall":
            return arguments["scope"] == "workspace"
        if name == "memory_tasks":
            return not arguments["all"]
        return True

    def execute(self, request_id: object, name: str, arguments: dict[str, Any]) -> None:
        try:
            if name == "memory_recall":
                text, is_error = self.recall(arguments), False
            elif name == "memory_tasks":
                text, is_error = self.tasks(arguments), False
            else:
                text, is_error = self.add(arguments), False
        except ToolError as exc:
            text, is_error = str(exc), True
        except LockTimeout:
            text, is_error = "The memory store is busy; try again in a moment.", True
        except (StoreSearchError, sqlite3.Error, OSError) as exc:
            text, is_error = f"Memory storage error: {exc}", True
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            text, is_error = f"Internal error: {exc}", True
        self.result(request_id, {"content": [{"type": "text", "text": _cap(text)}], "isError": is_error})

    def recall(self, arguments: dict[str, Any]) -> str:
        if arguments["scope"] == "all":
            return _format_hits(search_all(arguments["query"], limit=arguments["limit"]), "workspace_label")
        return _format_hits(recall_all(self.workspace(), arguments["query"], limit=arguments["limit"]), "scope")

    def tasks(self, arguments: dict[str, Any]) -> str:
        # All-workspace listing never reads its workspace argument, so it needs no binding.
        root = memory_home() if arguments["all"] else self.workspace()
        return render_tasks(
            root,
            status=arguments["status"],
            tag=arguments.get("tag"),
            owner=arguments.get("owner"),
            all_workspaces=arguments["all"],
        ).rstrip()

    def add(self, arguments: dict[str, Any]) -> str:
        root = self.workspace()
        bank_file = store_explicit_fact(root, arguments["text"], arguments["scope"])
        if bank_file is None:
            raise ToolError("Nothing stored: the text is empty or entirely private.")
        where = "global memory" if bank_file.parent.parent.name == GLOBAL_ID else f"the memory for {root}"
        saved = f"Saved to {bank_file.name} in {where}."
        try:
            _ = render_rule_file(root)
        except Exception as exc:
            return f"{saved} Refreshing the project rules failed ({exc}); they refresh at the next session start or end."
        return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-mcp", description="silly-memory MCP server over stdio")
    parser.add_argument("--client", choices=CLIENTS, default="standalone", help="how the workspace is found")
    parser.add_argument("--workspace", default=None, help="workspace directory; overrides the client's own")
    args = parser.parse_args(argv)
    protocol_out = sys.stdout
    # Anything else that prints must not corrupt the protocol stream.
    sys.stdout = sys.stderr
    server = McpServer(protocol_out, client=args.client, workspace=args.workspace)
    return server.serve(sys.stdin)
