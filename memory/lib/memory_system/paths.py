from __future__ import annotations

import hashlib
import json
import shlex
import uuid
from pathlib import Path

from .system.config import memory_home

MEMORY_ID_FILENAME = "memory-id"
MARKER_DIRNAME = ".silly-memory"
GLOBAL_ID = "_global"

_GENERATED_RULES: dict[str, Path] = {
    "cursor": Path(".cursor") / "rules" / "_memory-context.mdc",
    "claude-code": Path(".claude") / "rules" / "_memory-context.md",
}


def expand(path: str | Path) -> Path:
    return Path(str(path).replace("~", str(Path.home()))).expanduser().resolve()


def global_store() -> Path:
    return memory_home() / GLOBAL_ID


def display_path(path: Path) -> str:
    """``path`` with the user's home directory abbreviated to ``~``."""
    path = Path(path)
    for home in (Path.home(), Path.home().resolve()):
        try:
            rel = path.relative_to(home)
        except ValueError:
            continue
        return "~/" + rel.as_posix() if rel.parts else "~"
    return str(path)


def cli_command() -> str:
    """Shell command that runs this installation's ``memory`` CLI."""
    cli = memory_home() / "bin" / "memory"
    shown = display_path(cli)
    # Why: quoting would disable tilde expansion, so a ~ path is used only when it needs no quoting.
    if shown.startswith("~/") and shlex.quote(shown[2:]) == shown[2:]:
        return f"python3 {shown}"
    return f"python3 {shlex.quote(str(cli))}"


def workspace_marker(workspace_root: Path) -> Path:
    """The authoritative project marker, ``<root>/.silly-memory/memory-id``."""
    return Path(workspace_root) / MARKER_DIRNAME / MEMORY_ID_FILENAME


def write_workspace_marker(workspace_root: Path, store_id: str) -> Path:
    """Write (or overwrite) the project marker."""
    root = Path(workspace_root)
    _append_ignore_entries(root / MARKER_DIRNAME, ("*",))
    marker = workspace_marker(root)
    _ = marker.write_text(store_id + "\n", encoding="utf-8")
    return marker


def resolve_workspace_id(workspace_root: Path) -> str:
    workspace_root = Path(workspace_root)
    try:
        value = workspace_marker(workspace_root).read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value:
        return value
    value = hashlib.sha256(str(workspace_root.resolve()).encode()).hexdigest()[:16]
    _ = write_workspace_marker(workspace_root, value)
    return value


def resolve_workspace_root(start: Path) -> Path:
    """Nearest ancestor with a ``.git`` entry, else with a memory marker, else ``start``.

    A linked worktree's ``.git`` file counts as a root, so a worktree keeps its
    own identity instead of collapsing into the main checkout. Filesystem checks
    only; this never spawns git.
    """
    start = Path(start).expanduser().resolve()
    chain = [start, *start.parents]
    for directory in chain:
        if (directory / ".git").exists():
            return directory
    for directory in chain:
        if workspace_marker(directory).is_file():
            return directory
    return start


def workspace_store(workspace_root: Path) -> Path:
    workspace_root = Path(workspace_root)
    ws_id = resolve_workspace_id(workspace_root)
    store = memory_home() / ws_id
    _ = store.mkdir(parents=True, exist_ok=True)
    meta = store / ".meta.json"
    if not meta.exists():
        _ = meta.write_text(
            json.dumps({"workspace_id": ws_id, "workspace_root": str(workspace_root.resolve())}, indent=2),
            encoding="utf-8",
        )
    return store


def ensure_layout(store: Path) -> None:
    _ = (store / "memory-bank").mkdir(parents=True, exist_ok=True)
    _ = (store / "staging").mkdir(parents=True, exist_ok=True)
    _ = (store / "queues").mkdir(parents=True, exist_ok=True)
    for name in ("events.jsonl", "observations.md", "work-state.md", "context-pack.md"):
        p = store / name
        if not p.exists():
            if name.endswith(".md"):
                _ = p.write_text(f"# {name.replace('.md', '').replace('-', ' ').title()}\n\n", encoding="utf-8")
            else:
                _ = p.touch()


def bank_path(store: Path, filename: str) -> Path:
    return store / "memory-bank" / filename


def generated_rule_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / _GENERATED_RULES["cursor"]


def generated_rule_paths(workspace_root: Path, tools: list[str] | tuple[str, ...]) -> dict[str, Path]:
    """Project rule file per selected tool that loads the pack from disk.

    OpenCode has no project rule; its plugin injects the stored pack instead.
    """
    root = Path(workspace_root)
    return {tool: root / _GENERATED_RULES[tool] for tool in tools if tool in _GENERATED_RULES}


_IGNORE_HEADER = "# silly-memory — generated, local-only artifacts"
_TOOL_IGNORES: dict[str, tuple[str, tuple[str, ...]]] = {
    "cursor": (".cursor", ("rules/_memory-context.mdc",)),
    "claude-code": (".claude", ("rules/_memory-context.md", "settings.local.json")),
}


def _append_ignore_entries(directory: Path, entries: tuple[str, ...]) -> None:
    _ = directory.mkdir(parents=True, exist_ok=True)
    gi = directory / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    have = {ln.strip() for ln in existing}
    missing = [e for e in entries if e not in have]
    if not missing:
        return
    header = [] if existing else [_IGNORE_HEADER]
    new_text = "\n".join(existing + header + missing).strip() + "\n"
    _ = gi.write_text(new_text, encoding="utf-8")


def ensure_local_gitignore(workspace_root: Path, tools: list[str] | tuple[str, ...] = ("cursor",)) -> None:
    """Keep memory-system artifacts out of git in any workspace.

    Writes self-contained ``.gitignore`` files inside each generated folder
    (patterns are relative to that folder, so the repo root .gitignore is
    untouched): ``.silly-memory/`` always, plus ``.cursor/`` or ``.claude/`` only
    for the selected tools. Idempotent; appends only missing entries.
    """
    root = Path(workspace_root)
    _append_ignore_entries(root / MARKER_DIRNAME, ("*",))
    for tool in tools:
        spec = _TOOL_IGNORES.get(tool)
        if spec:
            _append_ignore_entries(root / spec[0], spec[1])


def lock_path(store: Path) -> Path:
    return store / ".lock"


def new_event_id() -> str:
    return uuid.uuid4().hex
