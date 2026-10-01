"""Remove the installer's tool artifacts, keeping everything the user changed.

Read this first if you are new to the codebase:
  - Candidates are the files in ``<home>/.installed-artifacts.json`` plus the
    known shim, plugin, recall-rule, and skill paths of every tool.
  - A file is removed only when it is exactly what an installer wrote: the
    digest recorded for it, or this checkout's shipped file. Anything else is
    kept and reported.
  - Shared settings lose only owned entries: the memory hook handlers in
    ``~/.cursor/hooks.json`` and ``~/.claude/settings.json``, and the
    ``silly-memory`` MCP entry that runs ``memory-mcp`` in each tracked
    project's ``.mcp.json``, ``.cursor/mcp.json``, and ``opencode.json`` (plus
    Claude Code's approval of it). A foreign entry with the same name stays.
  - The whole plan is computed first: every affected file is parsed and every
    removal decided before the first change, so malformed input changes nothing.

Public interface (imported elsewhere): the ``plan`` and ``apply`` subcommands.
Depends on: install_transaction (sibling, stdlib only).
Used by: uninstall.sh.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402
import copy  # noqa: E402
import os  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_transaction as tx  # noqa: E402
from install_transaction import InstallError  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from memory_system.project_mcp import project_roots, remove_project_mcp  # noqa: E402


@dataclass
class Plan:
    remove: list[Path] = field(default_factory=list)
    keep: list[tuple[Path, str]] = field(default_factory=list)
    edits: dict[Path, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    projects: dict[Path, list[Path]] = field(default_factory=dict)
    zsh: tuple[Path, str] | None = None

    def lines(self) -> list[str]:
        out = [f"remove {p}" for p in self.remove]
        out += [f"edit {p}: drop the silly-memory entries" for p in self.edits]
        out += [f"edit {p}: drop the silly-memory server" for files in self.projects.values() for p in files]
        if self.zsh is not None:
            out.append(f"edit {self.zsh[0]}: drop the silly-memory block")
        out += [f"keep {p} ({why})" for p, why in self.keep]
        return out


def _tool_dirs() -> dict[str, Path]:
    home = Path.home()
    return {
        "cursor": home / ".cursor",
        "claude-code": home / ".claude",
        "opencode": Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "opencode",
    }


def _shipped(repo: Path) -> dict[Path, frozenset[str]]:
    """Known artifact paths and the digests an installer may have written there."""
    dirs = _tool_dirs()
    skills_src = repo / "cursor-extras" / "skills"

    def digest(path: Path) -> frozenset[str]:
        data = tx.read_bytes(path)
        return frozenset({tx.sha256_bytes(data)}) if data is not None else frozenset()

    known: dict[Path, frozenset[str]] = {
        dirs["cursor"] / "hooks" / tx.CURSOR_SHIM: digest(repo / "cursor-extras" / "hooks" / tx.CURSOR_SHIM),
        dirs["claude-code"] / "hooks" / tx.CLAUDE_SHIM: digest(repo / "claude-code-extras" / "hooks" / tx.CLAUDE_SHIM),
        dirs["opencode"] / "plugins" / "silly-memory.js": digest(repo / "opencode-extras" / "plugins" / "silly-memory.js"),
        dirs["cursor"] / "rules" / "memory-recall.mdc": digest(repo / "cursor-extras" / "rules" / "memory-recall.mdc"),
        dirs["claude-code"] / "rules" / "memory-recall.md": digest(repo / "claude-code-extras" / "rules" / "memory-recall.md"),
    }
    sources = sorted(p for p in skills_src.rglob("*") if p.is_file()) if skills_src.is_dir() else []
    for source in sources:
        rel = source.relative_to(skills_src).as_posix()
        for tool in ("cursor", "claude-code", "opencode"):
            known[dirs[tool] / "skills" / rel] = digest(source)
    return known


def _shared_files() -> list[tuple[str, Path]]:
    dirs = _tool_dirs()
    return [("cursor-hooks", dirs["cursor"] / "hooks.json"), ("claude-hooks", dirs["claude-code"] / "settings.json")]


def build_plan(home: Path, repo: Path, remove_zsh: bool) -> Plan:
    plan = Plan()
    ownership = tx.load_ownership(home / tx.OWNERSHIP_FILE)
    recorded: dict[Path, dict[str, Any]] = {
        Path(p): e for p, e in ownership["files"].items() if isinstance(e, dict)
    }
    known = _shipped(repo)
    for path in sorted(set(known) | set(recorded)):
        data = tx.read_bytes(path) if path.is_file() and not path.is_symlink() else None
        if data is None:
            continue
        digest = tx.sha256_bytes(data)
        entry = recorded.get(path, {})
        if entry.get("sha256") == digest or digest in known.get(path, frozenset()):
            plan.remove.append(path)
        else:
            plan.keep.append((path, "changed since it was installed"))

    problems: list[str] = []
    for kind, path in _shared_files():
        try:
            doc, existed = tx.load_json_object(path)
            tx.validate_json(kind, path)
        except InstallError as exc:
            problems.append(str(exc))
            continue
        handler = tx.FRAGMENTS[kind]
        if not existed or not handler.extract(doc):
            continue
        plan.edits[path] = (kind, handler.remove(copy.deepcopy(doc)))
    for root in project_roots(home):
        files = remove_project_mcp(root, dry_run=True)
        if files:
            plan.projects[root] = files
    if remove_zsh:
        rc = Path.home() / ".zshrc"
        data = tx.read_bytes(rc)
        text = data.decode("utf-8") if data is not None else ""
        try:
            if tx.zsh_owned_blocks(text):
                plan.zsh = (rc, tx.zsh_with_blocks(text, []))
        except InstallError as exc:
            problems.append(f"{rc}: {exc}")
    if problems:
        raise InstallError("nothing was removed:\n  " + "\n  ".join(problems), tx.EXIT_INVALID)
    return plan


def apply_plan(plan: Plan) -> None:
    for path in plan.remove:
        path.unlink(missing_ok=True)
        # Emptied folders inside a tool's skills folder go too; that folder itself stays.
        skills_root = next((p for p in path.parents if p.name == "skills"), None)
        parent = path.parent
        while skills_root is not None and parent != skills_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    for path, (_, doc) in plan.edits.items():
        tx.atomic_write_bytes(path, tx._dump_json(doc))
    for root in plan.projects:
        remove_project_mcp(root)
    if plan.zsh is not None:
        rc, text = plan.zsh
        tx.atomic_write_bytes(rc, text.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uninstall_transaction", description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument("--home", required=True, help="the memory home being uninstalled")
    parser.add_argument("--repo", required=True, help="the checkout holding the shipped artifacts")
    parser.add_argument("--remove-zsh", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = build_plan(Path(args.home), Path(args.repo), args.remove_zsh)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    print("\n".join(plan.lines()) or "nothing to remove")
    if args.command == "apply":
        try:
            apply_plan(plan)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
