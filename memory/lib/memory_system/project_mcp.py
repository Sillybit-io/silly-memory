"""The silly-memory MCP server entry in each project's own tool configuration.

Read this first if you are new to the codebase:
  - The server is off unless the user turned it on (``./install.sh --mcp``
    records ``"mcp": true`` in config.json): some teams may not use MCP without
    approval, and the memory skills cover the same three operations.
  - Nothing is registered globally. While the server is on, each wired tool's
    project file gains a ``silly-memory`` server at the project's session start:
    ``.mcp.json`` for Claude Code, ``.cursor/mcp.json`` for Cursor, and
    ``opencode.json`` for OpenCode.
    Claude Code also gets ``enabledMcpjsonServers: ["silly-memory"]`` in the
    project's ``.claude/settings.local.json`` so it does not ask; Cursor asks
    once per project, in the editor or with ``cursor-agent mcp enable``.
  - The files are meant to be committed, so an entry holds no machine path:
    ``sh`` finds the memory home when the server starts (``SILLY_MEMORY_HOME``,
    else ``~/.silly-memory``) and runs
    ``python3 <home>/bin/memory-mcp``. The server finds the project itself
    (Claude Code's ``CLAUDE_PROJECT_DIR``, Cursor's roots, OpenCode's launch
    directory).
  - Only our own entry is added, updated, or removed; every other key stays. An
    entry named ``silly-memory`` that does not run ``memory-mcp`` is someone
    else's and is never touched. A file that is not valid JSON, and a project
    configured through ``opencode.jsonc``, are left alone.

Public interface (imported elsewhere): ``mcp_enabled``, ``ensure_project_mcp``,
    ``remove_project_mcp``, ``registered_tools``, ``project_roots``,
    ``project_mcp_files``, ``SERVER_NAME``.
Depends on: config, safety.
Used by: ``bin/memory`` (sessionStart), ``cli.doctor``, ``install.sh`` (turning
    the server off), ``system/uninstall_transaction.py``.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from memory_system.system.config import load_config
from memory_system.safety import atomic_write

SERVER_NAME = "silly-memory"
CLAUDE_APPROVALS = Path(".claude") / "settings.local.json"
OPENCODE_SCHEMA = "https://opencode.ai/config.json"
# No "${" anywhere: Claude Code and Cursor expand that syntax themselves.
_LAUNCH = (
    'h="$SILLY_MEMORY_HOME"; [ -n "$h" ] || h="$HOME/.silly-memory"; '
    'exec python3 "$h/bin/memory-mcp" --client {client}'
)
# tool -> (project file, key holding the servers)
_FILES: dict[str, tuple[Path, str]] = {
    "claude-code": (Path(".mcp.json"), "mcpServers"),
    "cursor": (Path(".cursor") / "mcp.json", "mcpServers"),
    "opencode": (Path("opencode.json"), "mcp"),
}


def mcp_enabled() -> bool:
    """Whether the user turned the server on with ``./install.sh --mcp``."""
    return load_config().get("mcp") is True


def project_mcp_files(workspace_root: Path) -> dict[str, Path]:
    """Every tool's project MCP file, whether or not it exists."""
    return {tool: Path(workspace_root) / rel for tool, (rel, _) in _FILES.items()}


def server_entry(tool: str) -> dict[str, Any]:
    command = ["sh", "-c", _LAUNCH.format(client=tool)]
    if tool == "opencode":
        return {"type": "local", "command": command}
    return {"type": "stdio", "command": command[0], "args": command[1:]}


