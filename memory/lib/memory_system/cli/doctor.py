"""Health check command — runs read-only probes and reports a verdict.

Read this first if you are new to the codebase:
  - Checks run in fixed order: home_writable, sqlite_integrity,
    version_marker, embedding_weights, privacy_invariant, tools, mcp,
    recall_p95, stale_locks, backup_retention. Each must return
    ``(level, detail)``.
  - ``tools`` checks the wiring of every tool listed under ``tools`` in
    ``config.json`` (Cursor and Claude Code hooks, the OpenCode plugin) and
    reports when each last started a session. An unwired listed tool, or an
    empty list, is an error. ``mcp`` reports the server off (the default) or,
    when on, checks the stdio server launcher and counts the projects whose own
    config registers it. Neither runs ``claude`` or ``opencode``.
  - Each check is wrapped in ``_run`` so a bug or exception inside ONE
    check produces a single ``error`` line — the rest of the report still
    runs. Never let a check leak unhandled exceptions to the caller.
  - Strict invariants: no writes to disk, no network calls, no blocking on
    user input. The recall p95 probe uses a temp directory with
    ``SILLY_MEMORY_HOME`` overridden so it never touches the user's real
    store. Exit codes are ``0`` healthy, ``1`` warn, ``2`` error.

Public interface (imported elsewhere): ``CHECKS``, ``VERSION``,
    ``run_doctor``.
Depends on: system.config, system.version, backends.embedding.weights_manifest
    (lazy), index, paths (both lazy inside ``recall_p95``),
    recall.sessions (lazy inside ``tools``).
Used by: ``bin/memory doctor`` and status.doctor_cache.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from memory_system.system.config import (
    HOME_ENV,
    MEMORY_ALLOW_NETWORK_ENV,
    load_config,
    memory_allow_network,
    memory_home,
)
from memory_system.system.version import CURRENT_VERSION as VERSION

_LEVEL_INFO = "info"
_LEVEL_WARN = "warn"
_LEVEL_ERROR = "error"

_GLYPH = {_LEVEL_INFO: "✅", _LEVEL_WARN: "⚠️ ", _LEVEL_ERROR: "❌"}

_HOOK_EVENTS = (
    "sessionStart",
    "beforeSubmitPrompt",
    "afterAgentResponse",
    "afterFileEdit",
    "afterShellExecution",
    "preCompact",
    "sessionEnd",
    "stop",
)
_CLAUDE_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact", "SessionEnd")
_CURSOR_SHIM = "memory-hook.sh"
_CLAUDE_SHIM = "silly-memory-hook.sh"
_KNOWN_TOOLS = ("cursor", "claude-code", "opencode")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _run(name: str, fn: Callable[[], tuple[str, str]]) -> dict:
    """Execute one check function; catch all exceptions so a bug in one
    check cannot break the rest of the doctor.
    """
    start = time.perf_counter()
    try:
        level, detail = fn()
    except Exception as exc:
        level, detail = _LEVEL_ERROR, f"check raised {type(exc).__name__}: {exc}"
    return {"name": name, "level": level, "ms": _ms_since(start), "detail": detail}


# --- The checks ------------------------------------------------------------


def _check_home_writable() -> tuple[str, str]:
    home = memory_home()
    if not home.exists():
        return _LEVEL_ERROR, f"memory home missing: {home}"
    try:
        mode = home.stat().st_mode & 0o777
    except OSError as exc:
        return _LEVEL_ERROR, f"stat {home} failed: {exc}"
    # Probe write without leaving an artifact behind: use os.access only.
    if not os.access(home, os.W_OK):
        return _LEVEL_ERROR, f"memory home not writable: {home}"
    if mode != 0o700:
        return _LEVEL_WARN, f"memory home perms {oct(mode)} (expected 0o700) at {home}"
    return _LEVEL_INFO, f"memory home ok ({home}, perms 0o700)"


def _check_sqlite_integrity() -> tuple[str, str]:
    home = memory_home()
    if not home.exists():
        return _LEVEL_WARN, f"memory home missing, no DBs to check: {home}"
    dbs = sorted(home.glob("*/memory.sqlite"))
    if not dbs:
        return _LEVEL_INFO, "no workspace DBs yet (nothing to verify)"
    bad: list[str] = []
    for db in dbs:
        try:
            conn = sqlite3.connect(db)
            try:
                cur = conn.execute("PRAGMA integrity_check")
                row = cur.fetchone()
                result = (row[0] if row else "") if row is not None else ""
                if result != "ok":
                    bad.append(f"{db.parent.name}: {result}")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            bad.append(f"{db.parent.name}: {exc}")
    if bad:
        return _LEVEL_ERROR, "integrity_check failed: " + "; ".join(bad)
    return _LEVEL_INFO, f"{len(dbs)} DB(s) integrity_check ok"


def _check_version_marker() -> tuple[str, str]:
    marker = memory_home() / "VERSION"
    if not marker.exists():
        return _LEVEL_WARN, "VERSION marker not present yet"
    try:
        installed = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return _LEVEL_WARN, f"VERSION marker unreadable: {exc}"
    if not installed:
        return _LEVEL_WARN, "VERSION marker is empty"
    if installed != VERSION:
        return (
            _LEVEL_WARN,
            f"VERSION marker {installed!r} differs from code {VERSION!r}",
        )
    return _LEVEL_INFO, f"VERSION matches ({installed})"


def _check_embedding_weights() -> tuple[str, str]:
    try:
        from memory_system.backends.embedding.weights_manifest import verify_weights
    except ImportError as exc:
        return _LEVEL_WARN, f"weight manifest not available yet ({exc})"
    cache_dir = memory_home() / "_embeddings"
    model_name = "sentence-transformers/all-MiniLM-L6-v2"
    try:
        verify_weights(model_name, cache_dir if cache_dir.exists() else None)
    except RuntimeError as exc:
        return _LEVEL_WARN, f"weight verify failed: {exc}"
    except Exception as exc:
        return _LEVEL_WARN, f"weight verify error: {type(exc).__name__}: {exc}"
    return _LEVEL_INFO, "embedding weights verified"


def _check_privacy_invariant() -> tuple[str, str]:
    value = memory_allow_network().strip()
    if value == "0":
        return (
            _LEVEL_INFO,
            f"{MEMORY_ALLOW_NETWORK_ENV}=0 (offline by default — gating active)",
        )
    return (
        _LEVEL_INFO,
        f"{MEMORY_ALLOW_NETWORK_ENV}={value!r} (network gating bypassed)",
    )


def _opencode_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "opencode"


def _read_json_object(path: Path) -> tuple[dict | None, str | None]:
    """``(object, None)``, or ``(None, problem)`` when missing or not a JSON object."""
    if not path.exists():
        return None, f"{path} missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path} invalid: {exc}"
    if not isinstance(data, dict):
        return None, f"{path} is not a JSON object"
    return data, None


def _configured_tools() -> list[str] | None:
    tools = load_config().get("tools")
    if not isinstance(tools, list):
        return None
    return [str(tool) for tool in tools]


def _cursor_wiring() -> str | None:
    cursor = Path.home() / ".cursor"
    data, problem = _read_json_object(cursor / "hooks.json")
    if problem:
        return f"hooks.json: {problem}"
    hooks = data.get("hooks") if data else None
    if not isinstance(hooks, dict):
        return f"{cursor / 'hooks.json'} has no 'hooks' object"
    def wired(entries: object) -> bool:
        return isinstance(entries, list) and any(
            isinstance(entry, dict) and str(entry.get("command", "")).endswith(_CURSOR_SHIM) for entry in entries
        )

    missing = [event for event in _HOOK_EVENTS if not wired(hooks.get(event))]
    if missing:
        return f"hook events not pointing at {_CURSOR_SHIM}: {', '.join(missing)}"
    if not (cursor / "hooks" / _CURSOR_SHIM).is_file():
        return f"hook shim missing: {cursor / 'hooks' / _CURSOR_SHIM}"
    return None


def _claude_wiring() -> str | None:
    claude = Path.home() / ".claude"
    data, problem = _read_json_object(claude / "settings.json")
    if problem:
        return f"settings: {problem}"
    hooks = data.get("hooks") if data else None
    if not isinstance(hooks, dict):
        return f"{claude / 'settings.json'} has no 'hooks' object"

    def wired(event: str) -> bool:
        groups = hooks.get(event)
        return isinstance(groups, list) and any(
            isinstance(handler, dict) and str(handler.get("command", "")).endswith(_CLAUDE_SHIM)
            for group in groups
            if isinstance(group, dict) and isinstance(group.get("hooks"), list)
            for handler in group["hooks"]
        )

    missing = [event for event in _CLAUDE_HOOK_EVENTS if not wired(event)]
    if missing:
        return f"hook events not pointing at {_CLAUDE_SHIM}: {', '.join(missing)}"
    if not (claude / "hooks" / _CLAUDE_SHIM).is_file():
        return f"hook shim missing: {claude / 'hooks' / _CLAUDE_SHIM}"
    return None


def _opencode_wiring() -> str | None:
    plugin = _opencode_config_dir() / "plugins" / "silly-memory.js"
    return None if plugin.is_file() else f"plugin missing: {plugin}"


_WIRING: dict[str, Callable[[], str | None]] = {
    "cursor": _cursor_wiring,
    "claude-code": _claude_wiring,
    "opencode": _opencode_wiring,
}


def _last_session_starts() -> dict[str, str]:
    """Latest recorded session start per tool, from every store's timeline."""
    from memory_system.recall.sessions import SESSIONS_FILE

    latest: dict[str, str] = {}
    for timeline in memory_home().glob(f"*/{SESSIONS_FILE}"):
        try:
            lines = timeline.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or not entry.get("started"):
                continue
            source, started = str(entry.get("source")), str(entry["started"])
            if started > latest.get(source, ""):
                latest[source] = started
    return latest


