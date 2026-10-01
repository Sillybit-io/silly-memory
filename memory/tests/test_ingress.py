"""Hook ingress: Cursor, Claude Code, and OpenCode payloads become canonical events.

Guards:
  - Claude Code hooks map onto the canonical vocabulary (``Stop`` is the reply
    then ``stop``), carry ``source`` and ``session_source``, and answer
    ``SessionStart`` with ``hookSpecificOutput``; every hook prints exactly one
    JSON object.
  - Native fields are allowlisted: raw ``last_assistant_message`` or
    ``tool_input`` never reach the event log, and private spans are stripped.
  - Cursor payloads keep their shape and output; an existing Cursor workspace
    keeps its store.
  - Every tool resolves its root to the repository, while a linked worktree
    stays separate.
  - OpenCode gets a persistence acknowledgment naming the resolved workspace
    and store, and needs its generation token to restore a handoff note.
  - Malformed or rootless input prints ``{}``, exits 0, and writes nothing.

Isolation: every CLI run uses a throwaway HOME, memory home, and project.
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

from memory_system.events.ingress import detect_tool, format_output, normalize  # noqa: E402
from memory_system.recall import handoff  # noqa: E402

_ENV_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN")


class _Dirs(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_ingress_")).resolve()
        self.home = self.tmp / "user-home"
        self.home.mkdir()
        self.stores = self.tmp / "stores"
        self.stores.mkdir()
        shutil.copy2(MEM_HOME / "config.json", self.stores / "config.json")
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.subdir = self.repo / "packages" / "api"
        self.subdir.mkdir(parents=True)
        self.worktree = self.repo / "wt"
        (self.worktree / "src").mkdir(parents=True)
        (self.worktree / ".git").write_text("gitdir: ../.git/worktrees/wt\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestNormalize(_Dirs):
    def claude(self, hook: str, **fields: object) -> list[dict]:
        return normalize("claude-code", {"hook_event_name": hook, "session_id": "s1", "cwd": str(self.subdir), **fields})

    def test_claude_hooks_map_onto_the_canonical_vocabulary(self) -> None:
        cases = {
            "SessionStart": ["sessionStart"],
            "UserPromptSubmit": ["beforeSubmitPrompt"],
            "Stop": ["stop"],
            "PreCompact": ["preCompact"],
            "SessionEnd": ["sessionEnd"],
            "Notification": [],
        }
        for native, canonical in cases.items():
            with self.subTest(native=native):
                events = self.claude(native)
                self.assertEqual([e["hook_event_name"] for e in events], canonical)
                for event in events:
                    self.assertEqual(event["source"], "claude-code")
                    self.assertEqual(event["conversation_id"], "s1")
                    self.assertEqual(event["workspace_roots"], [str(self.repo)])

    def test_claude_code_stop_yields_after_agent_response_then_stop(self) -> None:
        events = self.claude("Stop", last_assistant_message="Fixed the parser.", stop_hook_active=False)
        self.assertEqual([e["hook_event_name"] for e in events], ["afterAgentResponse", "stop"])
        self.assertEqual(events[0]["text"], "Fixed the parser.")
        self.assertNotIn("last_assistant_message", events[0])

    def test_tool_results_map_to_edit_and_shell_events(self) -> None:
        cases = [
            ("Edit", {"file_path": "/abs/a.py", "old_string": "x"}, ("afterFileEdit", "file_path", "/abs/a.py")),
            ("Write", {"file_path": "rel/b.py", "content": "y"}, ("afterFileEdit", "file_path", str(self.subdir / "rel/b.py"))),
            ("MultiEdit", {"file_path": "/abs/c.py", "edits": []}, ("afterFileEdit", "file_path", "/abs/c.py")),
            ("NotebookEdit", {"notebook_path": "/abs/n.ipynb"}, ("afterFileEdit", "file_path", "/abs/n.ipynb")),
            ("Bash", {"command": "npm test", "description": "run"}, ("afterShellExecution", "command", "npm test")),
        ]
        for tool_name, tool_input, (hook, field, value) in cases:
            with self.subTest(tool=tool_name):
                (event,) = self.claude("PostToolUse", tool_name=tool_name, tool_input=tool_input, tool_response={"ok": 1})
                self.assertEqual((event["hook_event_name"], event[field]), (hook, value))
                self.assertFalse({"tool_input", "tool_response", "tool_name"} & set(event))
        self.assertEqual(self.claude("PostToolUse", tool_name="Read", tool_input={"file_path": "/x"}), [])

    def test_session_source_is_carried_and_defaulted(self) -> None:
        self.assertEqual(self.claude("SessionStart", source="compact")[0]["session_source"], "compact")
        self.assertEqual(self.claude("SessionStart", source="weird")[0]["session_source"], "startup")
        (cursor,) = normalize("cursor", {"hook_event_name": "sessionStart", "workspace_roots": [str(self.repo)]})
        self.assertEqual(cursor["session_source"], "startup")

    def test_claude_code_cwd_in_subdir_resolves_to_repo_root(self) -> None:
        (event,) = self.claude("SessionStart")
        self.assertEqual(event["workspace_roots"], [str(self.repo)])
        (in_worktree,) = normalize(
            "claude-code", {"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(self.worktree / "src")}
        )
        self.assertEqual(in_worktree["workspace_roots"], [str(self.worktree)])

    def test_cursor_payload_passes_through_with_source(self) -> None:
        payload = {"hook_event_name": "afterFileEdit", "workspace_roots": [str(self.repo)], "file_path": "/a", "edits": [1]}
        (event,) = normalize("cursor", payload)
        self.assertEqual(event, {**payload, "source": "cursor"})

    def test_a_workspace_with_its_own_marker_keeps_it(self) -> None:
        (self.subdir / ".silly-memory").mkdir()
        (self.subdir / ".silly-memory" / "memory-id").write_text("abcdabcdabcdabcd\n", encoding="utf-8")
        (kept,) = normalize("cursor", {"hook_event_name": "stop", "workspace_roots": [str(self.subdir)]})
        self.assertEqual(kept["workspace_roots"], [str(self.subdir)])
        (resolved,) = normalize("cursor", {"hook_event_name": "stop", "workspace_roots": [str(self.worktree / "src")]})
        self.assertEqual(resolved["workspace_roots"], [str(self.worktree)])

    def test_opencode_payloads_are_validated_and_allowlisted(self) -> None:
        base = {"workspace_roots": [str(self.subdir)], "conversation_id": "ses_1", "tool": "opencode"}
        (event,) = normalize(
            "opencode",
            {**base, "hook_event_name": "sessionStart", "session_source": "compact", "handoff_token": "tok-1", "extra": "x"},
        )
        self.assertEqual(
            event,
            {
                "hook_event_name": "sessionStart",
                "workspace_roots": [str(self.repo)],
                "conversation_id": "ses_1",
                "source": "opencode",
                "session_source": "compact",
                "handoff_token": "tok-1",
            },
        )
        (prompt,) = normalize("opencode", {**base, "hook_event_name": "beforeSubmitPrompt", "prompt": "hi", "message_id": "msg_1", "handoff_token": "t"})
        self.assertEqual((prompt["prompt"], prompt["message_id"]), ("hi", "msg_1"))
        self.assertNotIn("handoff_token", prompt)
        (bad_token,) = normalize("opencode", {**base, "hook_event_name": "preCompact", "handoff_token": "x" * 500})
        self.assertNotIn("handoff_token", bad_token)
        self.assertEqual(normalize("opencode", {**base, "hook_event_name": "Stop"}), [])
        self.assertEqual(normalize("opencode", {"hook_event_name": "stop"}), [])

    def test_detect_tool_and_output_shapes(self) -> None:
        self.assertEqual(detect_tool({"workspace_roots": ["/x"]}), "cursor")
        self.assertEqual(detect_tool({"cwd": "/x", "session_id": "s"}), "claude-code")
        self.assertEqual(detect_tool({"tool": "opencode", "workspace_roots": ["/x"]}), "opencode")
        self.assertIsNone(detect_tool({"cwd": "/x"}))
        self.assertEqual(format_output("cursor", "sessionStart", "ctx"), {"additional_context": "ctx"})
        self.assertEqual(format_output("opencode", "sessionStart", "ctx"), {"additional_context": "ctx"})
        self.assertEqual(
            format_output("claude-code", "sessionStart", "ctx"),
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "ctx"}},
        )
        self.assertEqual(format_output("claude-code", "stop", "ctx"), {})
        self.assertEqual(format_output("cursor", "sessionStart", None), {})


class TestHookCli(_Dirs):
    def hook(self, payload: object, *flags: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        env.update(
            {
                "HOME": str(self.home),
                "SILLY_MEMORY_HOME": str(self.stores),
                "MEMORY_EMBEDDING_BACKEND": "noop",
                "MEMORY_ALLOW_NETWORK": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        proc = subprocess.run([sys.executable, str(CLI), "hook", *flags], input=stdin, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(proc.stdout.splitlines()), 1, f"exactly one JSON object expected: {proc.stdout!r}")
        return proc

    def reply(self, payload: object, *flags: str) -> dict:
        return json.loads(self.hook(payload, *flags).stdout)

    def store_of(self, root: Path) -> Path:
        return self.stores / (root / ".silly-memory" / "memory-id").read_text(encoding="utf-8").strip()

    def events(self, root: Path) -> list[dict]:
        path = self.store_of(root) / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def claude_payload(self, hook: str, cwd: Path | None = None, **fields: object) -> dict:
        return {"hook_event_name": hook, "session_id": "s1", "cwd": str(cwd or self.subdir), **fields}

    def test_claude_code_session_start_output_uses_hook_specific_output(self) -> None:
        checkout_marker = REPO / ".silly-memory"
        existed = checkout_marker.exists()
        out = self.reply(self.claude_payload("SessionStart", source="startup"), "--tool", "claude-code")
        self.assertEqual(set(out), {"hookSpecificOutput"})
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("silly-memory pack refreshed", out["hookSpecificOutput"]["additionalContext"])
        (event,) = self.events(self.repo)
        self.assertEqual((event["hook"], event["source"], event["conversation_id"]), ("sessionStart", "claude-code", "s1"))
        self.assertEqual(checkout_marker.exists(), existed, "the source checkout must not be touched")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [], "the fixture HOME must stay empty")

    def test_stop_appends_reply_then_stop_and_prints_one_object(self) -> None:
        proc = self.hook(self.claude_payload("Stop", last_assistant_message="All tests pass now."))
        self.assertEqual(proc.stdout, "{}\n")
        events = self.events(self.repo)
        self.assertEqual([(e["hook"], e["source"]) for e in events], [("afterAgentResponse", "claude-code"), ("stop", "claude-code")])
        self.assertEqual(events[0]["payload"]["text"], "All tests pass now.")
        jobs = [json.loads(line) for line in (self.store_of(self.repo) / "queues" / "pending.jsonl").read_text().splitlines()]
        self.assertEqual(jobs[-1]["type"], "stop")
        self.assertEqual((jobs[-1]["source"], jobs[-1]["conversation_id"]), ("claude-code", "s1"))

    def test_private_and_secret_native_text_never_reaches_the_log(self) -> None:
        self.hook(self.claude_payload("UserPromptSubmit", prompt="use <private>hunter2-prompt</private> and token=abc123"))
        self.hook(self.claude_payload("Stop", last_assistant_message="ok <private>hunter2-reply</private>"))
        self.hook(self.claude_payload("PostToolUse", tool_name="Bash", tool_input={"command": "export PASSWORD=hunter2-cmd"}))
        log = (self.store_of(self.repo) / "events.jsonl").read_text(encoding="utf-8")
        for secret in ("hunter2", "abc123", "last_assistant_message", "tool_input"):
            self.assertNotIn(secret, log)

    def test_subdirectory_and_worktree_matrix(self) -> None:
        self.hook(self.claude_payload("SessionStart"))
        self.hook({"hook_event_name": "sessionStart", "workspace_roots": [str(self.repo)], "conversation_id": "c1"})
        ack = self.reply(
            {"hook_event_name": "beforeSubmitPrompt", "workspace_roots": [str(self.subdir)], "conversation_id": "ses_1", "prompt": "x"},
            "--tool",
            "opencode",
        )["memory_capture"]
        self.assertEqual(ack["workspace_root"], str(self.repo))
        self.assertEqual(ack["store_path"], str(self.store_of(self.repo)))
        self.assertEqual({e["source"] for e in self.events(self.repo)}, {"claude-code", "cursor", "opencode"})
        self.assertFalse((self.subdir / ".silly-memory").exists())
        self.hook(self.claude_payload("SessionStart", cwd=self.worktree / "src"))
        self.assertNotEqual(self.store_of(self.worktree), self.store_of(self.repo))

    def test_cursor_payload_without_tool_flag_is_unchanged(self) -> None:
        payload = {"hook_event_name": "sessionStart", "workspace_roots": [str(self.repo)], "conversation_id": "c1"}
        out = self.reply(payload)
        self.assertEqual(set(out), {"additional_context"})
        self.assertIn("silly-memory pack refreshed", out["additional_context"])
        self.assertEqual(self.reply({**payload, "hook_event_name": "stop"}), {})
        prompt = {"hook_event_name": "beforeSubmitPrompt", "workspace_roots": [str(self.repo)], "conversation_id": "c1", "prompt": "hello", "model": "m"}
        self.hook(prompt)
        recorded = self.events(self.repo)[-1]
        self.assertEqual((recorded["hook"], recorded["source"]), ("beforeSubmitPrompt", "cursor"))
        self.assertEqual({k: recorded["payload"][k] for k in ("prompt", "model")}, {"prompt": "hello", "model": "m"})

    def test_opencode_payload_records_source_opencode(self) -> None:
        out = self.reply(
            {"hook_event_name": "beforeSubmitPrompt", "workspace_roots": [str(self.repo)], "conversation_id": "ses_1", "prompt": "hi", "message_id": "msg_1"},
            "--tool",
            "opencode",
        )
        capture = out["memory_capture"]
        self.assertEqual(capture["status"], "ok")
        self.assertEqual(len(capture["event_ids"]), 1)
        (event,) = self.events(self.repo)
        self.assertEqual((event["id"], event["source"], event["payload"]["message_id"]), (capture["event_ids"][0], "opencode", "msg_1"))
        started = self.reply({"hook_event_name": "sessionStart", "workspace_roots": [str(self.repo)], "conversation_id": "ses_1"}, "--tool", "opencode")
        self.assertIn("additional_context", started)
        self.assertEqual(started["memory_capture"]["status"], "ok")

    def test_edit_under_claude_settings_is_suppressed(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        self.hook(self.claude_payload("PostToolUse", tool_name="Edit", tool_input={"file_path": str(settings)}))
        store = self.store_of(self.repo)
        self.assertNotIn("afterFileEdit", (store / "events.jsonl").read_text(encoding="utf-8"))
        self.assertIn(str(settings), (store / "events.jsonl-suppressed.log").read_text(encoding="utf-8"))
        plugin = self.home / ".config" / "opencode" / "plugins" / "silly-memory.js"
        ack = self.reply(
            {"hook_event_name": "afterFileEdit", "workspace_roots": [str(self.repo)], "conversation_id": "ses_1", "file_path": str(plugin)},
            "--tool",
            "opencode",
        )["memory_capture"]
        self.assertEqual((ack["status"], ack["event_ids"]), ("ok", []))

    def test_self_observation_roots_hold_without_the_denylist(self) -> None:
        config = json.loads((self.stores / "config.json").read_text(encoding="utf-8"))
        config["path_denylist"] = []
        (self.stores / "config.json").write_text(json.dumps(config), encoding="utf-8")
        own_files = [
            self.home / ".claude" / "settings.json",
            self.home / ".claude" / "rules" / "memory-recall.md",
            self.home / ".claude" / "hooks" / "silly-memory-hook.sh",
            self.home / ".config" / "opencode" / "plugins" / "silly-memory.js",
            self.stores / "config.json",
        ]
        for path in own_files:
            self.hook(self.claude_payload("PostToolUse", tool_name="Write", tool_input={"file_path": str(path)}))
        self.hook(self.claude_payload("PostToolUse", tool_name="Write", tool_input={"file_path": str(self.repo / "app.py")}))
        edits = [e["payload"]["file_path"] for e in self.events(self.repo) if e["hook"] == "afterFileEdit"]
        self.assertEqual(edits, [str(self.repo / "app.py")])

    def test_claude_code_compact_source_reaches_top_up(self) -> None:
        self.hook(self.claude_payload("SessionStart"))
        store = self.store_of(self.repo)
        handoff.write_handoff(store, "claude-code", "s1", "- open task: ship 3.0")
        handoff.write_handoff(store, "claude-code", "other", "- someone else's work")
        startup = self.reply(self.claude_payload("SessionStart", source="startup"))
        self.assertNotIn("Context restored", startup["hookSpecificOutput"]["additionalContext"])
        restored = self.reply(self.claude_payload("SessionStart", source="compact"))["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(restored.startswith("Context restored after compaction"), restored)
        self.assertIn("ship 3.0", restored)
        again = self.reply(self.claude_payload("SessionStart", source="compact"))["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Context restored", again)
        self.assertTrue(handoff.handoff_path(store, "claude-code", "other").exists())
        anonymous = self.reply({"hook_event_name": "SessionStart", "cwd": str(self.subdir), "source": "compact"}, "--tool", "claude-code")
        self.assertNotIn("Context restored", anonymous["hookSpecificOutput"]["additionalContext"])

    def test_opencode_compact_top_up_needs_the_generation_token(self) -> None:
        self.hook({"hook_event_name": "sessionStart", "workspace_roots": [str(self.repo)], "conversation_id": "ses_1"}, "--tool", "opencode")
        token = handoff.write_handoff(self.store_of(self.repo), "opencode", "ses_1", "- resume here")
        request = {"hook_event_name": "sessionStart", "workspace_roots": [str(self.repo)], "conversation_id": "ses_1", "session_source": "compact"}
        self.assertNotIn("Context restored", self.reply(request, "--tool", "opencode")["additional_context"])
        self.assertNotIn("Context restored", self.reply({**request, "handoff_token": "wrong"}, "--tool", "opencode")["additional_context"])
        out = self.reply({**request, "handoff_token": token}, "--tool", "opencode")
        self.assertTrue(out["additional_context"].startswith("Context restored after compaction"))

    def test_rootless_malformed_and_unknown_input_writes_nothing(self) -> None:
        cases = [
            ("not json at all", ()),
            ("[1, 2]", ()),
            (json.dumps({"hook_event_name": "Stop", "session_id": "s1"}), ()),
            (json.dumps({"hook_event_name": "stop"}), ("--tool", "cursor")),
            (json.dumps({"hook_event_name": "Notification", "session_id": "s1", "cwd": str(self.subdir)}), ()),
            (json.dumps({"hook_event_name": "Stop", "workspace_roots": [str(self.repo)]}), ("--tool", "opencode")),
            ("", ()),
        ]
        for stdin, flags in cases:
            with self.subTest(stdin=stdin[:40]):
                self.assertEqual(self.hook(stdin, *flags).stdout, "{}\n")
        self.assertEqual(sorted(p.name for p in self.stores.iterdir()), ["config.json"])
        self.assertFalse((self.repo / ".silly-memory").exists())


if __name__ == "__main__":
    unittest.main()
