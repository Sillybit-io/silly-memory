"""uninstall.sh removes every tool's install artifacts and keeps user data.

Guards:
  - The engine footprint in the memory home is removed; config.json, _global/,
    every workspace store, and project markers stay byte-identical.
  - Cursor, Claude Code, and OpenCode lose their shim or plugin, unmodified
    recall rules and skills, and owned hook handlers; each tracked project loses
    the silly-memory server from its .mcp.json, .cursor/mcp.json, and
    opencode.json (and Claude's approval of it). Foreign hooks, servers,
    plugins, a skill the user edited, and a foreign server named silly-memory
    stay; the global MCP registries were never written.
  - Every shared file is parsed before anything is removed: invalid
    ~/.claude/settings.json stops the uninstall with HOME unchanged.
  - --dry-run lists every tool's paths and deletes nothing;
    --remove-zsh-helper and --remove-backups remove the shell block and the
    upgrade backups.

Runs against a throwaway HOME built once per class — never the real one.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = PROJECT_ROOT / "install.sh"
UNINSTALL_SH = PROJECT_ROOT / "uninstall.sh"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
HOME_VARS = ("SILLY_MEMORY_HOME", "SILLY_MEMORY_UPGRADE_JOURNAL", "XDG_CONFIG_HOME", "MEMORY_BIN")

# Install footprint paths that uninstall.sh must remove from the memory home.
INSTALL_FOOTPRINT_RELATIVE = (
    "bin",
    "lib",
    "tests",
    "hooks",
    "cursor-extras",
    "memory.zsh",
    "install-zsh.sh",
    "README.md",
    "VERSION",
    ".installed-artifacts.json",
    ".first-run-seen",
    ".upgraded-from",
    ".install.lock",
    ".upgrade.lock",
)
FOREIGN_CLAUDE_GROUP = {"hooks": [{"type": "command", "command": "afplay done.aiff"}]}
FOREIGN_CURSOR_HOOK = {"command": "~/bin/cursor-notify.sh"}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


@unittest.skipUnless(
    INSTALL_SH.exists() and UNINSTALL_SH.exists(),
    "install.sh and uninstall.sh must be adjacent to the source tree",
)
class TestUninstallResidue(unittest.TestCase):
    root: Path
    home: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="uninstall_residue_"))
        cls.home = cls.root / "home"
        py_bin = cls.root / "py-bin"
        py_bin.mkdir()
        (py_bin / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        (py_bin / "python3").chmod(0o755)
        cls.path = f"{py_bin}:{SYSTEM_PATH}"

        home = cls.home
        (home / ".cursor").mkdir(parents=True)
        (home / ".claude").mkdir()
        (home / ".config" / "opencode" / "plugins").mkdir(parents=True)
        (home / ".zshrc").write_text("export EDITOR=vim\n", encoding="utf-8")
        (home / ".cursor" / "hooks.json").write_text(
            json.dumps({"version": 1, "hooks": {"stop": [FOREIGN_CURSOR_HOOK]}}), encoding="utf-8"
        )
        (home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}), encoding="utf-8")
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"Stop": [FOREIGN_CLAUDE_GROUP]}}), encoding="utf-8"
        )
        (home / ".claude.json").write_text(
            json.dumps({"numStartups": 3, "mcpServers": {"other": {"command": "other-mcp"}}}), encoding="utf-8"
        )
        (home / ".config" / "opencode" / "plugins" / "other.js").write_text("export default {}\n", encoding="utf-8")
        result = cls._run(INSTALL_SH, "--tools", "cursor,claude-code,opencode", "--mcp")
        assert result.returncode == 0, f"install.sh failed:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}"

        memory_home = home / ".silly-memory"
        ws = memory_home / "ws-test"
        (ws / "memory-bank").mkdir(parents=True)
        (ws / "memory-bank" / "test-note.md").write_text("# Test bank\n\n- canonical fact: do not delete me\n", encoding="utf-8")
        (ws / "events.jsonl").write_text('{"id":"ev1","type":"prompt","text":"hello"}\n', encoding="utf-8")
        (ws / "observations.md").write_text("# Observations\n\n- \U0001f7e1 [2026-06-15] #note: keep me\n", encoding="utf-8")
        (ws / "work-state.md").write_text("# Work State\n\nBranch: main\n", encoding="utf-8")
        project = cls.root / "projects" / "alpha"
        (project / ".silly-memory").mkdir(parents=True)
        (project / ".silly-memory" / "memory-id").write_text("ws-test\n", encoding="utf-8")
        (ws / ".meta.json").write_text(json.dumps({"workspace_id": "ws-test", "workspace_root": str(project)}), encoding="utf-8")
        (memory_home / "_global" / "global-note.md").write_text("# Global bank\n\n- keep me\n", encoding="utf-8")
        config = _json(memory_home / "config.json")
        config["user_override"] = "do-not-clobber"
        (memory_home / "config.json").write_text(json.dumps(config), encoding="utf-8")
        # A team server already committed in the project; the first session adds ours beside it.
        (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}), encoding="utf-8")
        start = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(project), "source": "startup"}
        hook = subprocess.run(
            [sys.executable, str(memory_home / "bin" / "memory"), "hook", "--tool", "claude-code"],
            input=json.dumps(start), capture_output=True, text=True, timeout=120,
            env={"PATH": cls.path, "HOME": str(home), "MEMORY_EMBEDDING_BACKEND": "noop", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert (project / "opencode.json").is_file(), f"the first session wrote no project MCP config: {hook.stderr[-2000:]}"

        cls.template = cls.root / "template"
        shutil.copytree(home, cls.template / "home", symlinks=True)
        shutil.copytree(cls.root / "projects", cls.template / "projects", symlinks=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self) -> None:
        for name in ("home", "projects"):
            shutil.rmtree(self.root / name, ignore_errors=True)
            shutil.copytree(self.template / name, self.root / name, symlinks=True)
        self.memory_home = self.home / ".silly-memory"
        self.project = self.root / "projects" / "alpha"

    @classmethod
    def _run(cls, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in HOME_VARS}
        env.update(
            {
                "HOME": str(cls.home),
                "PATH": cls.path,
                "MEMORY_ALLOW_NETWORK": "0",
                "MEMORY_EMBEDDING_BACKEND": "noop",
                "MEMORY_SKIP_MODEL_DOWNLOAD": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return subprocess.run(
            ["bash", str(script), *args], cwd=str(cls.home), capture_output=True, text=True, env=env, timeout=300, check=False
        )

    def _uninstall(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self._run(UNINSTALL_SH, *args)
        self.assertEqual(result.returncode, 0, msg=f"uninstall.sh failed:\nstdout=\n{result.stdout}\nstderr=\n{result.stderr}")
        return result

    # --- the contract ----------------------------------------------------------

    def test_uninstall_clears_footprint_and_preserves_user_data(self) -> None:
        self.assertTrue((self.memory_home / "bin" / "memory").exists())
        before = _tree(self.memory_home)
        self._uninstall("--confirm")
        for rel in INSTALL_FOOTPRINT_RELATIVE:
            path = self.memory_home / rel
            self.assertFalse(path.exists() or path.is_symlink(), msg=f"install footprint not removed: {path}")
        after = _tree(self.memory_home)
        self.assertEqual(sorted(set(after) - set(before)), [], msg="stray files appeared post-uninstall")

    def test_user_data_preserved_under_silly_memory(self) -> None:
        user_data = {rel: sha for rel, sha in _tree(self.memory_home).items()
                     if rel == "config.json" or rel.startswith(("_global/", "ws-test/"))}
        marker = self.root / "projects" / "alpha" / ".silly-memory" / "memory-id"
        self._uninstall("--confirm")
        self.assertEqual({rel: sha for rel, sha in _tree(self.memory_home).items() if rel in user_data}, user_data)
        self.assertEqual(_json(self.memory_home / "config.json")["user_override"], "do-not-clobber")
        self.assertEqual(marker.read_text(encoding="utf-8"), "ws-test\n")

    def test_claude_code_artifacts_removed_and_foreign_settings_kept(self) -> None:
        claude = self.home / ".claude"
        edited = claude / "skills" / "query-memory" / "SKILL.md"
        edited.write_text(edited.read_text(encoding="utf-8") + "\n## Team notes\n\nAsk first.\n", encoding="utf-8")
        result = self._uninstall("--confirm")
        self.assertFalse((claude / "hooks" / "silly-memory-hook.sh").exists())
        self.assertFalse((claude / "rules" / "memory-recall.md").exists())
        self.assertEqual(_json(claude / "settings.json"), {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"Stop": [FOREIGN_CLAUDE_GROUP]}})
        self.assertEqual(_json(self.home / ".claude.json"), {"numStartups": 3, "mcpServers": {"other": {"command": "other-mcp"}}})
        self.assertIn("Ask first.", edited.read_text(encoding="utf-8"))
        self.assertEqual([p.name for p in (claude / "skills").iterdir()], ["query-memory"])
        self.assertIn(f"keep {edited}", result.stderr)

    def test_opencode_plugin_and_skills_removed(self) -> None:
        opencode = self.home / ".config" / "opencode"
        self._uninstall("--confirm")
        self.assertFalse((opencode / "plugins" / "silly-memory.js").exists())
        self.assertTrue((opencode / "plugins" / "other.js").exists())
        self.assertEqual(list((opencode / "skills").iterdir()), [])

    def test_cursor_artifacts_removed_and_foreign_settings_kept(self) -> None:
        cursor = self.home / ".cursor"
        self._uninstall("--confirm")
        self.assertEqual(_json(cursor / "mcp.json"), {"mcpServers": {"github": {"command": "gh-mcp"}}})
        self.assertEqual(_json(cursor / "hooks.json"), {"version": 1, "hooks": {"stop": [FOREIGN_CURSOR_HOOK]}})
        self.assertFalse((cursor / "hooks" / "memory-hook.sh").exists())
        self.assertFalse((cursor / "rules" / "memory-recall.mdc").exists())
        self.assertEqual(list((cursor / "skills").iterdir()), [])

    def test_foreign_server_named_silly_memory_is_kept(self) -> None:
        registry = self.home / ".claude.json"
        doc = _json(registry)
        doc["mcpServers"]["silly-memory"] = {"command": "npx", "args": ["-y", "someone-elses-memory"]}
        registry.write_text(json.dumps(doc), encoding="utf-8")
        self._uninstall("--confirm")
        self.assertEqual(_json(registry)["mcpServers"]["silly-memory"], {"command": "npx", "args": ["-y", "someone-elses-memory"]})

    def test_dry_run_lists_every_tool_and_deletes_nothing(self) -> None:
        before = _tree(self.home)
        result = self._uninstall("--dry-run", "--remove-zsh-helper")
        projects_before = _tree(self.project)
        for path in (
            self.home / ".claude" / "hooks" / "silly-memory-hook.sh",
            self.home / ".config" / "opencode" / "plugins" / "silly-memory.js",
            self.home / ".zshrc",
            self.project / ".mcp.json",
            self.project / ".cursor" / "mcp.json",
            self.project / "opencode.json",
            self.project / ".claude" / "settings.local.json",
        ):
            self.assertIn(str(path), result.stderr)
        self.assertEqual(_tree(self.home), before)
        self.assertEqual(_tree(self.project), projects_before)

    def test_project_mcp_entries_removed_and_committed_servers_kept(self) -> None:
        self._uninstall("--confirm")
        self.assertEqual(_json(self.project / ".mcp.json"), {"mcpServers": {"github": {"command": "gh-mcp"}}})
        for rel in (".cursor/mcp.json", "opencode.json", ".claude/settings.local.json"):
            self.assertFalse((self.project / rel).exists(), f"{rel} held only silly-memory's entry")
        self.assertEqual((self.project / ".silly-memory" / "memory-id").read_text(encoding="utf-8"), "ws-test\n")

    def test_invalid_claude_settings_stop_everything(self) -> None:
        (self.home / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        before = _tree(self.home)
        for args in (("--dry-run",), ("--confirm", "--remove-zsh-helper", "--remove-backups")):
            with self.subTest(args=args):
                result = self._run(UNINSTALL_SH, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("settings.json is not valid JSON", result.stderr)
                self.assertEqual(_tree(self.home), before)

    def test_zsh_block_and_backups_removed_on_request(self) -> None:
        backups = [self.home / f".silly-memory.upgrade-backup-{n}-2026010{n}T000000Z" for n in (1, 2)]
        for backup in backups:
            (backup / "bin").mkdir(parents=True)
        self.assertIn("# >>> silly-memory >>>", (self.home / ".zshrc").read_text(encoding="utf-8"))
        self._uninstall("--confirm", "--remove-zsh-helper", "--remove-backups")
        self.assertEqual((self.home / ".zshrc").read_text(encoding="utf-8"), "export EDITOR=vim\n")
        for backup in backups:
            self.assertFalse(backup.exists(), msg=str(backup))


if __name__ == "__main__":
    unittest.main(verbosity=2)