def _check_tools() -> tuple[str, str]:
    tools = _configured_tools()
    if not tools:
        return _LEVEL_ERROR, "no tools listed under 'tools' in config.json; rerun install.sh"
    problems: list[str] = []
    wired: list[str] = []
    last = _last_session_starts()
    for tool in tools:
        check = _WIRING.get(tool)
        problem = check() if check else f"unknown tool (expected one of {', '.join(_KNOWN_TOOLS)})"
        if problem:
            problems.append(f"{tool}: {problem}")
        else:
            wired.append(f"{tool} wired (last session start: {last.get(tool, 'none recorded')[:19]})")
    detail = "; ".join(problems + wired)
    return (_LEVEL_ERROR if problems else _LEVEL_INFO), detail


def _check_mcp() -> tuple[str, str]:
    # The server is registered in each project's own config at its session start.
    from memory_system.project_mcp import mcp_enabled, project_roots, registered_tools

    roots = project_roots(memory_home())
    if not mcp_enabled():
        detail = "off; the add-memory and query-memory skills cover recall, tasks, and add (./install.sh --mcp turns it on)"
        still = sum(1 for root in roots if registered_tools(root))
        if still:
            detail += f"; {still} project(s) still list the server (./install.sh --no-mcp removes it)"
        return _LEVEL_INFO, detail
    launcher = memory_home() / "bin" / "memory-mcp"
    if not launcher.is_file():
        return _LEVEL_ERROR, f"MCP server launcher missing: {launcher}"
    tools = [t for t in _configured_tools() or [] if t in ("cursor", "claude-code", "opencode")]
    counts = {tool: 0 for tool in tools}
    for root in roots:
        for tool in registered_tools(root) & set(tools):
            counts[tool] += 1
    per_tool = ", ".join(f"{tool} {n}/{len(roots)}" for tool, n in counts.items())
    detail = f"launcher {launcher}; registered in project configs: {per_tool or 'no tools wired'}"
    return _LEVEL_INFO, detail + " (added at each project's session start)"


