"""``memory doctor`` checks the wiring of each configured tool.

Guards:
  - Only tools listed under ``tools`` in config.json are checked; a listed
    tool that is not wired is an error (Cursor hooks, Claude Code hooks in
    ``~/.claude/settings.json``, the OpenCode plugin), as is an empty list.
  - Each wired tool reports when it last started a session.
  - MCP off (the default) is info and names projects that still list the
    server; with MCP on, a missing launcher is an error and registrations are
    counted per project.
  - Home, VERSION, locks, and backups are read from the effective home,
    including a custom one.
  - The doctor never runs ``claude`` or ``opencode`` and its recall probe
    leaves the environment as it found it.

Isolation: every test uses a throwaway HOME and memory home.
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

MEM_HOME = Path(__file__).resolve().parents[1]
MEM_LIB = MEM_HOME / "lib"
CLI = MEM_HOME / "bin" / "memory"
REPO = MEM_HOME.parent
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system.cli import doctor  # noqa: E402
from memory_system.project_mcp import ensure_project_mcp  # noqa: E402
from memory_system.system.version import get_code_version  # noqa: E402

_ENV_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN", "XDG_CONFIG_HOME")


class DoctorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_doctor_")).resolve()
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.store_home = self.tmp / "stores"
        self.make_home(self.store_home, ["claude-code"])

    def tearDown(self) -> None:
        for key in _ENV_VARS:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in self._saved.items() if v is not None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_home(self, home: Path, tools: list[str], *, mcp: bool = False) -> None:
        (home / "bin").mkdir(parents=True, exist_ok=True)
        home.chmod(0o700)
        (home / "VERSION").write_text(get_code_version() + "\n", encoding="utf-8")
        (home / "bin" / "memory-mcp").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        self.set_tools(tools, home, mcp=mcp)

    def set_tools(self, tools: object, home: Path | None = None, *, mcp: bool = False) -> None:
        config = json.loads((MEM_HOME / "config.json").read_text(encoding="utf-8"))
        config["tools"] = tools
        config["mcp"] = mcp
        ((home or self.store_home) / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def wire_claude(self) -> None:
        claude = self.home / ".claude"
        (claude / "hooks").mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / "claude-code-extras" / "hooks" / "silly-memory-hook.sh", claude / "hooks")
        template = json.loads((REPO / "claude-code-extras" / "hooks.json").read_text(encoding="utf-8"))
        (claude / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, **template}), encoding="utf-8")

    def wire_cursor(self) -> None:
        cursor = self.home / ".cursor"
        (cursor / "hooks").mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / "cursor-extras" / "hooks" / "memory-hook.sh", cursor / "hooks")
        shutil.copy2(REPO / "cursor-extras" / "hooks.json", cursor / "hooks.json")

    def track_project(self, name: str) -> Path:
        """A project folder with a store in the memory home that points at it."""
        root = self.tmp / "projects" / name
        root.mkdir(parents=True)
        store = self.store_home / f"ws-{name}"
        store.mkdir()
        (store / ".meta.json").write_text(json.dumps({"workspace_id": store.name, "workspace_root": str(root)}), encoding="utf-8")
        return root

    def wire_opencode(self, config_home: Path | None = None) -> None:
        plugins = (config_home or self.home / ".config") / "opencode" / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / "opencode-extras" / "plugins" / "silly-memory.js", plugins)

    def doctor(self, **env: str) -> tuple[int, dict[str, dict]]:
        base = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        base.update({"HOME": str(self.home), "MEMORY_EMBEDDING_BACKEND": "noop", "MEMORY_ALLOW_NETWORK": "0", "PYTHONDONTWRITEBYTECODE": "1"})
        if "DEFAULT_HOME" not in env:
            base["SILLY_MEMORY_HOME"] = str(self.store_home)
        env.pop("DEFAULT_HOME", None)
        proc = subprocess.run(
            [sys.executable, str(CLI), "doctor", "--json"], env={**base, **env}, capture_output=True, text=True, timeout=120
        )
        report = json.loads(proc.stdout)
        return proc.returncode, {check["name"]: check for check in report["checks"]}

    def assert_level(self, checks: dict[str, dict], name: str, level: str) -> str:
        self.assertEqual(checks[name]["level"], level, checks[name])
        return checks[name]["detail"]


class TestToolWiring(DoctorTestBase):
    def test_claude_only_wiring_is_healthy(self) -> None:
        self.wire_claude()
        code, checks = self.doctor()
        problems = {name: c for name, c in checks.items() if c["level"] != "info"}
        self.assertEqual((code, problems), (0, {}))
        detail = checks["tools"]["detail"]
        self.assertIn("claude-code wired", detail)
        self.assertNotIn("cursor", detail)
        proc = subprocess.run(
            [sys.executable, str(CLI), "doctor"],
            env={**{k: v for k, v in os.environ.items() if k not in _ENV_VARS}, "HOME": str(self.home), "SILLY_MEMORY_HOME": str(self.store_home), "MEMORY_EMBEDDING_BACKEND": "noop"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("silly-memory — doctor", proc.stdout)
        self.assertRegex(proc.stdout, r"tools .*claude-code wired")

    def test_last_session_start_is_reported_per_tool(self) -> None:
        self.wire_claude()
        self.wire_opencode()
        self.set_tools(["claude-code", "opencode"])
        store = self.store_home / "0123456789abcdef"
        store.mkdir()
        rows = [
            {"source": "claude-code", "conversation_id": "a", "started": "2026-09-20T10:00:00+00:00"},
            {"source": "claude-code", "conversation_id": "b", "started": "2026-09-28T09:30:00+00:00"},
        ]
        (store / "sessions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows) + "{broken\n", encoding="utf-8")
        _, checks = self.doctor()
        detail = self.assert_level(checks, "tools", "info")
        self.assertIn("claude-code wired (last session start: 2026-09-28T09:30:00)", detail)
        self.assertIn("opencode wired (last session start: none recorded)", detail)

    def test_missing_opencode_plugin_is_an_error(self) -> None:
        self.set_tools(["opencode"])
        code, checks = self.doctor()
        self.assertEqual(code, 2)
        self.assertIn("opencode: plugin missing", self.assert_level(checks, "tools", "error"))
        self.wire_opencode(self.tmp / "xdg")
        code, checks = self.doctor(XDG_CONFIG_HOME=str(self.tmp / "xdg"))
        self.assertIn("opencode wired", self.assert_level(checks, "tools", "info"))

    def test_selected_cursor_without_hooks_is_an_error(self) -> None:
        self.set_tools(["cursor"])
        code, checks = self.doctor()
        self.assertEqual(code, 2)
        self.assertIn("hooks.json", self.assert_level(checks, "tools", "error"))
        self.wire_cursor()
        hooks = json.loads((self.home / ".cursor" / "hooks.json").read_text(encoding="utf-8"))
        hooks["hooks"]["stop"] = [{"command": "./hooks/someone-else.sh"}]
        (self.home / ".cursor" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
        _, checks = self.doctor()
        self.assertIn("not pointing at memory-hook.sh: stop", self.assert_level(checks, "tools", "error"))

    def test_claude_hooks_missing_an_event_or_shim_is_an_error(self) -> None:
        self.wire_claude()
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        del settings["hooks"]["SessionEnd"]
        (self.home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        _, checks = self.doctor()
        self.assertIn("SessionEnd", self.assert_level(checks, "tools", "error"))
        self.wire_claude()
        (self.home / ".claude" / "hooks" / "silly-memory-hook.sh").unlink()
        _, checks = self.doctor()
        self.assertIn("hook shim missing", self.assert_level(checks, "tools", "error"))
        (self.home / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        _, checks = self.doctor()
        self.assertIn("invalid", self.assert_level(checks, "tools", "error"))

    def test_empty_or_unknown_tool_lists_are_errors(self) -> None:
        for tools, expected in (([], "no tools listed"), ("cursor", "no tools listed"), (["cursor-cli"], "unknown tool")):
            with self.subTest(tools=tools):
                self.set_tools(tools)
                code, checks = self.doctor()
                self.assertEqual(code, 2)
                self.assertIn(expected, self.assert_level(checks, "tools", "error"))


class TestMcpWiring(DoctorTestBase):
    def test_registration_is_counted_per_project(self) -> None:
        """Nothing is registered globally; each project's own config carries the server."""
        self.wire_claude()
        self.wire_cursor()
        self.wire_opencode()
        self.set_tools(["cursor", "claude-code", "opencode"], mcp=True)
        alpha, beta = self.track_project("alpha"), self.track_project("beta")
        ensure_project_mcp(alpha, ["cursor", "claude-code", "opencode"])
        (beta / ".mcp.json").write_text(json.dumps({"mcpServers": {"silly-memory": {"command": "npx", "args": ["other"]}}}), encoding="utf-8")
        _, checks = self.doctor()
        detail = self.assert_level(checks, "mcp", "info")
        self.assertIn("cursor 1/2, claude-code 1/2, opencode 1/2", detail)
        self.assertFalse((self.home / ".claude.json").exists())
        self.assertFalse((self.home / ".cursor" / "mcp.json").exists())

    def test_missing_launcher_is_an_error(self) -> None:
        self.wire_claude()
        self.set_tools(["claude-code"], mcp=True)
        (self.store_home / "bin" / "memory-mcp").unlink()
        code, checks = self.doctor()
        self.assertEqual(code, 2)
        self.assertIn("launcher missing", self.assert_level(checks, "mcp", "error"))

    def test_off_is_info_and_names_projects_that_still_list_the_server(self) -> None:
        """Off is the default: the skills cover the same operations, so nothing is missing."""
        self.wire_claude()
        (self.store_home / "bin" / "memory-mcp").unlink()
        alpha, _ = self.track_project("alpha"), self.track_project("beta")
        code, checks = self.doctor()
        detail = self.assert_level(checks, "mcp", "info")
        self.assertTrue(detail.startswith("off;"), detail)
        self.assertIn("--mcp turns it on", detail)
        self.assertNotIn("still list", detail)
        self.assertNotEqual(code, 2)
        ensure_project_mcp(alpha, ["claude-code"])
        _, checks = self.doctor()
        self.assertIn("1 project(s) still list the server", self.assert_level(checks, "mcp", "info"))


