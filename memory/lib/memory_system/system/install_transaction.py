"""Installer-owned artifacts, the upgrade transaction journal, and the upgrade lock.

Read this first if you are new to the codebase:
  - Owned artifacts are files the installer puts outside the memory home (hook
    shims, recall rules, skills, the OpenCode plugin) and fragments it merges
    into shared files: the memory hook entries in ``~/.cursor/hooks.json`` and
    ``~/.claude/settings.json``, and the managed block in ``~/.zshrc``. Only the
    owned part of a shared file is ever changed. The MCP server is registered
    per project (see ``memory_system.project_mcp``), never here.
  - An existing recall rule or skill is replaced only when it is byte-identical
    to what this installer last wrote there (its digest in the ownership
    record). A file you changed, or one that was never ours, is left alone and
    reported.
  - Journal: when ``SILLY_MEMORY_UPGRADE_JOURNAL`` names a file, every write
    first appends an fsynced "intended" record with the before- and
    after-image of the owned part, then an "applied" record once it finished.
    ``revert-journal`` restores the before-images: an owned part already equal
    to its before- or after-image is fine, any third value is a conflict, and
    every entry is checked before anything is written.
  - ``<home>/.installed-artifacts.json`` records the files this installer wrote
    and their digests, so uninstall removes only unmodified ones.
  - ``run-locked`` holds an advisory lock on the upgrade lock file while its
    child runs; the child inherits the descriptor, so the kernel releases the
    lock when the last holder exits, even after a crash. The file is never
    deleted.
  - Malformed shared configuration fails before anything is changed.

Public interface (imported elsewhere): the CLI subcommands below and the
    functions they call (``put_file``, ``install_text_artifact``,
    ``merge_json_fragment``, ``apply_zsh_block``, ``revert_entries``,
    ``run_locked``, ``Journal``).
Depends on: stdlib only, so the installer can run it from the source tree.
Used by: install.sh, install-zsh.sh, upgrade.sh.
"""
from __future__ import annotations

import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

JOURNAL_ENV = "SILLY_MEMORY_UPGRADE_JOURNAL"
LOCK_FD_ENV = "SILLY_MEMORY_UPGRADE_LOCK_FD"
OWNERSHIP_FILE = ".installed-artifacts.json"
CURSOR_SHIM = "memory-hook.sh"
CLAUDE_SHIM = "silly-memory-hook.sh"

ZSH_OPEN = "# >>> silly-memory >>>"
ZSH_CLOSE = "# <<< silly-memory <<<"

EXIT_INVALID = 3
EXIT_CONFLICT = 4
EXIT_ESCAPE = 65
EXIT_LOCKED = 73

class InstallError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


# --- small file helpers ---------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _file_mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def check_within(root: Path, target: Path) -> None:
    """Refuse a target that resolves (through symlinks) outside ``root``."""
    real_root = os.path.realpath(root)
    real_target = os.path.realpath(target)
    if real_target != real_root and not real_target.startswith(real_root.rstrip("/") + "/"):
        raise InstallError(f"{target} escapes {root}; not writing through that link", EXIT_ESCAPE)


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Replace ``path`` atomically, keeping its mode unless ``mode`` is given.

    A symlinked file (a dotfiles checkout) is written through, keeping the link.
    """
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    final_mode = mode if mode is not None else (_file_mode(path) or 0o644)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, final_mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- journal --------------------------------------------------------------------


class Journal:
    """Append-only JSON-lines record of intended and applied writes (or a no-op)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    @classmethod
    def from_env(cls) -> "Journal":
        value = os.environ.get(JOURNAL_ENV)
        return cls(Path(value) if value else None)

    def _append(self, record: dict[str, Any]) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def intend(self, entry: dict[str, Any]) -> int | None:
        if self.path is None:
            return None
        entry_id = sum(1 for record in _read_records(self.path) if record.get("phase") == "intended") + 1
        self._append({"id": entry_id, "phase": "intended", **entry})
        return entry_id

    def applied(self, entry_id: int | None) -> None:
        if self.path is not None and entry_id is not None:
            self._append({"id": entry_id, "phase": "applied"})