def _check_recall_p95() -> tuple[str, str]:
    """Synthetic 10-record corpus, FTS5 rebuild + recall, p95 < 50ms."""
    # Import locally so module load stays cheap when the check is bypassed.
    with TemporaryDirectory(prefix="memdoctor-recall-") as tmp:
        # Isolate the entire run from the user's real store by pointing
        # memory_home() at our temp directory via SILLY_MEMORY_HOME.
        original = {HOME_ENV: os.environ.get(HOME_ENV)}
        os.environ[HOME_ENV] = tmp
        try:
            from memory_system.index import rebuild_index
            from memory_system.paths import ensure_layout, workspace_store

            ws_root = Path(tmp) / "ws"
            ws_root.mkdir(parents=True, exist_ok=True)
            store = workspace_store(ws_root)
            ensure_layout(store)
            # Seed 10 deterministic observation bullets.
            obs = store / "observations.md"
            lines = ["# Observations", ""]
            for i in range(10):
                lines.append(f"- record {i}: alpha beta gamma delta target{i}")
            obs.write_text("\n".join(lines) + "\n", encoding="utf-8")
            rebuild_index(store, store.name)

            from memory_system.index import recall_all

            # Warm once so JIT/connection setup doesn't skew p95.
            recall_all(ws_root, "target0", limit=5)

            samples: list[int] = []
            for i in range(20):
                t0 = time.perf_counter()
                _ = recall_all(ws_root, f"target{i % 10}", limit=5)
                samples.append(int((time.perf_counter() - t0) * 1_000_000))
            samples.sort()
            p95_us = samples[int(0.95 * (len(samples) - 1))]
            p95_ms = p95_us / 1000.0
        finally:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    if p95_ms >= 50.0:
        return _LEVEL_WARN, f"recall p95 = {p95_ms:.1f}ms (budget 50ms)"
    return _LEVEL_INFO, f"recall p95 = {p95_ms:.1f}ms (10-record corpus)"


