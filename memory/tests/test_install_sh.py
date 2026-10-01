"""install.sh and its transaction helper (``system/install_transaction.py``).

install.sh runs against a throwaway HOME with model downloads disabled
(MEMORY_SKIP_MODEL_DOWNLOAD=1), neither home variable inherited, and a PATH
holding only python3, the system tools, and per-test fake tool commands, so a
real ``cursor``, ``claude``, or ``opencode`` on this machine is never detected.

Guards:
  - Tool detection by config folder or command on PATH; ``--tools`` wins over
    detection; an unknown name exits 2 before anything is written.
  - Shared settings: only the owned hook handlers change, foreign
    keys/hooks stay, reruns change nothing, and a malformed file, fence, or
    escaping link fails with HOME untouched.
  - No MCP server is registered globally: ``~/.claude.json`` and
    ``~/.cursor/mcp.json`` are never written, and the ``claude`` command is never
    run. The server is off by default; with ``--mcp`` the installed engine adds
    it to each project's own config at its first session. A plain rerun keeps
    the setting, and turning it off (``--no-mcp``, or a rerun over an install
    from before the setting existed) removes our entry from tracked projects.
  - Neutral, custom-home, and OpenCode-only installs, including the engine copy
    of the recall rule the OpenCode plugin reads.
  - Recall rules and skills: a file is updated only while it still matches what
    the installer last wrote there; a changed or unrelated file with the same
    name is left alone with a warning.
  - Journal: no external write precedes its journal entry; ``revert-journal``
    restores exact before-images and refuses a third value; the upgrade lock is
    released once every process holding it has been killed.

Skipped automatically when install.sh isn't sitting next to the source tree
(e.g. when the regression suite is run from inside the installed memory home).
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = PROJECT_ROOT / "install.sh"
# The engine directory: memory/ in the source tree, the memory home once installed.
MEM_ROOT = Path(__file__).resolve().parents[1]
INSTALL_ZSH = MEM_ROOT / "install-zsh.sh"
TX_PY = MEM_ROOT / "lib" / "memory_system" / "system" / "install_transaction.py"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
HOME_VARS = ("SILLY_MEMORY_HOME", "SILLY_MEMORY_UPGRADE_JOURNAL", "XDG_CONFIG_HOME", "MEMORY_BIN")
CLI_EXPRESSION = 'python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory"'
CURSOR_EVENTS = {
    "sessionStart", "beforeSubmitPrompt", "afterAgentResponse", "afterFileEdit",
    "afterShellExecution", "preCompact", "sessionEnd", "stop",
}
CLAUDE_EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact", "SessionEnd"}
SKILLS_DIR = PROJECT_ROOT / "cursor-extras" / "skills"
SKILLS = sorted(p.name for p in SKILLS_DIR.iterdir() if p.is_dir()) if SKILLS_DIR.is_dir() else []


def _load_tx() -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_transaction_under_test", TX_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(*roots: Path) -> dict[str, bytes]:
    """Every file under (or at) each root, keyed by path, with its bytes."""
    files: dict[str, bytes] = {}
    for root in roots:
        if root.is_file():
            files[str(root)] = root.read_bytes()
        elif root.is_dir():
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    files[str(path)] = path.read_bytes()
    return files


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class _InstallCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="install_sh_test_"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        # install-zsh.sh rewrites ~/.zshrc; an empty file is fine.
        (self.home / ".zshrc").write_text("", encoding="utf-8")
        self.dest = self.home / ".silly-memory"
        self.fake_bin = self.tmp / "fake-bin"
        self.fake_bin.mkdir()
        self.cli_log = self.tmp / "tool-cli-calls.log"
        # A wrapper (not a symlink) keeps a virtualenv's interpreter working.
        self.py_bin = self.tmp / "py-bin"
        self.py_bin.mkdir()
        self.python = self.py_bin / "python3"
        self.python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        self.python.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, **overrides: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in HOME_VARS}
        env.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.fake_bin}:{self.py_bin}:{SYSTEM_PATH}",
                "MEMORY_SKIP_MODEL_DOWNLOAD": "1",
                "MEMORY_EMBEDDING_BACKEND": "noop",
            }
        )
        env.update(overrides)
        return env

    def _run(self, *extra_args: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(INSTALL_SH), *extra_args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            env=self._env(**env_overrides),
            timeout=300,
        )

    def _ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(
            result.returncode, 0, msg=f"install.sh failed:\nstdout=\n{result.stdout}\nstderr=\n{result.stderr}"
        )

    def _tx(self, *args: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TX_PY), *args],
            capture_output=True,
            text=True,
            env=self._env(**env_overrides),
            timeout=60,
        )

    def _fake_cli(self, name: str) -> None:
        """A tool command that only records that it was run (and fails)."""
        script = self.fake_bin / name
        script.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.cli_log}"\nexit 1\n', encoding="utf-8")
        script.chmod(0o755)

    def _config_tools(self, dest: Path | None = None) -> list[str]:
        return _json((dest or self.dest) / "config.json")["tools"]

    def _assert_cursor_hooks(self, hooks_json: Path) -> None:
        hooks = _json(hooks_json)["hooks"]
        for event in CURSOR_EVENTS:
            commands = [e.get("command", "") for e in hooks.get(event, [])]
            self.assertEqual(sum(c.endswith("/memory-hook.sh") for c in commands), 1, msg=f"{event}: {commands}")

    def _assert_claude_hooks(self, settings: Path) -> None:
        hooks = _json(settings)["hooks"]
        self.assertTrue(CLAUDE_EVENTS <= set(hooks), msg=sorted(hooks))
        for event in CLAUDE_EVENTS:
            commands = [h["command"] for group in hooks[event] for h in group.get("hooks", [])]
            owned = [c for c in commands if c.endswith("/silly-memory-hook.sh")]
            self.assertEqual(len(owned), 2 if event == "PostToolUse" else 1, msg=f"{event}: {commands}")

    def _run_new_home_command(self, text: str, project: Path, dest: Path | None = None) -> None:
        """Run the first runnable CLI line of a rule or skill, as the agent would."""
        lines = [line.strip() for line in text.splitlines() if line.strip().startswith(CLI_EXPRESSION)]
        runnable = [line.replace('"<keywords>"', '"deploy"') for line in lines]
        runnable = [line for line in runnable if "<" not in line and "|" not in line]
        self.assertTrue(runnable, msg=f"no new-home command in:\n{text}")
        env = self._env(**({"SILLY_MEMORY_HOME": str(dest)} if dest else {}))
        result = subprocess.run(
            ["bash", "-c", runnable[0]], cwd=str(project), capture_output=True, text=True, env=env, timeout=120
        )
        self.assertEqual(result.returncode, 0, msg=f"{runnable[0]}\nstdout={result.stdout}\nstderr={result.stderr}")

    def _project(self) -> Path:
        project = self.tmp / "project"
        (project / ".git").mkdir(parents=True, exist_ok=True)
        return project


@unittest.skipUnless(INSTALL_SH.exists(), "install.sh not adjacent to the source tree")
class TestInstallSh(_InstallCase):
    # --- functional contract --------------------------------------------------
    def test_install_runs_clean_and_a_rerun_succeeds(self) -> None:
        first = self._run()
        self._ok(first)
        self.assertTrue((self.dest / "bin" / "memory").exists(), msg="bin/memory was not copied into fake HOME")
        self.assertTrue(os.access(self.dest / "bin" / "memory-mcp", os.X_OK))
        self.assertIn("preflight: FTS5 OK", first.stdout)
        self.assertIn("embedding backend selected:", first.stdout)
        # Nothing detected: nothing wired, and the message says how to wire one.
        self.assertEqual(self._config_tools(), [])
        self.assertIn("--tools", first.stdout)
        self.assertFalse((self.home / ".cursor").exists())
        second = self._run()
        self._ok(second)
        # Re-run must never imply an HF network download.
        self.assertNotIn("downloaded ->", second.stderr)

    def test_existing_home_is_owner_only_and_loses_modules_dropped_upstream(self) -> None:
        """Installs left the home at 0755, and rsync without --delete kept stale modules."""
        lib = self.dest / "lib" / "memory_system"
        lib.mkdir(parents=True)
        self.dest.chmod(0o755)
        (lib / "status.py").write_text("# stale flat module\n", encoding="utf-8")
        user_file = self.dest / "config.json"
        user_file.write_text('{"keep": true}\n', encoding="utf-8")
        self._ok(self._run())
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o700)
        self.assertFalse((lib / "status.py").exists())
        self.assertTrue((lib / "status" / "__init__.py").exists())
        self.assertEqual(_json(user_file), {"keep": True, "tools": [], "mcp": False})

    def test_unknown_flag_exits_2_and_installs_nothing(self) -> None:
        """`./install.sh --dry-run` silently ran a REAL install (unknown flags were ignored)."""
        result = self._run("--dry-run")
        self.assertEqual(result.returncode, 2, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        self.assertIn("unknown option: --dry-run", result.stderr)
        self.assertFalse(self.dest.exists(), msg="install.sh wrote into HOME")

    def test_help_documents_tools(self) -> None:
        result = self._run("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--tools LIST", result.stdout)
        self.assertIn("cursor, claude-code, opencode", result.stdout)
        self.assertIn("SILLY_MEMORY_HOME", result.stdout)
        self.assertIn("--mcp ", result.stdout)
        self.assertIn("--no-mcp ", result.stdout)

    # --- preflight gating -----------------------------------------------------
    def test_fts5_preflight_failure_exits_2(self) -> None:
        result = self._run(MEMORY_FORCE_NO_FTS5="1")
        self.assertEqual(result.returncode, 2, msg=f"stdout={result.stdout}\nstderr={result.stderr}")
        self.assertIn("FTS5", result.stderr)

    # --- embedding selection chain -------------------------------------------
    def test_no_torch_flag_skips_sentence_transformers(self) -> None:
        result = self._run("--no-torch")
        self._ok(result)
        selection_lines = [line for line in result.stdout.splitlines() if "embedding backend selected:" in line]
        self.assertEqual(len(selection_lines), 1, msg=result.stdout)
        # With --no-torch we must never pick sentence-transformers; the chain
        # collapses to fastembed (if installed) or noop.
        self.assertNotIn("sentence-transformers", selection_lines[0])
        self.assertIn("--no-torch passed; skipping sentence-transformers", result.stderr)

    def test_embedding_fallback_to_noop_when_no_libs(self) -> None:
        # PYTHONNOUSERSITE=1 hides user-site ML installs. If the dev box still has
        # them globally, assert the weaker invariant: a known backend is selected.
        result = self._run(PYTHONNOUSERSITE="1")
        self._ok(result)
        selection_lines = [line for line in result.stdout.splitlines() if "embedding backend selected:" in line]
        self.assertEqual(len(selection_lines), 1, msg=result.stdout)
        selected = selection_lines[0].split(":", 1)[1].strip()
        self.assertIn(selected, {"sentence-transformers", "fastembed", "noop"})


@unittest.skipUnless(INSTALL_SH.exists(), "install.sh not adjacent to the source tree")
class TestToolSelection(_InstallCase):
    def test_claude_found_by_folder_is_wired_without_its_cli(self) -> None:
        """QA happy path: six hook events, untouched permissions, tools lists claude-code."""
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"permissions":{"allow":["Bash(ls)"]}}', encoding="utf-8")
        result = self._run()
        self._ok(result)

        doc = _json(settings)
        self.assertEqual(doc["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(set(doc), {"permissions", "hooks"})
        self.assertEqual(set(doc["hooks"]), CLAUDE_EVENTS)
        self._assert_claude_hooks(settings)
        self.assertEqual(self._config_tools(), ["claude-code"])
        self.assertFalse((self.home / ".claude.json").exists(), "nothing is registered globally")
        shim = self.home / ".claude" / "hooks" / "silly-memory-hook.sh"
        self.assertTrue(os.access(shim, os.X_OK))
        rule = self.home / ".claude" / "rules" / "memory-recall.md"
        self.assertEqual(rule.read_bytes(), (PROJECT_ROOT / "claude-code-extras" / "rules" / "memory-recall.md").read_bytes())
        self.assertEqual(sorted(p.name for p in (self.home / ".claude" / "skills").iterdir()), SKILLS)
        self.assertFalse((self.home / ".cursor").exists())
        self.assertFalse((self.home / ".config" / "opencode").exists())
        self.assertIn("Claude Code: start a new session", result.stdout)

        # The installed shim reaches the installed engine.
        project = self._project()
        payload = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(project), "source": "startup"}
        hook = subprocess.run(
            ["bash", str(shim)], input=json.dumps(payload), capture_output=True, text=True, env=self._env(), timeout=60
        )
        self.assertEqual(hook.returncode, 0, msg=hook.stderr)
        self.assertEqual(json.loads(hook.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")
        workspace_id = (project / ".silly-memory" / "memory-id").read_text(encoding="utf-8").strip()
        self.assertTrue((self.dest / workspace_id).is_dir())
        # MCP is off by default: the skills do the work, and no project file names the server.
        self.assertIs(_json(self.dest / "config.json")["mcp"], False)
        self.assertIn("MCP server: off", result.stdout)
        self.assertIn("the add-memory and query-memory skills", result.stdout)
        self.assertFalse((project / ".mcp.json").exists())
        self.assertFalse((project / ".claude" / "settings.local.json").exists())
        self.assertFalse((self.home / ".claude.json").exists())
        self._run_new_home_command(rule.read_text(encoding="utf-8"), project)

        # A rerun changes no tool file.
        wired = (self.home / ".claude", self.home / ".claude.json", self.home / ".zshrc")
        before = _tree(*wired)
        self._ok(self._run())
        self.assertEqual(_tree(*wired), before)

    def test_every_tool_found_by_command_and_the_claude_cli_is_never_run(self) -> None:
        for name in ("cursor", "claude", "opencode"):
            self._fake_cli(name)
        result = self._run()
        self._ok(result)
        self.assertEqual(self._config_tools(), ["cursor", "claude-code", "opencode"])
        self.assertFalse(self.cli_log.exists(), msg=self.cli_log.read_text() if self.cli_log.exists() else "")

        self._assert_cursor_hooks(self.home / ".cursor" / "hooks.json")
        self._assert_claude_hooks(self.home / ".claude" / "settings.json")
        self.assertFalse((self.home / ".cursor" / "mcp.json").exists(), "nothing is registered globally")
        self.assertFalse((self.home / ".claude.json").exists(), "nothing is registered globally")
        plugin = self.home / ".config" / "opencode" / "plugins" / "silly-memory.js"
        self.assertEqual(plugin.read_bytes(), (PROJECT_ROOT / "opencode-extras" / "plugins" / "silly-memory.js").read_bytes())
        for skills in (self.home / ".cursor" / "skills", self.home / ".claude" / "skills", self.home / ".config" / "opencode" / "skills"):
            self.assertEqual(sorted(p.name for p in skills.iterdir()), SKILLS, msg=str(skills))
        for line in ("Cursor: restart Cursor", "Claude Code: start a new session", "OpenCode: the plugin reloads"):
            self.assertIn(line, result.stdout)

        doctor = subprocess.run(
            [sys.executable, str(self.dest / "bin" / "memory"), "doctor"],
            capture_output=True, text=True, env=self._env(), timeout=180,
        )
        checks = {line.split()[1]: line for line in doctor.stdout.splitlines() if line.startswith("  ") and len(line.split()) > 1}
        for check in ("tools", "mcp"):
            self.assertIn("✅", checks.get(check, ""), msg=doctor.stdout)

    def test_tools_flag_wins_and_opencode_only_touches_no_other_tool(self) -> None:
        (self.home / ".claude").mkdir()
        self._fake_cli("cursor")
        result = self._run("--tools", "opencode")
        self._ok(result)
        self.assertEqual(self._config_tools(), ["opencode"])
        opencode = self.home / ".config" / "opencode"
        plugin = opencode / "plugins" / "silly-memory.js"
        self.assertEqual(plugin.read_bytes(), (PROJECT_ROOT / "opencode-extras" / "plugins" / "silly-memory.js").read_bytes())
        # The plugin injects this engine copy of the recall rule.
        self.assertIn('join(home, "cursor-extras", "rules", "memory-recall.mdc")', plugin.read_text(encoding="utf-8"))
        engine_rule = self.dest / "cursor-extras" / "rules" / "memory-recall.mdc"
        self.assertEqual(engine_rule.read_bytes(), (PROJECT_ROOT / "cursor-extras" / "rules" / "memory-recall.mdc").read_bytes())
        self.assertEqual(sorted(p.name for p in (opencode / "skills").iterdir()), SKILLS)
        self.assertFalse((self.home / ".cursor").exists())
        self.assertEqual(list((self.home / ".claude").iterdir()), [])
        self.assertFalse((self.home / ".claude.json").exists())
        self.assertFalse(self.cli_log.exists())

    def test_unknown_tool_exits_2_and_creates_nothing(self) -> None:
        """QA failure path: `./install.sh --tools nope`."""
        before = _tree(self.home)
        for value in ("nope", "cursor,nope", ""):
            with self.subTest(tools=value):
                result = self._run("--tools", value)
                self.assertEqual(result.returncode, 2, msg=result.stderr)
                self.assertIn("claude-code", result.stderr)
                self.assertEqual(_tree(self.home), before)
                self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".zshrc"])

    def test_custom_home_is_named_by_every_entry_point(self) -> None:
        custom = self.tmp / "custom home"
        result = self._run("--tools", "claude-code", SILLY_MEMORY_HOME=str(custom))
        self._ok(result)
        self.assertTrue((custom / "bin" / "memory").exists())
        self.assertFalse(self.dest.exists())
        self.assertEqual(self._config_tools(custom), ["claude-code"])
        self.assertTrue((custom / ".installed-artifacts.json").exists())
        self.assertFalse((self.home / ".claude.json").exists())
        zshrc = (self.home / ".zshrc").read_text(encoding="utf-8")
        self.assertIn(f'export SILLY_MEMORY_HOME="{custom}"', zshrc)
        self.assertIn(f'source "{custom}/memory.zsh"', zshrc)
        self.assertIn("Custom home:", result.stdout)

        project = self._project()
        shim = self.home / ".claude" / "hooks" / "silly-memory-hook.sh"
        payload = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(project), "source": "startup"}
        hook = subprocess.run(
            ["bash", str(shim)], input=json.dumps(payload), capture_output=True, text=True,
            env=self._env(SILLY_MEMORY_HOME=str(custom)), timeout=60,
        )
        self.assertEqual(hook.returncode, 0, msg=hook.stderr)
        workspace_id = (project / ".silly-memory" / "memory-id").read_text(encoding="utf-8").strip()
        self.assertTrue((custom / workspace_id).is_dir())
        self._run_new_home_command((self.home / ".claude" / "rules" / "memory-recall.md").read_text(encoding="utf-8"), project, custom)


@unittest.skipUnless(INSTALL_SH.exists(), "install.sh not adjacent to the source tree")
class TestMcpSetting(_InstallCase):
    def _session_start(self, project: Path) -> None:
        payload = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(project), "source": "startup"}
        hook = subprocess.run(
            ["bash", str(self.home / ".claude" / "hooks" / "silly-memory-hook.sh")], input=json.dumps(payload),
            capture_output=True, text=True, env=self._env(), timeout=60,
        )
        self.assertEqual(hook.returncode, 0, msg=hook.stderr)

    def _mcp(self) -> object:
        return _json(self.dest / "config.json").get("mcp", "absent")

    def test_mcp_is_opt_in_kept_on_rerun_and_removed_when_turned_off(self) -> None:
        project = self._project()
        team = {"github": {"command": "gh-mcp"}}
        (project / ".mcp.json").write_text(json.dumps({"mcpServers": team}), encoding="utf-8")

        on = self._run("--tools", "claude-code", "--mcp")
        self._ok(on)
        self.assertIs(self._mcp(), True)
        self.assertIn("MCP server: on", on.stdout)
        self.assertIn("Memory tools (MCP)", on.stdout)
        self._session_start(project)
        self.assertEqual(set(_json(project / ".mcp.json")["mcpServers"]), {"github", "silly-memory"})
        self.assertTrue((project / ".claude" / "settings.local.json").is_file())

        # A plain rerun (and so an upgrade) keeps the setting and the project's entry.
        self._ok(self._run("--tools", "claude-code"))
        self.assertIs(self._mcp(), True)
        self.assertIn("silly-memory", _json(project / ".mcp.json")["mcpServers"])

        # An install from before the setting existed always registered the server:
        # a plain rerun turns it off and takes our entry out of every tracked project.
        config = _json(self.dest / "config.json")
        del config["mcp"]
        (self.dest / "config.json").write_text(json.dumps(config), encoding="utf-8")
        off = self._run("--tools", "claude-code")
        self._ok(off)
        self.assertIs(self._mcp(), False)
        self.assertIn(f"removed the silly-memory server from {project.resolve() / '.mcp.json'}", off.stderr)
        self.assertEqual(_json(project / ".mcp.json"), {"mcpServers": team})
        self.assertFalse((project / ".claude" / "settings.local.json").exists())
        self._session_start(project)
        self.assertEqual(_json(project / ".mcp.json"), {"mcpServers": team}, "an off session start adds nothing")

        # --no-mcp turns it off again after --mcp.
        self._ok(self._run("--tools", "claude-code", "--mcp"))
        self._session_start(project)
        self.assertIn("silly-memory", _json(project / ".mcp.json")["mcpServers"])
        self._ok(self._run("--tools", "claude-code", "--no-mcp"))
        self.assertIs(self._mcp(), False)
        self.assertEqual(_json(project / ".mcp.json"), {"mcpServers": team})
        doctor = subprocess.run(
            [sys.executable, str(self.dest / "bin" / "memory"), "doctor", "--json"],
            capture_output=True, text=True, env=self._env(), timeout=180,
        )
        checks = {c["name"]: c for c in json.loads(doctor.stdout)["checks"]}
        self.assertEqual(checks["mcp"]["level"], "info")
        self.assertTrue(checks["mcp"]["detail"].startswith("off;"), msg=checks["mcp"])


@unittest.skipUnless(INSTALL_SH.exists(), "install.sh not adjacent to the source tree")
class TestSharedSettings(_InstallCase):
    FOREIGN_CURSOR_HOOK = {"command": "~/bin/cursor-notify.sh"}
    FOREIGN_CLAUDE_GROUP = {"hooks": [{"type": "command", "command": "afplay done.aiff"}]}
    FOREIGN_MCP = {"command": "npx", "args": ["-y", "someone-elses-memory"]}
    USER_RULE = "# My recall rule\n\nAlways grep the wiki first.\n"
    UNRELATED_SKILL = "# My add-memory\n\nMy own workflow, nothing to do with memory tools.\n"
    USER_ZSHRC = "export EDITOR=vim\nalias ll='ls -l'\n"

    def _seed(self) -> None:
        cursor, claude = self.home / ".cursor", self.home / ".claude"
        (cursor / "rules").mkdir(parents=True)
        (cursor / "hooks.json").write_text(
            json.dumps({"version": 1, "hooks": {"stop": [self.FOREIGN_CURSOR_HOOK]}}), encoding="utf-8"
        )
        (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}), encoding="utf-8")
        (cursor / "rules" / "memory-recall.mdc").write_text(self.USER_RULE, encoding="utf-8")
        (cursor / "skills" / "add-memory").mkdir(parents=True)
        (cursor / "skills" / "add-memory" / "SKILL.md").write_text(self.UNRELATED_SKILL, encoding="utf-8")
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps({"model": "opus", "hooks": {"Stop": [self.FOREIGN_CLAUDE_GROUP]}}), encoding="utf-8"
        )
        (self.home / ".claude.json").write_text(
            json.dumps({"numStartups": 3, "mcpServers": {"silly-memory": self.FOREIGN_MCP}}), encoding="utf-8"
        )
        (self.home / ".zshrc").write_text(self.USER_ZSHRC, encoding="utf-8")

    def _wired(self) -> tuple[Path, ...]:
        return (self.home / ".cursor", self.home / ".claude", self.home / ".claude.json", self.home / ".zshrc")

    def test_owned_parts_merge_rerun_changes_nothing_and_revert_restores_before_images(self) -> None:
        self._seed()
        before = _tree(*self._wired())
        journal = self.tmp / "journal.jsonl"
        env = {"SILLY_MEMORY_UPGRADE_JOURNAL": str(journal)}
        result = self._run("--tools", "cursor,claude-code", **env)
        self._ok(result)
        cursor, claude = self.home / ".cursor", self.home / ".claude"

        hooks = _json(cursor / "hooks.json")
        self.assertIn(self.FOREIGN_CURSOR_HOOK, hooks["hooks"]["stop"])
        self._assert_cursor_hooks(cursor / "hooks.json")
        # Global MCP registries are never written.
        self.assertEqual(_json(cursor / "mcp.json"), {"mcpServers": {"github": {"command": "gh-mcp"}}})

        settings = _json(claude / "settings.json")
        self.assertEqual(settings["model"], "opus")
        self.assertIn(self.FOREIGN_CLAUDE_GROUP, settings["hooks"]["Stop"])
        self._assert_claude_hooks(claude / "settings.json")
        registry = _json(self.home / ".claude.json")
        self.assertEqual(registry, {"numStartups": 3, "mcpServers": {"silly-memory": self.FOREIGN_MCP}})

        # Files that are not ours stay as they are; the rest is installed.
        rule_path = cursor / "rules" / "memory-recall.mdc"
        unrelated = cursor / "skills" / "add-memory" / "SKILL.md"
        self.assertEqual(rule_path.read_text(encoding="utf-8"), self.USER_RULE)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), self.UNRELATED_SKILL)
        for path in (rule_path, unrelated):
            self.assertIn(f"{path} differs from what silly-memory installed", result.stderr)
        skill_path = cursor / "skills" / "query-memory" / "SKILL.md"
        skill = skill_path.read_text(encoding="utf-8")
        self.assertEqual(skill, (PROJECT_ROOT / "cursor-extras" / "skills" / "query-memory" / "SKILL.md").read_text(encoding="utf-8"))
        self._run_new_home_command((claude / "rules" / "memory-recall.md").read_text(encoding="utf-8"), self._project())

        ownership = _json(self.dest / ".installed-artifacts.json")
        self.assertIn(str(skill_path), ownership["files"])
        self.assertNotIn(str(rule_path), ownership["files"])
        self.assertNotIn(str(unrelated), ownership["files"])
        self.assertNotIn(str(self.home / ".claude.json"), ownership["fragments"])
        self.assertNotIn(str(cursor / "mcp.json"), ownership["fragments"])

        zshrc = (self.home / ".zshrc").read_text(encoding="utf-8")
        self.assertEqual(zshrc.count("# >>> silly-memory >>>"), 1)
        self.assertTrue(zshrc.startswith(self.USER_ZSHRC))

        # Every journal entry was applied; a rerun writes (and journals) nothing.
        records = journal.read_text(encoding="utf-8").splitlines()
        intended = [json.loads(r) for r in records if json.loads(r)["phase"] == "intended"]
        applied = {json.loads(r)["id"] for r in records if json.loads(r)["phase"] == "applied"}
        self.assertEqual({e["id"] for e in intended}, applied)
        self.assertIn(str(skill_path), {e["target"] for e in intended})
        after_install = _tree(*self._wired())
        self._ok(self._run("--tools", "cursor,claude-code", **env))
        self.assertEqual(_tree(*self._wired()), after_install)
        self.assertEqual(journal.read_text(encoding="utf-8").splitlines(), records)

        # A hand edit to an owned entry is a third value: revert refuses and writes nothing.
        settings_path = claude / "settings.json"
        edited = _json(settings_path)
        edited["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 99
        settings_path.write_text(json.dumps(edited), encoding="utf-8")
        edited_tree = _tree(*self._wired())
        refused = self._tx("revert-journal", "--journal", str(journal))
        self.assertEqual(refused.returncode, 4, msg=refused.stderr)
        self.assertIn(str(settings_path), refused.stderr)
        self.assertEqual(_tree(*self._wired()), edited_tree)

        edited["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 10
        settings_path.write_text(json.dumps(edited), encoding="utf-8")
        reverted = self._tx("revert-journal", "--journal", str(journal))
        self.assertEqual(reverted.returncode, 0, msg=reverted.stderr)
        restored = _tree(*self._wired())
        self.assertEqual(sorted(restored), sorted(before), msg="files created by the install must be removed")
        shared = {str(p) for p in (cursor / "hooks.json", claude / "settings.json")}
        for path, data in before.items():
            if path in shared:
                self.assertEqual(json.loads(restored[path]), json.loads(data), msg=path)
            else:
                self.assertEqual(restored[path], data, msg=path)

    def test_malformed_or_escaping_shared_config_fails_before_any_write(self) -> None:
        outside = self.tmp / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        cases: list[tuple[str, str, int, Any]] = [
            ("claude-code", ".claude/settings.json", 3, "{not json"),
            ("claude-code", ".claude/settings.json", 3, '{"hooks": []}'),
            ("cursor", ".cursor/hooks.json", 3, '{"hooks": "none"}'),
            ("cursor", ".zshrc", 3, "# >>> silly-memory >>>\nsource x\n"),
            ("cursor", ".zshrc", 3, "echo hi\n# <<< silly-memory <<<\n"),
            ("claude-code", ".claude/settings.json", 65, outside),
        ]
        for tool, rel, code, content in cases:
            with self.subTest(file=rel, content=str(content)):
                shutil.rmtree(self.home)
                self.home.mkdir()
                (self.home / ".zshrc").write_text("", encoding="utf-8")
                target = self.home / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, Path):
                    target.symlink_to(content)
                else:
                    target.write_text(content, encoding="utf-8")
                before = _tree(self.home)
                result = self._run("--tools", tool)
                self.assertEqual(result.returncode, code, msg=result.stderr)
                self.assertIn("nothing was changed", result.stderr)
                self.assertEqual(_tree(self.home), before)
                self.assertFalse(self.dest.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "{}")

    def test_install_stops_at_the_first_write_it_cannot_journal(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"permissions":{"allow":["Bash(ls)"]}}', encoding="utf-8")
        blocker = self.tmp / "blocker"
        blocker.write_text("a file, so the journal directory cannot exist\n", encoding="utf-8")
        before = _tree(self.home / ".claude", self.home / ".zshrc")
        result = self._run("--tools", "claude-code", SILLY_MEMORY_UPGRADE_JOURNAL=str(blocker / "journal.jsonl"))
        self.assertNotEqual(result.returncode, 0, msg=result.stdout)
        self.assertEqual(_tree(self.home / ".claude", self.home / ".zshrc"), before)
        self.assertFalse((self.home / ".claude.json").exists())


@unittest.skipUnless(TX_PY.exists(), "install_transaction.py not beside the tests")
class TestTransactionHelper(_InstallCase):
    def setUp(self) -> None:
        super().setUp()
        self.tx = _load_tx()

    def test_no_write_precedes_its_journal_entry(self) -> None:
        target = self.home / "file.txt"
        source = self.tmp / "source.txt"
        source.write_text("new\n", encoding="utf-8")
        blocker = self.tmp / "blocker"
        blocker.write_text("", encoding="utf-8")
        result = self._tx(
            "put-file", "--target", str(target), "--source", str(source),
            SILLY_MEMORY_UPGRADE_JOURNAL=str(blocker / "journal.jsonl"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())

    def test_crash_between_intent_write_and_applied_reverts_cleanly(self) -> None:
        target = self.home / "rule.md"
        target.write_text("before\n", encoding="utf-8")
        target.chmod(0o640)
        journal_path = self.tmp / "journal.jsonl"
        journal = self.tx.Journal(journal_path)

        # Crash before the write: journaled, not applied, file untouched, nothing to undo.
        with mock.patch.object(self.tx, "atomic_write_bytes", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.tx.put_file(target, b"after\n", journal)
        entries = self.tx.load_journal(journal_path)
        self.assertEqual([(e["target"], e["applied"]) for e in entries], [(str(target), False)])
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
        self.assertEqual(self.tx.revert_entries(entries), [])

        # Crash after the write, before "applied": revert restores bytes and mode.
        with mock.patch.object(self.tx.Journal, "applied", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                self.tx.put_file(target, b"after\n", journal)
        self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
        entries = self.tx.load_journal(journal_path)
        self.assertEqual(self.tx.revert_entries(entries), [str(target)])
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        before_image = entries[-1]["before"]
        self.assertEqual(base64.b64decode(before_image["content_b64"]), b"before\n")

    def test_current_owned_entries_are_left_in_their_place(self) -> None:
        """A hooks.json that already holds the same entries stays as is; rewriting it would only reorder them."""
        template = MEM_ROOT.parent / "cursor-extras" / "hooks.json"
        if not template.is_file():
            self.skipTest("cursor-extras/hooks.json not beside the engine")
        hooks = {event: [{"command": "./hooks/memory-hook.sh"}] for event in sorted(CURSOR_EVENTS)}
        hooks["stop"].append({"command": "~/bin/cursor-notify.sh"})
        target = self.home / ".cursor" / "hooks.json"
        target.parent.mkdir()
        target.write_text(json.dumps({"version": 1, "hooks": hooks}), encoding="utf-8")
        before = target.read_bytes()
        journal = self.tmp / "journal.jsonl"
        result = self._tx(
            "merge", "--kind", "cursor-hooks", "--target", str(target), "--template", str(template),
            SILLY_MEMORY_UPGRADE_JOURNAL=str(journal),
        )
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "unchanged"), msg=result.stderr)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(journal.exists())

    def test_a_target_written_twice_reverts_to_its_first_before_image(self) -> None:
        target = self.home / "rule.md"
        target.write_text("A\n", encoding="utf-8")
        journal_path = self.tmp / "journal.jsonl"
        journal = self.tx.Journal(journal_path)
        self.tx.put_file(target, b"B\n", journal)
        self.tx.put_file(target, b"C\n", journal)
        self.assertEqual(self.tx.revert_entries(self.tx.load_journal(journal_path)), [str(target)])
        self.assertEqual(target.read_text(encoding="utf-8"), "A\n")

    def test_a_created_file_is_removed_and_a_torn_journal_line_is_ignored(self) -> None:
        target = self.home / "new" / "shim.sh"
        journal_path = self.tmp / "journal.jsonl"
        self.tx.put_file(target, b"#!/bin/sh\n", self.tx.Journal(journal_path), 0o755)
        with journal_path.open("a", encoding="utf-8") as handle:
            handle.write('{"id": 2, "phase": "inten')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
        result = self._tx("revert-journal", "--journal", str(journal_path))
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertFalse(target.exists())
        # Reverting again finds everything at its before-image.
        again = self._tx("revert-journal", "--journal", str(journal_path))
        self.assertEqual((again.returncode, again.stdout.strip()), (0, "nothing to restore"))

    def test_shared_settings_behind_a_dotfile_link_are_written_through(self) -> None:
        dotfiles = self.home / "dotfiles" / "claude-settings.json"
        dotfiles.parent.mkdir()
        dotfiles.write_text('{"model": "opus"}', encoding="utf-8")
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.symlink_to(dotfiles)
        group = {"hooks": [{"type": "command", "command": "~/.claude/hooks/silly-memory-hook.sh", "timeout": 10}]}
        template = self.tmp / "hooks.json"
        template.write_text(json.dumps({"hooks": {"SessionStart": [group]}}), encoding="utf-8")
        result = self._tx(
            "merge", "--kind", "claude-hooks", "--target", str(settings), "--template", str(template),
            "--within", str(self.home),
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertTrue(settings.is_symlink())
        self.assertEqual(_json(dotfiles), {"model": "opus", "hooks": {"SessionStart": [group]}})

        # An owned file may not resolve outside its tool folder.
        outside = self.tmp / "outside.sh"
        outside.write_text("mine\n", encoding="utf-8")
        shim = self.home / ".claude" / "hooks" / "silly-memory-hook.sh"
        shim.parent.mkdir()
        shim.symlink_to(outside)
        escaped = self._tx(
            "put-file", "--target", str(shim), "--source", str(template), "--within", str(self.home / ".claude")
        )
        self.assertEqual(escaped.returncode, 65, msg=escaped.stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "mine\n")

    def test_an_artifact_is_updated_only_while_it_matches_what_was_installed(self) -> None:
        ownership = self.tmp / "ownership.json"
        target = self.home / ".cursor" / "rules" / "memory-recall.mdc"
        first, second = self.tmp / "first.mdc", self.tmp / "second.mdc"
        first.write_text("release one\n", encoding="utf-8")
        second.write_text("release two\n", encoding="utf-8")

        def put(source: Path) -> subprocess.CompletedProcess[str]:
            return self._tx(
                "put-artifact", "--target", str(target), "--source", str(source), "--kind", "rule",
                "--ownership", str(ownership), "--tool", "cursor",
            )

        self.assertEqual(put(first).stdout.strip(), "installed")
        self.assertEqual(put(second).stdout.strip(), "updated")
        self.assertEqual(target.read_text(encoding="utf-8"), "release two\n")
        target.write_text("release two\nmy own note\n", encoding="utf-8")
        kept = put(first)
        self.assertEqual((kept.returncode, kept.stdout.strip()), (0, "kept"), msg=kept.stderr)
        self.assertIn("differs from what silly-memory installed", kept.stderr)
        self.assertEqual(target.read_text(encoding="utf-8"), "release two\nmy own note\n")

    def test_zsh_block_replaces_an_older_block_once_and_malformed_fences_fail(self) -> None:
        rc = self.home / ".zshrc"
        original = (
            "export EDITOR=vim\n\n"
            "# >>> silly-memory >>>\n"
            'export SILLY_MEMORY_HOME="/Users/me/elsewhere"\n'
            '[ -f "/Users/me/elsewhere/memory.zsh" ] && source "/Users/me/elsewhere/memory.zsh"\n'
            "# <<< silly-memory <<<\n"
            "alias ll='ls -l'\n"
        )
        rc.write_text(original, encoding="utf-8")

        def install_zsh(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", str(INSTALL_ZSH), *args], capture_output=True, text=True, env=self._env(**env), timeout=60
            )

        dry = install_zsh("--dry-run")
        self.assertEqual(dry.returncode, 0, msg=dry.stderr)
        self.assertEqual(rc.read_text(encoding="utf-8"), original)

        journal = self.tmp / "journal.jsonl"
        self.assertEqual(install_zsh(SILLY_MEMORY_UPGRADE_JOURNAL=str(journal)).returncode, 0)
        expected = (
            "export EDITOR=vim\n\nalias ll='ls -l'\n\n"
            "# >>> silly-memory >>>\n"
            '[ -f "$HOME/.silly-memory/memory.zsh" ] && source "$HOME/.silly-memory/memory.zsh"\n'
            "# <<< silly-memory <<<\n"
        )
        self.assertEqual(rc.read_text(encoding="utf-8"), expected)
        self.assertEqual(install_zsh().returncode, 0)
        self.assertEqual(rc.read_text(encoding="utf-8"), expected)

        # Revert brings back exactly the older blocks; user lines stay.
        self.assertEqual(self._tx("revert-journal", "--journal", str(journal)).returncode, 0)
        restored = rc.read_text(encoding="utf-8")
        self.assertEqual(self.tx.zsh_owned_blocks(restored), self.tx.zsh_owned_blocks(original))
        self.assertEqual(self.tx.zsh_without_blocks(restored), self.tx.zsh_without_blocks(original))

        self.assertEqual(install_zsh("--remove").returncode, 0)
        self.assertEqual(rc.read_text(encoding="utf-8"), "export EDITOR=vim\n\nalias ll='ls -l'\n")

        for broken in ("# >>> silly-memory >>>\nsource x\n", "echo hi\n# <<< silly-memory <<<\n"):
            with self.subTest(rc=broken):
                rc.write_text(broken, encoding="utf-8")
                result = install_zsh()
                self.assertEqual(result.returncode, 3, msg=result.stderr)
                self.assertEqual(rc.read_text(encoding="utf-8"), broken)

    def _locked(self, lock: Path, timeout: float, *command: str) -> list[str]:
        return [sys.executable, str(TX_PY), "run-locked", "--lock", str(lock), "--timeout", str(timeout), "--", *command]

    def test_upgrade_lock_is_released_when_every_holder_is_killed(self) -> None:
        lock = self.tmp / "silly-memory-upgrade.lock"
        ready = self.tmp / "ready"
        owner = subprocess.Popen(
            self._locked(lock, 5, "/bin/sh", "-c", f'touch "{ready}"; exec sleep 60'), start_new_session=True
        )
        try:
            deadline = time.monotonic() + 20
            while not ready.exists():
                self.assertLess(time.monotonic(), deadline, msg="lock owner never started its child")
                time.sleep(0.05)
            busy = subprocess.run(self._locked(lock, 0.3, "/usr/bin/true"), capture_output=True, text=True, timeout=30)
            self.assertEqual(busy.returncode, 73, msg=busy.stderr)

            # The supervisor dies; its child still holds the inherited descriptor.
            os.kill(owner.pid, signal.SIGKILL)
            owner.wait(timeout=10)
            still = subprocess.run(self._locked(lock, 0.3, "/usr/bin/true"), capture_output=True, text=True, timeout=30)
            self.assertEqual(still.returncode, 73, msg=still.stderr)
        finally:
            try:
                os.killpg(owner.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            owner.wait(timeout=10)

        fresh = subprocess.run(
            self._locked(lock, 10, "/bin/sh", "-c", 'exit 7'), capture_output=True, text=True, timeout=30
        )
        self.assertEqual(fresh.returncode, 7, msg=fresh.stderr)
        self.assertTrue(lock.is_file())
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

        shown = subprocess.run(
            self._locked(lock, 5, "/bin/sh", "-c", 'echo "$SILLY_MEMORY_UPGRADE_LOCK_FD"'),
            capture_output=True, text=True, timeout=30,
        )
        self.assertTrue(shown.stdout.strip().isdigit(), msg=shown.stdout)

    def test_a_symlinked_lock_file_is_refused(self) -> None:
        target = self.tmp / "precious"
        target.write_text("keep\n", encoding="utf-8")
        lock = self.tmp / "silly-memory-upgrade.lock"
        lock.symlink_to(target)
        result = subprocess.run(self._locked(lock, 1, "/usr/bin/true"), capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