def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a crash can leave a torn final line
        if isinstance(record, dict):
            records.append(record)
    return records


def load_journal(path: Path) -> list[dict[str, Any]]:
    """Intended entries in order, each with ``applied`` set from its applied record."""
    entries: dict[int, dict[str, Any]] = {}
    for record in _read_records(path):
        entry_id = record.get("id")
        if not isinstance(entry_id, int):
            continue
        if record.get("phase") == "intended":
            entries[entry_id] = {**record, "applied": False}
        elif record.get("phase") == "applied" and entry_id in entries:
            entries[entry_id]["applied"] = True
    return [entries[key] for key in sorted(entries)]


# --- owned files ----------------------------------------------------------------


def _file_image(data: bytes | None, mode: int | None, *, with_content: bool) -> dict[str, Any]:
    image: dict[str, Any] = {"exists": data is not None, "sha256": sha256_bytes(data) if data is not None else None}
    if data is not None:
        image["mode"] = mode
        if with_content:
            image["content_b64"] = base64.b64encode(data).decode("ascii")
    return image


def put_file(target: Path, data: bytes, journal: Journal, mode: int | None = None) -> str:
    """Write an owned file atomically after journaling it; returns what happened."""
    before = read_bytes(target)
    before_mode = _file_mode(target)
    wanted_mode = mode if mode is not None else (before_mode or 0o644)
    if before == data and before_mode == wanted_mode:
        return "unchanged"
    entry_id = journal.intend(
        {
            "op": "file",
            "target": str(target),
            "before": _file_image(before, before_mode, with_content=True),
            "after": _file_image(data, wanted_mode, with_content=False),
        }
    )
    atomic_write_bytes(target, data, wanted_mode)
    journal.applied(entry_id)
    return "created" if before is None else "updated"


def install_text_artifact(
    target: Path,
    shipped: bytes,
    journal: Journal,
    owned_shipped_digests: frozenset[str] = frozenset(),
) -> str:
    """Install or update an owned recall rule or skill file; returns what happened.

    ``kept`` means the file differs from what this installer last wrote there
    (you changed it, or it was never ours), so it is left untouched.
    """
    current = read_bytes(target)
    if current is None:
        put_file(target, shipped, journal)
        return "installed"
    if current == shipped:
        return "unchanged"
    if sha256_bytes(current) in owned_shipped_digests:
        put_file(target, shipped, journal)
        return "updated"
    return "kept"


# --- ownership record -------------------------------------------------------------