class TestHome(DoctorTestBase):
    def test_custom_home_is_the_one_checked(self) -> None:
        self.wire_claude()
        custom = self.tmp / "elsewhere" / "memory"
        self.make_home(custom, ["claude-code"], mcp=True)
        (custom / ".install.lock").mkdir()
        os.utime(custom / ".install.lock", (1, 1))
        for n in range(6):
            (custom.parent / f"memory.upgrade-backup-{n}-x").mkdir()
        code, checks = self.doctor(SILLY_MEMORY_HOME=str(custom))
        self.assertIn(str(custom), self.assert_level(checks, "home_writable", "info"))
        self.assertIn("VERSION matches", self.assert_level(checks, "version_marker", "info"))
        self.assertIn(".install.lock", self.assert_level(checks, "stale_locks", "warn"))
        self.assertIn("6 upgrade backups", self.assert_level(checks, "backup_retention", "warn"))
        self.assertIn(str(custom / "bin" / "memory-mcp"), self.assert_level(checks, "mcp", "info"))

    def test_default_home_is_checked_and_its_backups_counted(self) -> None:
        self.wire_claude()
        neutral = self.home / ".silly-memory"
        self.make_home(neutral, ["claude-code"])
        for n in range(6):
            (self.home / f".silly-memory.upgrade-backup-{n}-x").mkdir()
        code, checks = self.doctor(DEFAULT_HOME="1")
        self.assertIn(str(neutral), self.assert_level(checks, "home_writable", "info"))
        self.assertIn("6 upgrade backups", self.assert_level(checks, "backup_retention", "warn"))
        self.assertEqual(code, 1)

    def test_recall_probe_restores_the_environment_and_leaves_the_store_alone(self) -> None:
        os.environ["SILLY_MEMORY_HOME"] = str(self.store_home)
        before = sorted(p.relative_to(self.store_home) for p in self.store_home.rglob("*"))
        level, _ = doctor._check_recall_p95()
        self.assertIn(level, ("info", "warn"))
        self.assertEqual(os.environ["SILLY_MEMORY_HOME"], str(self.store_home))
        self.assertEqual(sorted(p.relative_to(self.store_home) for p in self.store_home.rglob("*")), before)

    def test_doctor_never_runs_the_tools(self) -> None:
        source = Path(doctor.__file__).read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("os.system", source)


if __name__ == "__main__":
    unittest.main()
