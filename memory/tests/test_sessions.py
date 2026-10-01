"""The "Recent sessions" timeline and cross-tool freshness of the memory pack.

Guards:
  - Session boundaries are recorded per ``(source, conversation_id)`` with the
    first prompt sanitized and cut to 120 characters; repeated deliveries keep
    the first start and prompt; events without an id are not recorded.
  - Retention stays at the 200 most recently active conversations on every
    write, and corrupt lines are skipped.
  - The active pack lists the newest five sessions, newest first, with edit
    and command counts, after "Current work state" and before "Recent
    observations", and still fits the token cap.
  - A fact told to one tool reaches both project rules, the stored pack, and
    the timeline at that tool's processing boundary, before the next tool
    starts, without ``memory render``. OpenCode ends a turn through its
    plugin's execution-end handling with the real engine.

Isolation: every test uses a throwaway HOME, memory home, and projects.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

MEM_HOME = Path(__file__).resolve().parents[1]
MEM_LIB = MEM_HOME / "lib"
CLI = MEM_HOME / "bin" / "memory"
PLUGIN = MEM_HOME.parent / "opencode-extras" / "plugins" / "silly-memory.js"
NODE = shutil.which("node")
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system.events import append_event  # noqa: E402
from memory_system.paths import workspace_store  # noqa: E402
from memory_system.recall import sessions  # noqa: E402
from memory_system.recall.context_pack import render_rule_file  # noqa: E402
from memory_system.safety import file_lock  # noqa: E402

_ENV_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN", "MEMORY_EMBEDDING_BACKEND", "CLAUDE_PROJECT_DIR")
FACT = "the deploy target is zephyr-stage"
CURSOR_RULE = Path(".cursor") / "rules" / "_memory-context.mdc"
CLAUDE_RULE = Path(".claude") / "rules" / "_memory-context.md"
BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _ts(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


def _plugin_harness() -> str:
    spec = importlib.util.spec_from_file_location("_silly_opencode_plugin_tests", Path(__file__).with_name("test_opencode_plugin.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._HARNESS)


class SessionsTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_sessions_")).resolve()
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        self.stores = self.tmp / "stores"
        self.stores.mkdir()
        self.configure(["cursor"])
        os.environ["SILLY_MEMORY_HOME"] = str(self.stores)
        os.environ["MEMORY_EMBEDDING_BACKEND"] = "noop"
        self.project = self.new_project("project")

    def tearDown(self) -> None:
        for key in _ENV_VARS:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in self._saved.items() if v is not None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def configure(self, tools: list[str], **extra: object) -> None:
        config = json.loads((MEM_HOME / "config.json").read_text(encoding="utf-8"))
        config.update({"tools": tools, **extra})
        (self.stores / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def new_project(self, name: str) -> Path:
        project = self.tmp / name
        (project / ".git").mkdir(parents=True)
        return project

    def store(self, project: Path | None = None) -> Path:
        return workspace_store(project or self.project)

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        env.update(
            {
                "HOME": str(self.tmp / "user-home"),
                "SILLY_MEMORY_HOME": str(self.stores),
                "MEMORY_EMBEDDING_BACKEND": "noop",
                "MEMORY_ALLOW_NETWORK": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return env

    def hook(self, payload: dict, tool: str | None = None) -> dict:
        flags = ["--tool", tool] if tool else []
        proc = subprocess.run(
            [sys.executable, str(CLI), "hook", *flags], input=json.dumps(payload), env=self.env(), capture_output=True, text=True, timeout=120
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def cursor(self, hook: str, project: Path, conversation: str = "c1", **fields: object) -> dict:
        return self.hook({"hook_event_name": hook, "workspace_roots": [str(project)], "conversation_id": conversation, **fields})

    def claude(self, hook: str, project: Path, session: str = "s1", **fields: object) -> dict:
        return self.hook({"hook_event_name": hook, "session_id": session, "cwd": str(project), **fields})

    def record(self, hook: str, conversation: str | None, minutes: float, source: str = "cursor", prompt: str | None = None) -> None:
        sessions.record_session_event(self.store(), hook, conversation, source, _ts(minutes), prompt=prompt)

    def artifacts(self, project: Path) -> dict[str, str]:
        return {
            "cursor rule": (project / CURSOR_RULE).read_text(encoding="utf-8"),
            "claude rule": (project / CLAUDE_RULE).read_text(encoding="utf-8"),
            "stored pack": (self.store(project) / "context-pack.md").read_text(encoding="utf-8"),
        }

    @staticmethod
    def timeline(text: str) -> str:
        """The bullet lines of the pack's "Recent sessions" section."""
        match = re.search(r"^## Recent sessions\n(.*?)(?=^## |\Z)", text, re.M | re.S)
        section = match.group(1) if match else ""
        return "\n".join(line for line in section.splitlines() if line.startswith("- "))