def load_ownership(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    if not isinstance(data.get("files"), dict):
        data["files"] = {}
    if not isinstance(data.get("fragments"), dict):
        data["fragments"] = {}
    return data


def record_file(path: Path, target: Path, content: bytes, kind: str, tool: str) -> None:
    data = load_ownership(path)
    data["files"][str(target)] = {"sha256": sha256_bytes(content), "kind": kind, "tool": tool}
    atomic_write_bytes(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o600)


def record_fragment(path: Path, target: Path, kind: str) -> None:
    data = load_ownership(path)
    kinds = data["fragments"].setdefault(str(target), [])
    if kind not in kinds:
        kinds.append(kind)
    atomic_write_bytes(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"), 0o600)


def owned_shipped_digests(path: Path | None, target: Path) -> frozenset[str]:
    if path is None:
        return frozenset()
    entry = load_ownership(path)["files"].get(str(target))
    if isinstance(entry, dict) and isinstance(entry.get("sha256"), str):
        return frozenset({entry["sha256"]})
    return frozenset()


# --- JSON fragments ---------------------------------------------------------------


def _command_is(command: object, shim: str) -> bool:
    if not isinstance(command, str):
        return False
    command = command.strip()
    return command == shim or command.endswith("/" + shim)


class JsonFragment:
    """The owned part of one shared JSON file: extract, remove, and insert it."""

    def extract(self, doc: dict[str, Any]) -> Any:
        raise NotImplementedError

    def remove(self, doc: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def insert(self, doc: dict[str, Any], fragment: Any) -> dict[str, Any]:
        raise NotImplementedError

    def is_residue(self, doc: dict[str, Any]) -> bool:
        """True when a document holds nothing but empty structure (safe to delete)."""
        return doc == {}


class CursorHooks(JsonFragment):
    @staticmethod
    def _owned(entry: object) -> bool:
        return isinstance(entry, dict) and _command_is(entry.get("command"), CURSOR_SHIM)

    def extract(self, doc: dict[str, Any]) -> dict[str, list[Any]]:
        hooks = doc.get("hooks")
        if not isinstance(hooks, dict):
            return {}
        out: dict[str, list[Any]] = {}
        for event, entries in hooks.items():
            owned = [e for e in entries if self._owned(e)] if isinstance(entries, list) else []
            if owned:
                out[event] = owned
        return out

    def remove(self, doc: dict[str, Any]) -> dict[str, Any]:
        hooks = doc.get("hooks")
        if isinstance(hooks, dict):
            for event in list(hooks):
                entries = hooks[event]
                if isinstance(entries, list) and any(self._owned(e) for e in entries):
                    kept = [e for e in entries if not self._owned(e)]
                    if kept:
                        hooks[event] = kept
                    else:
                        del hooks[event]
        return doc

    def insert(self, doc: dict[str, Any], fragment: dict[str, list[Any]]) -> dict[str, Any]:
        if not fragment:
            return doc
        doc.setdefault("version", 1)
        hooks = doc.setdefault("hooks", {})
        for event, entries in fragment.items():
            hooks.setdefault(event, []).extend(copy.deepcopy(entries))
        return doc

    def is_residue(self, doc: dict[str, Any]) -> bool:
        return set(doc) <= {"version", "hooks"} and not any(doc.get("hooks") or {})


class ClaudeHooks(JsonFragment):
    """Hook handlers under the ``hooks`` key of ``~/.claude/settings.json`` only."""

    @staticmethod
    def _owned(handler: object) -> bool:
        return isinstance(handler, dict) and _command_is(handler.get("command"), CLAUDE_SHIM)

    def extract(self, doc: dict[str, Any]) -> dict[str, list[Any]]:
        hooks = doc.get("hooks")
        if not isinstance(hooks, dict):
            return {}
        out: dict[str, list[Any]] = {}
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    continue
                owned = [h for h in group["hooks"] if self._owned(h)]
                if owned:
                    out.setdefault(event, []).append({**group, "hooks": owned})
        return out

    def remove(self, doc: dict[str, Any]) -> dict[str, Any]:
        hooks = doc.get("hooks")
        if not isinstance(hooks, dict):
            return doc
        touched = False
        for event in list(hooks):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            kept_groups = []
            event_touched = False
            for group in groups:
                if isinstance(group, dict) and isinstance(group.get("hooks"), list) and any(self._owned(h) for h in group["hooks"]):
                    event_touched = True
                    rest = [h for h in group["hooks"] if not self._owned(h)]
                    if rest:
                        kept_groups.append({**group, "hooks": rest})
                else:
                    kept_groups.append(group)
            if event_touched:
                touched = True
                if kept_groups:
                    hooks[event] = kept_groups
                else:
                    del hooks[event]
        if touched and not hooks:
            del doc["hooks"]
        return doc

    def insert(self, doc: dict[str, Any], fragment: dict[str, list[Any]]) -> dict[str, Any]:
        if not fragment:
            return doc
        hooks = doc.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise InstallError("the 'hooks' value in Claude Code settings is not an object", EXIT_INVALID)
        for event, groups in fragment.items():
            hooks.setdefault(event, []).extend(copy.deepcopy(groups))
        return doc


FRAGMENTS: dict[str, JsonFragment] = {"cursor-hooks": CursorHooks(), "claude-hooks": ClaudeHooks()}


def load_json_object(path: Path) -> tuple[dict[str, Any], bool]:
    """``(document, existed)``; raises InstallError when present but not a JSON object."""
    data = read_bytes(path)
    if data is None:
        return {}, False
    if not data.strip():
        return {}, True
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"{path} is not valid JSON ({exc}); fix or move it, then rerun", EXIT_INVALID)
    if not isinstance(doc, dict):
        raise InstallError(f"{path} does not hold a JSON object; fix or move it, then rerun", EXIT_INVALID)
    return doc, True


def _dump_json(doc: dict[str, Any]) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def validate_json(kind: str, path: Path) -> None:
    doc, _ = load_json_object(path)
    if kind in ("cursor-hooks", "claude-hooks") and "hooks" in doc and not isinstance(doc["hooks"], dict):
        raise InstallError(f"{path}: 'hooks' is not an object", EXIT_INVALID)


def merge_json_fragment(kind: str, target: Path, fragment: Any, journal: Journal, new_file_mode: int = 0o644) -> str:
    """Replace the owned fragment of ``target`` with ``fragment``; returns what happened."""
    handler = FRAGMENTS[kind]
    validate_json(kind, target)
    doc, existed = load_json_object(target)
    before = handler.extract(doc)
    if existed and before == handler.extract(handler.insert({}, fragment)):
        return "unchanged"  # already current: rewriting would only reorder the file
    new_doc = handler.insert(handler.remove(copy.deepcopy(doc)), fragment)
    if existed and new_doc == doc:
        return "unchanged"
    entry_id = journal.intend(
        {
            "op": "fragment",
            "kind": kind,
            "target": str(target),
            "before": {"file_existed": existed, "fragment": before},
            "after": {"fragment": handler.extract(new_doc)},
        }
    )
    atomic_write_bytes(target, _dump_json(new_doc), None if existed else new_file_mode)
    journal.applied(entry_id)
    return "updated" if existed else "created"


# --- zsh managed block --------------------------------------------------------------


def _split_zsh(text: str) -> tuple[list[str], list[str]]:
    """``(kept_lines, owned_blocks)``; raises InstallError for an unclosed block."""
    kept: list[str] = []
    blocks: list[str] = []
    lines = text.split("\n")
    i = 0
    pending_blank = 0
    after_block = False
    while i < len(lines):
        line = lines[i]
        if line == ZSH_OPEN:
            try:
                end = lines.index(ZSH_CLOSE, i + 1)
            except ValueError:
                raise InstallError(f"unclosed silly-memory block starting with {line!r}; fix the rc file, then rerun", EXIT_INVALID)
            blocks.append("\n".join(lines[i : end + 1]))
            after_block = True
            i = end + 1
            continue
        if line == ZSH_CLOSE:
            raise InstallError(f"stray {line!r} without its opening line; fix the rc file, then rerun", EXIT_INVALID)
        if line == "":
            # Blank lines on both sides of a removed block collapse into the ones before it.
            if not (after_block and (pending_blank or not kept)):
                pending_blank += 1
        else:
            kept.extend([""] * pending_blank)
            pending_blank = 0
            kept.append(line)
            after_block = False
        i += 1
    kept.extend([""] * pending_blank)
    return kept, blocks


def zsh_owned_blocks(text: str) -> list[str]:
    return _split_zsh(text)[1]


def zsh_without_blocks(text: str) -> str:
    return "\n".join(_split_zsh(text)[0])


def zsh_with_blocks(text: str, blocks: list[str]) -> str:
    base = zsh_without_blocks(text).rstrip("\n")
    if not blocks:
        return base + "\n" if base else ""
    joined = "\n\n".join(blocks)
    return (f"{base}\n\n{joined}" if base else joined) + "\n"


def _zsh_quoted_path(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    if text.startswith(home + "/"):
        return "$HOME/" + re.sub(r'(["$`\\])', r"\\\1", text[len(home) + 1 :])
    return re.sub(r'(["$`\\])', r"\\\1", text)


def zsh_block(memory_zsh: Path) -> str:
    shown = _zsh_quoted_path(memory_zsh)
    lines = [ZSH_OPEN]
    if memory_zsh.parent != Path.home() / ".silly-memory":
        # A custom home: the helpers and tools started from this shell must find it.
        lines.append(f'export SILLY_MEMORY_HOME="{_zsh_quoted_path(memory_zsh.parent)}"')
    lines += [f'[ -f "{shown}" ] && source "{shown}"', ZSH_CLOSE]
    return "\n".join(lines)


def apply_zsh_block(rc: Path, memory_zsh: Path | None, journal: Journal, *, remove: bool = False, dry_run: bool = False) -> str:
    """Write (or remove) the one managed block; it goes last in the file."""
    data = read_bytes(rc)
    text = data.decode("utf-8") if data is not None else ""
    before = zsh_owned_blocks(text)
    if remove:
        if not before:
            return "unchanged"
        new_text = zsh_with_blocks(text, [])
    else:
        assert memory_zsh is not None
        block = zsh_block(memory_zsh)
        if before == [block]:
            return "unchanged"
        new_text = zsh_with_blocks(text, [block])
    if dry_run:
        return "would-change"
    entry_id = journal.intend(
        {
            "op": "fragment",
            "kind": "zsh",
            "target": str(rc),
            "before": {"file_existed": data is not None, "fragment": before},
            "after": {"fragment": zsh_owned_blocks(new_text)},
        }
    )
    atomic_write_bytes(rc, new_text.encode("utf-8"), None if data is not None else 0o644)
    journal.applied(entry_id)
    return "removed" if remove else ("updated" if before else "added")


# --- revert -----------------------------------------------------------------------


def _owned_value(entry: dict[str, Any]) -> Any:
    """The current value of the part of the entry's target that the installer owns."""
    target = Path(entry["target"])
    if entry["op"] == "file":
        current = read_bytes(target)
        return sha256_bytes(current) if current is not None else None
    if entry["kind"] == "zsh":
        data = read_bytes(target)
        return zsh_owned_blocks(data.decode("utf-8") if data is not None else "")
    try:
        doc, _ = load_json_object(target)
    except InstallError as exc:
        raise InstallError(f"cannot revert {target}: {exc}", EXIT_CONFLICT)
    return FRAGMENTS[entry["kind"]].extract(doc)


def _images(entry: dict[str, Any]) -> tuple[Any, Any]:
    if entry["op"] == "file":
        return entry["before"].get("sha256"), entry["after"].get("sha256")
    return entry["before"]["fragment"], entry["after"]["fragment"]


def _restore(entry: dict[str, Any]) -> None:
    """Put the entry's before-image back, leaving everything it does not own as it is."""
    target = Path(entry["target"])
    before = entry["before"]
    if entry["op"] == "file":
        if not before.get("exists"):
            target.unlink(missing_ok=True)
        else:
            atomic_write_bytes(target, base64.b64decode(before["content_b64"]), before.get("mode"))
        return
    if entry["kind"] == "zsh":
        data = read_bytes(target)
        restored = zsh_with_blocks(data.decode("utf-8") if data is not None else "", list(before["fragment"]))
        if not before.get("file_existed") and not restored.strip():
            target.unlink(missing_ok=True)
        else:
            atomic_write_bytes(target, restored.encode("utf-8"))
        return
    handler = FRAGMENTS[entry["kind"]]
    doc, _ = load_json_object(target)
    stripped = handler.remove(copy.deepcopy(doc))
    if not before.get("file_existed") and not before["fragment"] and handler.is_residue(stripped):
        target.unlink(missing_ok=True)
    else:
        atomic_write_bytes(target, _dump_json(handler.insert(stripped, before["fragment"])))


def revert_entries(entries: list[dict[str, Any]], *, dry_run: bool = False) -> list[str]:
    """Restore every entry's before-image, newest first; checks all entries before writing.

    An owned part equal to its before-image needs nothing; equal to its
    after-image it is restored; any third value is a conflict. A target written
    twice is checked against the state the later entry's revert leaves.
    """
    simulated: dict[tuple[str, str, str], Any] = {}
    changes: list[dict[str, Any]] = []
    problems: list[str] = []
    for entry in reversed(entries):
        key = (entry["op"], entry.get("kind", ""), entry["target"])
        before, after = _images(entry)
        try:
            current = simulated[key] if key in simulated else _owned_value(entry)
        except InstallError as exc:
            problems.append(str(exc))
            continue
        if current == after and current != before:
            changes.append(entry)
        elif current != before:
            what = "file" if entry["op"] == "file" else "silly-memory part"
            problems.append(f"{entry['target']}: the {what} changed since the upgrade wrote it; not restoring over it")
        simulated[key] = before
    if problems:
        raise InstallError("; ".join(problems), EXIT_CONFLICT)
    if not dry_run:
        for entry in changes:
            _restore(entry)
    return list(dict.fromkeys(entry["target"] for entry in changes))


# --- upgrade lock -------------------------------------------------------------------


def acquire_lock(path: Path, timeout: float) -> int:
    """Open and lock ``path`` (a regular, owner-only file); returns the descriptor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise InstallError(f"{path} is not a regular file owned by you", EXIT_INVALID)
    os.fchmod(fd, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise InstallError(f"another upgrade or rollback holds {path}", EXIT_LOCKED)
            time.sleep(0.1)


def run_locked(path: Path, timeout: float, command: list[str]) -> int:
    """Run ``command`` while holding the lock; the child inherits the descriptor."""
    fd = acquire_lock(path, timeout)
    os.set_inheritable(fd, True)
    proc = subprocess.Popen(command, pass_fds=(fd,), env={**os.environ, LOCK_FD_ENV: str(fd)})

    def forward(signum: int, _frame: object) -> None:
        proc.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    return proc.wait()


# --- CLI ----------------------------------------------------------------------------


def _checked_target(args: argparse.Namespace, target: str | Path) -> Path:
    path = Path(target)
    if getattr(args, "within", None):
        check_within(Path(args.within), path)
    return path


def _cmd_put_file(args: argparse.Namespace) -> str:
    data = Path(args.source).read_bytes()
    target = _checked_target(args, args.target)
    status = put_file(target, data, Journal.from_env(), int(args.mode, 8) if args.mode else None)
    if args.ownership:
        record_file(Path(args.ownership), target, data, args.kind, args.tool)
    return status


def _put_artifact(args: argparse.Namespace, target: Path, source: Path, kind: str) -> str:
    ownership = Path(args.ownership) if args.ownership else None
    shipped = source.read_bytes()
    status = install_text_artifact(target, shipped, Journal.from_env(), owned_shipped_digests(ownership, target))
    if status == "kept":
        print(f"warning: {target} differs from what silly-memory installed; left unchanged", file=sys.stderr)
    elif ownership:
        record_file(ownership, target, shipped, kind, args.tool)
    return status


def _cmd_put_artifact(args: argparse.Namespace) -> str:
    return _put_artifact(args, _checked_target(args, args.target), Path(args.source), args.kind)


def _cmd_put_skills(args: argparse.Namespace) -> str:
    """Every file of every shipped skill, in one process (one line per file)."""
    source_dir = Path(args.source_dir)
    lines = []
    for source in sorted(p for p in source_dir.rglob("*") if p.is_file() and p.name != ".DS_Store"):
        rel = source.relative_to(source_dir).as_posix()
        target = _checked_target(args, Path(args.target_dir) / rel)
        lines.append(f"{rel}: {_put_artifact(args, target, source, 'skill')}")
    return "\n".join(lines)


def _cmd_merge(args: argparse.Namespace) -> str:
    target = _checked_target(args, args.target)
    template = json.loads(Path(args.template).read_text(encoding="utf-8"))
    status = merge_json_fragment(args.kind, target, FRAGMENTS[args.kind].extract(template), Journal.from_env())
    if args.ownership:
        record_fragment(Path(args.ownership), target, args.kind)
    return status


def _cmd_validate(args: argparse.Namespace) -> str:
    for kind, target in args.check:
        if kind not in (*FRAGMENTS, "zsh"):
            raise InstallError(f"unknown kind {kind!r}", 2)
        path = _checked_target(args, target)
        if kind == "zsh":
            data = read_bytes(path)
            _split_zsh(data.decode("utf-8") if data is not None else "")
        else:
            validate_json(kind, path)
    return "ok"


def _cmd_zsh(args: argparse.Namespace) -> str:
    memory_zsh = Path(args.memory_zsh) if args.memory_zsh else None
    if not args.remove and memory_zsh is None:
        raise InstallError("--memory-zsh is required unless --remove is given", 2)
    status = apply_zsh_block(Path(args.rc), memory_zsh, Journal.from_env(), remove=args.remove, dry_run=args.dry_run)
    if args.ownership and not args.remove and not args.dry_run:
        record_fragment(Path(args.ownership), Path(args.rc), "zsh")
    return status


def _cmd_revert(args: argparse.Namespace) -> str:
    restored = revert_entries(load_journal(Path(args.journal)), dry_run=args.dry_run)
    return "\n".join(restored) if restored else "nothing to restore"


def _cmd_run_locked(args: argparse.Namespace) -> str:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise InstallError("run-locked needs a command after --", 2)
    raise SystemExit(run_locked(Path(args.lock), args.timeout, command))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install_transaction", description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    within_help = "refuse a target that resolves outside this directory"

    p = sub.add_parser("put-file", help="write one owned file")
    p.add_argument("--target", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--mode")
    p.add_argument("--ownership")
    p.add_argument("--kind", default="file")
    p.add_argument("--tool", default="")
    p.add_argument("--within", help=within_help)
    p.set_defaults(func=_cmd_put_file)

    p = sub.add_parser("put-artifact", help="install or update an owned recall rule or skill file")
    p.add_argument("--target", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--kind", choices=("rule", "skill"), required=True)
    p.add_argument("--ownership")
    p.add_argument("--tool", default="")
    p.add_argument("--within", help=within_help)
    p.set_defaults(func=_cmd_put_artifact)

    p = sub.add_parser("put-skills", help="install or update every shipped skill file")
    p.add_argument("--source-dir", required=True, help="the shipped skills directory")
    p.add_argument("--target-dir", required=True, help="the tool's skills directory")
    p.add_argument("--ownership")
    p.add_argument("--tool", default="")
    p.add_argument("--within", help=within_help)
    p.set_defaults(func=_cmd_put_skills)

    p = sub.add_parser("merge", help="merge the owned hook entries of a shared JSON file")
    p.add_argument("--kind", choices=("cursor-hooks", "claude-hooks"), required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--template", required=True, help="the shipped hooks template")
    p.add_argument("--ownership")
    p.add_argument("--within", help=within_help)
    p.set_defaults(func=_cmd_merge)

    p = sub.add_parser("validate", help="fail when a shared file cannot be merged safely")
    p.add_argument(
        "--check", nargs=2, action="append", required=True, metavar=("KIND", "TARGET"),
        help=f"one of {', '.join((*FRAGMENTS, 'zsh'))} and its file; repeatable",
    )
    p.add_argument("--within", help=within_help)
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("zsh", help="write or remove the managed zsh block")
    p.add_argument("--rc", required=True)
    p.add_argument("--memory-zsh")
    p.add_argument("--remove", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--ownership")
    p.set_defaults(func=_cmd_zsh)

    p = sub.add_parser("revert-journal", help="restore the before-images recorded in a journal")
    p.add_argument("--journal", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_revert)

    p = sub.add_parser("run-locked", help="run a command while holding the upgrade lock")
    p.add_argument("--lock", required=True)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=_cmd_run_locked)

    args = parser.parse_args(argv)
    try:
        print(args.func(args))
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
