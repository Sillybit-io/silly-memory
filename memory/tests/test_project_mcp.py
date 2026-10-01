"""The silly-memory MCP server registered in each project's own tool configuration.

Guards:
  - With MCP off (the default), session start writes no MCP file at all.
  - With it on, session start writes ``.mcp.json`` (Claude Code, pre-approved
    in the project's ``.claude/settings.local.json``), ``.cursor/mcp.json``, and
    ``opencode.json`` for the wired tools, and nothing global.
  - The entries hold no machine path, so a team can commit them; the launcher
    finds the memory home at start (SILLY_MEMORY_HOME, else ~/.silly-memory).
  - Other keys and servers stay, reruns change nothing, a foreign server named
    silly-memory is never touched (nor approved), and malformed files and
    ``opencode.jsonc`` projects are left alone.
  - Removal takes out only our entry and approval, deleting a file that held
    nothing else.

Isolation: throwaway HOME, memory home, and projects.
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
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system import project_mcp  # noqa: E402

ALL = ["claude-code", "cursor", "opencode"]
_ENV_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN")


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ProjectMcpBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_project_mcp_")).resolve()
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        self.project = self.tmp / "project"
        (self.project / ".git").mkdir(parents=True)

    def tearDown(self) -> None:
        for key in _ENV_VARS:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in self._saved.items() if v is not None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def files(self) -> dict[str, Path]:
        return project_mcp.project_mcp_files(self.project)


class TestEnsure(ProjectMcpBase):
    def test_each_tool_gets_a_portable_entry_and_claude_is_pre_approved(self) -> None:
        self.assertEqual(project_mcp.ensure_project_mcp(self.project, ALL), [])
        files = self.files()
        self.assertEqual(_json(files["claude-code"])["mcpServers"]["silly-memory"], project_mcp.server_entry("claude-code"))
        self.assertEqual(_json(files["cursor"])["mcpServers"]["silly-memory"], project_mcp.server_entry("cursor"))
        opencode = _json(files["opencode"])
        self.assertEqual(opencode["mcp"]["silly-memory"], project_mcp.server_entry("opencode"))
        self.assertEqual(opencode["$schema"], project_mcp.OPENCODE_SCHEMA)
        self.assertEqual(_json(self.project / ".claude" / "settings.local.json"), {"enabledMcpjsonServers": ["silly-memory"]})
        for path in files.values():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(str(Path.home()), text, "a committed file must not name this machine's home")
            self.assertNotIn("${", text, "Claude Code and Cursor would expand it themselves")
        self.assertEqual(project_mcp.registered_tools(self.project), set(ALL))

    def test_only_the_wired_tools_get_a_file(self) -> None:
        project_mcp.ensure_project_mcp(self.project, ["opencode"])
        self.assertEqual([t for t, p in self.files().items() if p.exists()], ["opencode"])
        self.assertFalse((self.project / ".claude").exists())

    def test_other_entries_stay_and_a_rerun_changes_nothing(self) -> None:
        mcp = self.project / ".mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}), encoding="utf-8")
        approvals = self.project / ".claude" / "settings.local.json"
        approvals.parent.mkdir()
        approvals.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "enabledMcpjsonServers": ["github"]}), encoding="utf-8")
        opencode = self.project / "opencode.json"
        opencode.write_text(json.dumps({"model": "anthropic/claude-sonnet-5", "mcp": {"docs": {"type": "remote", "url": "https://x.invalid"}}}), encoding="utf-8")
        project_mcp.ensure_project_mcp(self.project, ALL)
        self.assertEqual(_json(mcp)["mcpServers"]["github"], {"command": "gh-mcp"})
        self.assertEqual(_json(approvals), {"permissions": {"allow": ["Bash(ls)"]}, "enabledMcpjsonServers": ["github", "silly-memory"]})
        self.assertEqual(_json(opencode)["model"], "anthropic/claude-sonnet-5")
        self.assertNotIn("$schema", _json(opencode), "an existing opencode.json keeps its own shape")
        before = {p: p.read_bytes() for p in [*self.files().values(), approvals]}
        project_mcp.ensure_project_mcp(self.project, ALL)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_a_foreign_server_named_silly_memory_is_kept_and_not_approved(self) -> None:
        foreign = {"command": "npx", "args": ["-y", "someone-elses-memory"]}
        mcp = self.project / ".mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"silly-memory": foreign}}), encoding="utf-8")
        skipped = project_mcp.ensure_project_mcp(self.project, ["claude-code"])
        self.assertEqual(_json(mcp)["mcpServers"]["silly-memory"], foreign)
        self.assertTrue(any("unrelated server" in s for s in skipped), skipped)
        self.assertFalse((self.project / ".claude" / "settings.local.json").exists())

    def test_malformed_files_and_jsonc_projects_are_left_alone(self) -> None:
        (self.project / ".cursor").mkdir()
        (self.project / ".cursor" / "mcp.json").write_text("{not json", encoding="utf-8")
        (self.project / "opencode.jsonc").write_text('{\n  // team config\n  "model": "x"\n}\n', encoding="utf-8")
        skipped = project_mcp.ensure_project_mcp(self.project, ["cursor", "opencode"])
        self.assertEqual((self.project / ".cursor" / "mcp.json").read_text(encoding="utf-8"), "{not json")
        self.assertFalse((self.project / "opencode.json").exists())
        self.assertEqual(len(skipped), 2, skipped)


class TestRemove(ProjectMcpBase):
    def test_only_our_entry_and_approval_go(self) -> None:
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}), encoding="utf-8")
        project_mcp.ensure_project_mcp(self.project, ALL)
        changed = project_mcp.remove_project_mcp(self.project)
        self.assertEqual(len(changed), 4)
        self.assertEqual(_json(self.project / ".mcp.json"), {"mcpServers": {"github": {"command": "gh-mcp"}}})
        for path in (self.project / ".cursor" / "mcp.json", self.project / "opencode.json", self.project / ".claude" / "settings.local.json"):
            self.assertFalse(path.exists(), f"{path} held nothing but our entry")
        self.assertEqual(project_mcp.remove_project_mcp(self.project), [])

    def test_a_foreign_same_name_server_is_never_removed(self) -> None:
        foreign = {"type": "local", "command": ["npx", "other-memory"]}
        (self.project / "opencode.json").write_text(json.dumps({"mcp": {"silly-memory": foreign}}), encoding="utf-8")
        self.assertEqual(project_mcp.remove_project_mcp(self.project), [])
        self.assertEqual(_json(self.project / "opencode.json")["mcp"]["silly-memory"], foreign)


class TestLauncher(ProjectMcpBase):
    """The committed command starts the engine of whoever runs it."""

    def _launch(self, tool: str, **env: str) -> str:
        entry = project_mcp.server_entry(tool)
        argv = entry["command"] if tool == "opencode" else [entry["command"], *entry["args"]]
        base = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(self.tmp)}
        proc = subprocess.run(argv, capture_output=True, text=True, env={**base, **env}, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _engine(self, home: Path) -> None:
        (home / "bin").mkdir(parents=True)
        (home / "bin" / "memory-mcp").write_text(f"import sys; print('{home.name}', *sys.argv[1:])\n", encoding="utf-8")

    def test_the_home_is_found_at_start(self) -> None:
        for name in ("custom", ".silly-memory"):
            self._engine(self.tmp / name)
        self.assertEqual(self._launch("claude-code", SILLY_MEMORY_HOME=str(self.tmp / "custom")), "custom --client claude-code")
        self.assertEqual(self._launch("cursor"), ".silly-memory --client cursor")
        self.assertEqual(self._launch("opencode"), ".silly-memory --client opencode")


class TestSessionStart(ProjectMcpBase):
    def _session_start(self, **config_values: object) -> subprocess.CompletedProcess[str]:
        home = self.tmp / "memory-home"
        home.mkdir()
        config = _json(MEM_HOME / "config.json")
        config.update(tools=["claude-code", "opencode"], **config_values)
        (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
        payload = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(self.project), "source": "startup"}
        env = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        env.update(HOME=str(self.tmp), SILLY_MEMORY_HOME=str(home), MEMORY_EMBEDDING_BACKEND="noop", PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run(
            [sys.executable, str(CLI), "hook", "--tool", "claude-code"],
            input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["hookSpecificOutput"]["hookEventName"], "SessionStart")
        return proc

    def test_by_default_no_project_file_names_the_server(self) -> None:
        """MCP is off unless installed with --mcp: nothing to approve or commit."""
        self._session_start()
        self.assertTrue((self.project / ".silly-memory" / "memory-id").is_file())
        for path in [*self.files().values(), self.project / ".claude" / "settings.local.json"]:
            self.assertFalse(path.exists(), f"{path} was written with MCP off")

    def test_the_first_session_writes_the_wired_tools_files(self) -> None:
        self._session_start(mcp=True)
        self.assertEqual(project_mcp.registered_tools(self.project), {"claude-code", "opencode"})
        self.assertTrue((self.project / ".claude" / "settings.local.json").is_file())
        self.assertFalse((self.tmp / ".claude.json").exists())
        self.assertIn("settings.local.json", (self.project / ".claude" / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
