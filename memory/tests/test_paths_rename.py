"""The silly-memory home, project marker, and workspace-root rules.

Guards:
  - ``SILLY_MEMORY_HOME`` selects the home; the default is ``~/.silly-memory``.
  - Projects are identified by ``.silly-memory/memory-id``.
  - ``resolve_workspace_root`` maps a subdirectory to its repository (or a
    linked worktree to itself) without spawning git.
  - Generated project files are git-ignored, and only for the selected tools.
  - The CLI prints the neutral product name.

Isolation: every test uses a throwaway HOME, memory home, and project.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MEM_HOME = Path(__file__).resolve().parents[1]
MEM_LIB = MEM_HOME / "lib"
CLI = MEM_HOME / "bin" / "memory"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system.system import config  # noqa: E402
from memory_system import paths  # noqa: E402

_HOME_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN")


class _IsolatedEnv(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_paths_rename_")).resolve()
        self._saved = {k: os.environ.pop(k, None) for k in _HOME_VARS}

    def tearDown(self) -> None:
        for key in _HOME_VARS:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in self._saved.items() if v is not None})
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestMemoryHome(_IsolatedEnv):
    def test_variable_selects_the_home(self) -> None:
        os.environ["SILLY_MEMORY_HOME"] = str(self.tmp / "custom")
        self.assertEqual(config.memory_home(), self.tmp / "custom")

    def test_default_is_the_neutral_home(self) -> None:
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
            self.assertEqual(config.memory_home(), self.tmp / ".silly-memory")
            self.assertEqual(paths.cli_command(), "python3 ~/.silly-memory/bin/memory")

    def test_installed_engine_is_its_own_home(self) -> None:
        """An engine copied beside a VERSION file keeps its data next to its code."""
        engine = self.tmp / "installed-engine"
        shutil.copytree(MEM_LIB, engine / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        (engine / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in _HOME_VARS}
        env.update({"HOME": str(self.tmp / "home"), "PYTHONPATH": str(engine / "lib"), "PYTHONDONTWRITEBYTECODE": "1"})
        proc = subprocess.run(
            [sys.executable, "-c", "from memory_system.system.config import memory_home; print(memory_home())"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(engine))

    def test_shipped_config_uses_neutral_defaults(self) -> None:
        shipped = json.loads((MEM_HOME / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(shipped["memory_home"], "~/.silly-memory")
        self.assertEqual(shipped["tools"], ["cursor"])
        for entry in ("~/.silly-memory", "~/.claude/hooks", "~/.claude/settings.json", "~/.claude/rules", "~/.config/opencode/plugins"):
            self.assertIn(entry, shipped["path_denylist"])
        self.assertEqual(config.DEFAULT_CONFIG["memory_home"], "~/.silly-memory")


class TestHookShimLaunchers(_IsolatedEnv):
    """The Cursor and Claude Code shims pick the same engine as the CLI does."""

    SHIMS = (
        MEM_HOME.parent / "cursor-extras" / "hooks" / "memory-hook.sh",
        MEM_HOME.parent / "claude-code-extras" / "hooks" / "silly-memory-hook.sh",
    )

    def _engine(self, path: Path, name: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'import json; print(json.dumps({{"engine": "{name}"}}))\n', encoding="utf-8")
        return path

    def _run(self, shim: Path, **env: str) -> str:
        base = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(self.tmp)}
        proc = subprocess.run(["bash", str(shim)], input="{}", capture_output=True, text=True, env={**base, **env}, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_memory_bin_wins_then_the_home_engine_and_a_missing_engine_fails_open(self) -> None:
        home = self.tmp / "home"
        self._engine(home / "bin" / "memory", "home")
        self._engine(self.tmp / ".silly-memory" / "bin" / "memory", "default")
        explicit = self._engine(self.tmp / "explicit" / "memory", "explicit")
        for shim in self.SHIMS:
            with self.subTest(shim=shim.name):
                self.assertEqual(self._run(shim, MEMORY_BIN=str(explicit), SILLY_MEMORY_HOME=str(home)), '{"engine": "explicit"}')
                self.assertEqual(self._run(shim, SILLY_MEMORY_HOME=str(home)), '{"engine": "home"}')
                self.assertEqual(self._run(shim), '{"engine": "default"}')
                self.assertEqual(self._run(shim, SILLY_MEMORY_HOME=str(self.tmp / "none")), "{}")


class TestWorkspaceMarkers(_IsolatedEnv):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.tmp / "project"
        self.project.mkdir()

    def test_marker_path(self) -> None:
        self.assertEqual(paths.workspace_marker(self.project), self.project / ".silly-memory" / "memory-id")

    def test_new_workspace_gets_an_ignored_marker(self) -> None:
        ws_id = paths.resolve_workspace_id(self.project)
        self.assertEqual((self.project / ".silly-memory" / "memory-id").read_text(encoding="utf-8").strip(), ws_id)
        self.assertIn("*", (self.project / ".silly-memory" / ".gitignore").read_text(encoding="utf-8").splitlines())
        self.assertFalse((self.project / ".cursor").exists())

    def test_existing_marker_is_reused(self) -> None:
        paths.write_workspace_marker(self.project, "2222222222222222")
        self.assertEqual(paths.resolve_workspace_id(self.project), "2222222222222222")


class TestResolveWorkspaceRoot(_IsolatedEnv):
    def test_subdirectory_resolves_to_the_repository(self) -> None:
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        nested = repo / "packages" / "api"
        nested.mkdir(parents=True)
        self.assertEqual(paths.resolve_workspace_root(nested), repo)
        self.assertEqual(paths.resolve_workspace_root(repo), repo)

    def test_linked_worktree_keeps_its_own_root(self) -> None:
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        worktree = repo / "wt"
        (worktree / "src").mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: ../.git/worktrees/wt\n", encoding="utf-8")
        self.assertEqual(paths.resolve_workspace_root(worktree / "src"), worktree)

    def test_marker_is_used_outside_git(self) -> None:
        project = self.tmp / "plain"
        (project / ".silly-memory").mkdir(parents=True)
        (project / ".silly-memory" / "memory-id").write_text("abcdabcdabcdabcd\n", encoding="utf-8")
        (project / "sub").mkdir()
        self.assertEqual(paths.resolve_workspace_root(project / "sub"), project)

    def test_start_is_used_when_nothing_marks_a_root(self) -> None:
        loose = self.tmp / "loose" / "dir"
        loose.mkdir(parents=True)
        self.assertEqual(paths.resolve_workspace_root(loose), loose)


class TestGeneratedProjectFiles(_IsolatedEnv):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.tmp / "project"
        self.project.mkdir()

    def test_rule_paths_per_tool(self) -> None:
        self.assertEqual(
            paths.generated_rule_paths(self.project, ["cursor", "claude-code", "opencode"]),
            {
                "cursor": self.project / ".cursor" / "rules" / "_memory-context.mdc",
                "claude-code": self.project / ".claude" / "rules" / "_memory-context.md",
            },
        )
        self.assertEqual(paths.generated_rule_path(self.project), self.project / ".cursor" / "rules" / "_memory-context.mdc")

    def test_cursor_only_never_creates_claude_dir(self) -> None:
        paths.ensure_local_gitignore(self.project, ["cursor"])
        self.assertFalse((self.project / ".claude").exists())
        cursor_ignore = (self.project / ".cursor" / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("rules/_memory-context.mdc", cursor_ignore)
        self.assertIn("*", (self.project / ".silly-memory" / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_claude_only_ignores_its_rule_and_skips_cursor(self) -> None:
        paths.ensure_local_gitignore(self.project, ["claude-code"])
        paths.ensure_local_gitignore(self.project, ["claude-code"])
        claude_ignore = (self.project / ".claude" / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(claude_ignore.count("rules/_memory-context.md"), 1)
        # The MCP approval is personal; the project .mcp.json itself is meant to be committed.
        self.assertEqual(claude_ignore.count("settings.local.json"), 1)
        self.assertEqual(claude_ignore[0], "# silly-memory — generated, local-only artifacts")
        self.assertFalse((self.project / ".cursor").exists())


class TestCliUsesNeutralHome(_IsolatedEnv):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.store_home = self.tmp / "stores"
        self.project = self.tmp / "project"
        (self.project / ".git").mkdir(parents=True)

    def _cli(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        base = {k: v for k, v in os.environ.items() if k not in _HOME_VARS}
        base.update({"HOME": str(self.home), "MEMORY_EMBEDDING_BACKEND": "noop", "MEMORY_ALLOW_NETWORK": "0"})
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            env={**base, **env},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_status_workspaces_and_paths_use_neutral_labels(self) -> None:
        env = {"SILLY_MEMORY_HOME": str(self.store_home)}
        outputs = {}
        for args in (("status",), ("paths",)):
            proc = self._cli(*args, "--workspace", str(self.project), **env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            outputs[args[0]] = proc.stdout
        proc = self._cli("workspaces", **env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outputs["workspaces"] = proc.stdout

        self.assertIn("silly-memory — status", outputs["status"])
        self.assertIn("silly-memory — markdown files", outputs["paths"])
        self.assertIn("silly-memory — workspaces", outputs["workspaces"])
        self.assertIn(str(self.project), outputs["workspaces"])
        for text in outputs.values():
            self.assertNotIn("Cursor Memory", text)
        ws_id = (self.project / ".silly-memory" / "memory-id").read_text(encoding="utf-8").strip()
        self.assertTrue((self.store_home / ws_id).is_dir())
        # Rendering, not status, lays out the global store.
        proc = self._cli("render", "--workspace", str(self.project), **env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.store_home / "_global").is_dir())
        self.assertFalse((self.home / ".silly-memory").exists(), "the override must keep the default home untouched")


if __name__ == "__main__":
    unittest.main()