def _check_no_stale_locks() -> tuple[str, str]:
    home = memory_home()
    candidates = [home / ".install.lock", home / ".upgrade.lock"]
    stale: list[str] = []
    for lock in candidates:
        if not lock.exists():
            continue
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError as exc:
            stale.append(f"{lock.name}: stat failed ({exc})")
            continue
        # Freshly-held = under 5 minutes old; older is considered stale.
        if age > 300:
            stale.append(f"{lock.name}: {int(age)}s old")
    if stale:
        return _LEVEL_WARN, "stale lockfile(s): " + "; ".join(stale)
    return _LEVEL_INFO, "no stale lockfiles"


def _check_backup_retention() -> tuple[str, str]:
    # Backups sit next to the home as `<home name>.upgrade-backup-*`.
    home = memory_home()
    count = len(list(home.parent.glob(f"{home.name}.upgrade-backup-*"))) if home.parent.exists() else 0
    if count > 5:
        return _LEVEL_WARN, f"{count} upgrade backups (retention limit: 5)"
    return _LEVEL_INFO, f"{count} upgrade backup(s) on disk"


# --- Orchestration -------------------------------------------------------


CHECKS: tuple[tuple[str, Callable[[], tuple[str, str]]], ...] = (
    ("home_writable", _check_home_writable),
    ("sqlite_integrity", _check_sqlite_integrity),
    ("version_marker", _check_version_marker),
    ("embedding_weights", _check_embedding_weights),
    ("privacy_invariant", _check_privacy_invariant),
    ("tools", _check_tools),
    ("mcp", _check_mcp),
    ("recall_p95", _check_recall_p95),
    ("stale_locks", _check_no_stale_locks),
    ("backup_retention", _check_backup_retention),
)


def _overall_status(results: list[dict]) -> str:
    levels = {r["level"] for r in results}
    if _LEVEL_ERROR in levels:
        return "error"
    if _LEVEL_WARN in levels:
        return "warn"
    return "healthy"


def _exit_code(status: str) -> int:
    return {"healthy": 0, "warn": 1, "error": 2}[status]


def _render_text(results: list[dict], status: str) -> str:
    lines = ["silly-memory — doctor", "=" * 48]
    for r in results:
        glyph = _GLYPH.get(r["level"], "?")
        lines.append(
            f"  {glyph} {r['name']:<20} ({r['ms']:>4}ms) {r['detail']}"
        )
    lines.append("")
    lines.append(f"verdict: {status}")
    return "\n".join(lines) + "\n"


def run_doctor(json_output: bool) -> int:
    results = [_run(name, fn) for name, fn in CHECKS]
    status = _overall_status(results)
    if json_output:
        payload = {
            "status": status,
            "checks": results,
            "version": VERSION,
            "ts": _now_iso(),
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(_render_text(results, status))
    return _exit_code(status)


__all__ = ["CHECKS", "VERSION", "run_doctor"]