class TestRecording(SessionsTestBase):
    def test_session_boundaries_are_recorded_with_source(self) -> None:
        long_prompt = "fix the parser <private>hunter2</private> " + "and more detail " * 20
        self.record("sessionStart", "c1", 0, "claude-code")
        self.record("beforeSubmitPrompt", "c1", 1, "claude-code", prompt=long_prompt)
        self.record("sessionStart", "c1", 2, "claude-code")
        self.record("beforeSubmitPrompt", "c1", 3, "claude-code", prompt="a later prompt")
        self.record("afterFileEdit", "c1", 4, "claude-code")
        self.record("stop", "c1", 5, "claude-code")
        self.record("sessionEnd", "c1", 12, "claude-code")
        (entry,) = sessions.read_sessions(self.store())
        self.assertEqual((entry["source"], entry["conversation_id"]), ("claude-code", "c1"))
        self.assertEqual((entry["started"], entry["ended"], entry["last_activity"]), (_ts(0), _ts(12), _ts(12)))
        self.assertTrue(entry["first_prompt"].startswith("fix the parser [private] and more detail"))
        self.assertLessEqual(len(entry["first_prompt"]), 120)
        self.assertNotIn("hunter2", (self.store() / "sessions.jsonl").read_text(encoding="utf-8"))

    def test_same_id_from_two_tools_stays_separate_and_missing_ids_are_skipped(self) -> None:
        self.record("sessionStart", "shared", 0, "cursor")
        self.record("sessionStart", "shared", 1, "opencode")
        self.record("sessionStart", None, 2, "cursor")
        self.record("sessionStart", "", 3, "cursor")
        found = sorted((s["source"], s["conversation_id"]) for s in sessions.read_sessions(self.store(), 10))
        self.assertEqual(found, [("cursor", "shared"), ("opencode", "shared")])

    def test_append_event_records_the_sanitized_hook(self) -> None:
        payload = {"workspace_roots": [str(self.project)], "conversation_id": "c9", "source": "cursor"}
        append_event(self.project, "sessionStart", {**payload, "hook_event_name": "sessionStart"})
        append_event(self.project, "beforeSubmitPrompt", {**payload, "hook_event_name": "beforeSubmitPrompt", "prompt": "token=abc123 deploy"})
        (entry,) = sessions.read_sessions(self.store())
        self.assertEqual(entry["first_prompt"], "token=[REDACTED] deploy")

    def test_prune_keeps_last_200_conversations(self) -> None:
        for n in range(205):
            self.record("sessionStart", f"c{n:03d}", n)
        kept = {s["conversation_id"] for s in sessions.read_sessions(self.store(), 1000)}
        self.assertEqual(len(kept), 200)
        self.assertNotIn("c004", kept)
        self.assertIn("c005", kept)
        # A stop keeps a long-running conversation (OpenCode never sends sessionEnd) inside the window.
        self.record("stop", "c005", 1000)
        self.record("sessionStart", "c999", 1001)
        kept = {s["conversation_id"] for s in sessions.read_sessions(self.store(), 1000)}
        self.assertEqual(len(kept), 200)
        self.assertIn("c005", kept)
        self.assertNotIn("c006", kept)
        self.assertEqual(len((self.store() / "sessions.jsonl").read_text(encoding="utf-8").splitlines()), 200)

    def test_corrupt_session_line_is_skipped(self) -> None:
        self.record("sessionStart", "good", 0)
        path = self.store() / "sessions.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{not json\n["a list"]\n{"source": "cursor"}\n')
        self.assertEqual([s["conversation_id"] for s in sessions.read_sessions(self.store())], ["good"])
        self.assertIn("· cursor ·", sessions.render_recent_sessions(self.store()))
        self.record("sessionStart", "next", 1)
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_concurrent_distinct_sessions_are_all_kept(self) -> None:
        self.store()
        script = (
            "import sys\n"
            "from datetime import datetime, timezone\n"
            "from pathlib import Path\n"
            "from memory_system.paths import workspace_store\n"
            "from memory_system.recall.sessions import record_session_event\n"
            "store = workspace_store(Path(sys.argv[1]))\n"
            "for n in range(10):\n"
            "    record_session_event(store, 'sessionStart', f'{sys.argv[2]}-{n}', 'cursor', datetime.now(timezone.utc).isoformat())\n"
        )
        env = self.env() | {"PYTHONPATH": str(MEM_LIB)}
        procs = [
            subprocess.Popen([sys.executable, "-c", script, str(self.project), f"w{w}"], env=env, stderr=subprocess.PIPE, text=True)
            for w in range(4)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(len(sessions.read_sessions(self.store(), 1000)), 40)

    def test_busy_sessions_lock_skips_quickly(self) -> None:
        store = self.store()
        with file_lock(store / ".sessions.lock", timeout=1):
            start = time.monotonic()
            self.record("sessionStart", "c1", 0)
            self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual(sessions.read_sessions(store), [])


class TestRendering(SessionsTestBase):
    def test_recent_sessions_lists_newest_five_with_counts(self) -> None:
        for n in range(7):
            self.record("sessionStart", f"c{n}", n * 60, "claude-code" if n % 2 else "cursor", None)
            self.record("beforeSubmitPrompt", f"c{n}", n * 60 + 1, "claude-code" if n % 2 else "cursor", prompt=f"prompt number {n}")
        self.record("sessionEnd", "c6", 6 * 60 + 25, "cursor")
        base = {"workspace_roots": [str(self.project)], "conversation_id": "c6", "source": "cursor"}
        for _ in range(3):
            append_event(self.project, "afterFileEdit", {**base, "hook_event_name": "afterFileEdit", "file_path": str(self.project / "a.py")})
        append_event(self.project, "afterShellExecution", {**base, "hook_event_name": "afterShellExecution", "command": "make"})
        append_event(self.project, "afterShellExecution", {**base, "source": "claude-code", "hook_event_name": "afterShellExecution", "command": "ls"})
        lines = sessions.render_recent_sessions(self.store()).splitlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual([re.search(r'prompt number (\d)', line).group(1) for line in lines], ["6", "5", "4", "3", "2"])
        self.assertEqual(lines[0], f'- 2026-09-01 06:00 UTC · cursor · 25 min · 3 edits, 1 command — "prompt number 6"')
        self.assertIn("· claude-code · 0 edits, 0 commands", lines[1])

    def test_pack_stays_within_token_cap(self) -> None:
        store = self.store()
        (store / "memory-bank").mkdir(parents=True, exist_ok=True)
        (store / "memory-bank" / "domainContext.md").write_text(
            "# Domain\n\n" + "".join(f"- [2026-09-01] #decision: fact {i} " + "x" * 100 + "\n" for i in range(4000)),
            encoding="utf-8",
        )
        for n in range(200):
            self.record("sessionStart", f"c{n}", n, prompt=None)
            self.record("beforeSubmitPrompt", f"c{n}", n, prompt="y" * 500)
        render_rule_file(self.project)
        rule = (self.project / CURSOR_RULE).read_text(encoding="utf-8")
        cap_chars = int(json.loads((self.stores / "config.json").read_text())["context_pack_token_cap"]) * 4
        self.assertLessEqual(len(rule), cap_chars)
        self.assertIn("## Recent sessions", rule)
        self.assertEqual(len(self.timeline(rule).strip().splitlines()), 5)
        self.assertIn("memory recall", rule, "the recall footer survives the trim")

    def test_cli_render_includes_recent_sessions(self) -> None:
        self.configure(["cursor"], observer_token_threshold=1)
        self.cursor("sessionStart", self.project)
        self.cursor("beforeSubmitPrompt", self.project, prompt="look into the flaky upload test")
        self.cursor("sessionEnd", self.project)
        proc = subprocess.run([sys.executable, str(CLI), "render", "--workspace", str(self.project)], env=self.env(), capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rule = (self.project / CURSOR_RULE).read_text(encoding="utf-8")
        headings = re.findall(r"^## (.+)$", rule, re.M)
        self.assertIn("Recent sessions", headings)
        position = headings.index("Recent sessions")
        if "Current work state" in headings:
            self.assertLess(headings.index("Current work state"), position)
        if "Recent observations" in headings:
            self.assertGreater(headings.index("Recent observations"), position)
        self.assertIn('— "look into the flaky upload test"', self.timeline(rule))


class TestCrossToolFreshness(SessionsTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.configure(["cursor", "claude-code", "opencode"], observer_token_threshold=1)

    def assert_fact_and_timeline(self, project: Path, source: str) -> None:
        for name, text in self.artifacts(project).items():
            with self.subTest(artifact=name, source=source):
                self.assertIn(FACT, text)
                self.assertRegex(self.timeline(text), rf"· {re.escape(source)} ·")

    def test_cross_tool_session_end_refreshes_rules_and_timeline(self) -> None:
        cursor_project = self.new_project("cursor-sender")
        self.cursor("sessionStart", cursor_project, "same-id")
        self.cursor("beforeSubmitPrompt", cursor_project, "same-id", prompt=f"remember that {FACT}")
        self.cursor("sessionEnd", cursor_project, "same-id")
        self.assert_fact_and_timeline(cursor_project, "cursor")
        receiver = self.claude("SessionStart", cursor_project, "same-id", source="startup")
        self.assertIn("hookSpecificOutput", receiver)
        claude_view = self.timeline((cursor_project / CLAUDE_RULE).read_text(encoding="utf-8"))
        self.assertIn("· cursor ·", claude_view)
        self.assertIn("· claude-code ·", claude_view, "the same id from another tool is a separate session")

        claude_project = self.new_project("claude-sender")
        self.claude("SessionStart", claude_project, source="startup")
        self.claude("UserPromptSubmit", claude_project, prompt=f"remember that {FACT}")
        self.claude("Stop", claude_project, last_assistant_message="Noted.")
        self.claude("SessionEnd", claude_project, reason="exit")
        self.assert_fact_and_timeline(claude_project, "claude-code")
        self.assertIn("additional_context", self.cursor("sessionStart", claude_project, "c2"))
        self.assertIn(FACT, (claude_project / CURSOR_RULE).read_text(encoding="utf-8"))

    @unittest.skipUnless(NODE, "node is not installed; the OpenCode sender runs the real plugin in node")
    def test_cross_tool_opencode_execution_end_refreshes_rules_and_timeline(self) -> None:
        project = self.new_project("opencode-sender")
        (self.tmp / "harness.mjs").write_text(_plugin_harness(), encoding="utf-8")
        script = self.tmp / "scenario.mjs"
        script.write_text(
            'import { run } from "./harness.mjs"\n'
            f"const PROJECT = {json.dumps(str(project))}\n"
            "await run(async (h) => {\n"
            '  const s = h.session("ses_o", PROJECT)\n'
            "  const p = await h.load(PROJECT)\n"
            f'  await p.prompt("ses_o", "msg_u1", {json.dumps("remember that " + FACT)})\n'
            '  s.messages.push(h.user("msg_u1", "remember"), h.assistant("msg_a1", "Noted."))\n'
            '  await h.emit("session.execution.succeeded", "ses_o")\n'
            '  await p.context("ses_o")\n'
            "  await p.unload()\n"
            "})\n",
            encoding="utf-8",
        )
        env = self.env() | {
            "MEMORY_BIN": str(CLI),
            "MEMORY_PY": sys.executable,
            "PLUGIN_PATH": str(PLUGIN),
            "FAKE_LOG": str(self.tmp / "no-fake-cli.jsonl"),
        }
        proc = subprocess.run([NODE, str(script)], env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertEqual(json.loads(proc.stdout)["thrown"], [])
        self.assert_fact_and_timeline(project, "opencode")
        hooks = [json.loads(line)["hook"] for line in (self.store(project) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(hooks, ["sessionStart", "beforeSubmitPrompt", "afterAgentResponse", "stop"])
        self.assertIn("hookSpecificOutput", self.claude("SessionStart", project, source="startup"))
        claude_view = self.timeline((project / CLAUDE_RULE).read_text(encoding="utf-8"))
        self.assertIn("· opencode ·", claude_view)
        self.assertIn(FACT, (project / CLAUDE_RULE).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