def is_ours(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    parts = entry.get("command"), entry.get("args")
    words = [w for part in parts for w in (part if isinstance(part, list) else [part]) if isinstance(w, str)]
    return any("memory-mcp" in word for word in words)


def project_roots(memory_home: Path) -> list[Path]:
    """Every tracked project folder that still exists, from each store's ``.meta.json``."""
    home = Path(memory_home)
    stores = sorted(p for p in home.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))) if home.is_dir() else []
    roots: list[Path] = []
    for store in stores:
        try:
            root = Path(json.loads((store / ".meta.json").read_text(encoding="utf-8"))["workspace_root"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if root.is_dir() and root not in roots:
            roots.append(root)
    return roots


def registered_tools(workspace_root: Path) -> set[str]:
    """The tools whose project file in ``workspace_root`` carries our server."""
    found = set()
    for tool, (rel, key) in _FILES.items():
        doc = _load(Path(workspace_root) / rel)
        servers = doc.get(key) if doc else None
        if isinstance(servers, dict) and is_ours(servers.get(SERVER_NAME)):
            found.add(tool)
    return found


def _load(path: Path) -> dict[str, Any] | None:
    """The file's JSON object, ``{}`` when missing, None when it must be left alone."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(data, indent=2) + "\n")


def _configured_tools() -> list[str]:
    tools = load_config().get("tools", [])
    return [t for t in tools if t in _FILES] if isinstance(tools, list) else []


def ensure_project_mcp(workspace_root: Path, tools: list[str] | None = None) -> list[str]:
    """Add or update the server in each wired tool's project file; returns what was left alone."""
    root = Path(workspace_root)
    wired = _configured_tools() if tools is None else [t for t in tools if t in _FILES]
    skipped: list[str] = []
    placed: set[str] = set()
    for tool in wired:
        rel, key = _FILES[tool]
        path = root / rel
        if tool == "opencode" and (root / "opencode.jsonc").exists() and not path.exists():
            skipped.append(f"{root / 'opencode.jsonc'}: configured as JSONC; add the silly-memory server by hand")
            continue
        doc = _load(path)
        servers = doc.get(key) if doc is not None else None
        if doc is None or (servers is not None and not isinstance(servers, dict)):
            skipped.append(f"{path}: not a JSON object with a '{key}' object")
            continue
        existing = (servers or {}).get(SERVER_NAME)
        if existing is not None and not is_ours(existing):
            skipped.append(f"{path}: an unrelated server is already named '{SERVER_NAME}'")
            continue
        wanted = server_entry(tool)
        placed.add(tool)
        if existing == wanted:
            continue
        new = copy.deepcopy(doc)
        if tool == "opencode" and not path.exists():
            new["$schema"] = OPENCODE_SCHEMA
        new.setdefault(key, {})[SERVER_NAME] = wanted
        _write(path, new)
    if "claude-code" in placed:
        skipped += _approve_for_claude(root)
    return skipped


def _approve_for_claude(root: Path) -> list[str]:
    path = root / CLAUDE_APPROVALS
    doc = _load(path)
    approved = doc.get("enabledMcpjsonServers", []) if doc is not None else None
    if doc is None or not isinstance(approved, list):
        return [f"{path}: not a JSON object with an 'enabledMcpjsonServers' list"]
    if SERVER_NAME not in approved:
        _write(path, {**doc, "enabledMcpjsonServers": [*approved, SERVER_NAME]})
    return []


def remove_project_mcp(workspace_root: Path, *, dry_run: bool = False) -> list[Path]:
    """Remove our server (and Claude's approval of it) from a project; returns the files changed."""
    root = Path(workspace_root)
    changed: list[Path] = []
    for tool, (rel, key) in _FILES.items():
        path = root / rel
        doc = _load(path) if path.exists() else None
        if not doc or not isinstance(doc.get(key), dict) or not is_ours(doc[key].get(SERVER_NAME)):
            continue
        new = copy.deepcopy(doc)
        del new[key][SERVER_NAME]
        if not new[key]:
            del new[key]
        changed.append(path)
        if not dry_run:
            _finish(path, new, {"$schema"} if tool == "opencode" else set())
    path = root / CLAUDE_APPROVALS
    doc = _load(path) if path.exists() else None
    approved = doc.get("enabledMcpjsonServers") if doc else None
    if doc and isinstance(approved, list) and SERVER_NAME in approved:
        new = {**doc, "enabledMcpjsonServers": [s for s in approved if s != SERVER_NAME]}
        if not new["enabledMcpjsonServers"]:
            del new["enabledMcpjsonServers"]
        changed.append(path)
        if not dry_run:
            _finish(path, new, set())
    return changed


def _finish(path: Path, doc: dict[str, Any], boilerplate: set[str]) -> None:
    """Write what is left, or delete a file that held nothing but our entry."""
    if set(doc) <= boilerplate:
        path.unlink()
    else:
        _write(path, doc)
