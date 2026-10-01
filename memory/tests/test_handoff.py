"""Compaction handoff notes: written at preCompact, restored once after compaction.

Guards:
  - A completed preCompact pass writes the conversation's note with its work
    state, open tasks, and its own last five prompts (not another
    conversation's or another tool's).
  - Claude Code's compact SessionStart restores the note once; notes of other
    conversations stay. A restoration is capped at 9,000 characters, and a
    note older than 24 hours is never restored.
  - A busy worker writes nothing and leaves the earlier note and its token as
    they were; a missing identity writes nothing.
  - OpenCode notes carry the compaction attempt's token: the preCompact
    acknowledgment reports it only when a new note was written, and a failed
    or different attempt never restores the note.
  - preCompact stays inside the hook's 10-second budget.

Isolation: every test uses a throwaway HOME, memory home, and project.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

MEM_HOME = Path(__file__).resolve().parents[1]
MEM_LIB = MEM_HOME / "lib"
CLI = MEM_HOME / "bin" / "memory"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system.events import worker  # noqa: E402
from memory_system.paths import workspace_store  # noqa: E402
from memory_system.recall import handoff  # noqa: E402
from memory_system.safety import file_lock  # noqa: E402

_ENV_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN", "MEMORY_EMBEDDING_BACKEND")
RESTORED = "Context restored after compaction"


class HandoffTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_handoff_")).resolve()
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        self.stores = self.tmp / "stores"
        self.stores.mkdir()
        shutil.copy2(MEM_HOME / "config.json", self.stores / "config.json")
        os.environ["SILLY_MEMORY_HOME"] = str(self.stores)
        os.environ["MEMORY_EMBEDDING_BACKEND"] = "noop"
        self.project = self.tmp / "project"
        (self.project / ".git").mkdir(parents=True)

    def tearDown(self) -> None:
        for key in _ENV_VARS:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in self._saved.items() if v is not None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def hook(self, payload: dict, tool: str | None = None) -> dict:
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
        flags = ["--tool", tool] if tool else []
        proc = subprocess.run(
            [sys.executable, str(CLI), "hook", *flags], input=json.dumps(payload), env=env, capture_output=True, text=True, timeout=120
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def claude(self, hook: str, session: str = "A", **fields: object) -> dict:
        return self.hook({"hook_event_name": hook, "session_id": session, "cwd": str(self.project), **fields})

    def opencode(self, hook: str, session: str = "ses_1", **fields: object) -> dict:
        payload = {"hook_event_name": hook, "workspace_roots": [str(self.project)], "conversation_id": session, **fields}
        return self.hook(payload, "opencode")

    def store(self) -> Path:
        return workspace_store(self.project)

    def note(self, source: str = "claude-code", session: str = "A") -> Path:
        return handoff.handoff_path(self.store(), source, session)

    def note_meta(self, path: Path) -> dict:
        return json.loads(re.match(r"<!-- silly-memory handoff (\{.*\}) -->", path.read_text(encoding="utf-8")).group(1))

    def restore_claude(self, session: str = "A") -> str:
        return self.claude("SessionStart", session, source="compact")["hookSpecificOutput"]["additionalContext"]


class TestHandoffWriter(HandoffTestBase):
    def test_precompact_writes_handoff_with_state_tasks_and_prompts(self) -> None:
        self.claude("SessionStart", source="startup")
        for n in range(7):
            self.claude("UserPromptSubmit", prompt=f"step {n} of the parser rewrite")
        self.claude("UserPromptSubmit", "B", prompt="unrelated prompt from session B")
        self.hook({"hook_event_name": "beforeSubmitPrompt", "workspace_roots": [str(self.project)], "conversation_id": "A", "prompt": "cursor prompt with the same id"})
        (self.store() / "memory-bank").mkdir(parents=True, exist_ok=True)
        (self.store() / "memory-bank" / "actionItems.md").write_text(
            "# Actions\n\n- [ ] [2026-09-29] #release: ship the parser — owner: Maya\n- [x] [2026-09-28] #release: finished spike\n",
            encoding="utf-8",
        )
        self.claude("PreCompact", trigger="auto")

        text = self.note().read_text(encoding="utf-8")
        meta = self.note_meta(self.note())
        self.assertEqual((meta["source"], meta["conversation_id"]), ("claude-code", "A"))
        self.assertTrue(meta["token"])
        self.assertIn("## Work state", text)
        work_state = (self.store() / "work-state.md").read_text(encoding="utf-8").strip()
        self.assertIn(work_state, text)
        self.assertIn("ship the parser", text)
        self.assertNotIn("finished spike", text)
        prompts = re.findall(r"^\d\. (.*)$", text, re.M)
        self.assertEqual(prompts, [f"step {n} of the parser rewrite" for n in range(2, 7)])
        self.assertNotIn("session B", text)
        self.assertNotIn("cursor prompt", text)

        restored = self.restore_claude()
        self.assertTrue(restored.startswith(RESTORED), restored)
        self.assertIn("ship the parser", restored)
        self.assertFalse(self.note().exists())
        self.assertNotIn(RESTORED, self.restore_claude(), "a note is restored only once")

    def test_overlapping_sessions_restore_only_their_own_note(self) -> None:
        self.claude("UserPromptSubmit", "A", prompt="alpha work")
        self.claude("UserPromptSubmit", "B", prompt="beta work")
        self.claude("PreCompact", "A")
        self.claude("PreCompact", "B")
        restored_a = self.restore_claude("A")
        self.assertIn("alpha work", restored_a)
        self.assertNotIn("beta work", restored_a)
        self.assertTrue(self.note(session="B").exists())
        self.assertIn("beta work", self.restore_claude("B"))

    def test_handoff_is_capped_at_9000_chars(self) -> None:
        (self.store() / "memory-bank").mkdir(parents=True, exist_ok=True)
        tasks = "".join(f"- [ ] [2026-09-29] #backlog: task {n} " + "x" * 80 + "\n" for n in range(300))
        (self.store() / "memory-bank" / "actionItems.md").write_text("# Actions\n\n" + tasks, encoding="utf-8")
        self.claude("UserPromptSubmit", prompt="long session")
        self.claude("PreCompact")
        self.assertGreater(len(self.note().read_text(encoding="utf-8")), 9000)
        restored = self.restore_claude()
        self.assertTrue(restored.startswith(RESTORED))
        self.assertLessEqual(len(restored), 9000)

    def test_top_up_ignores_handoff_older_than_24h(self) -> None:
        self.claude("PreCompact")
        path = self.note()
        first, _, body = path.read_text(encoding="utf-8").partition("\n")
        meta = self.note_meta(path)
        meta["created"] = time.time() - handoff.HANDOFF_MAX_AGE_SECONDS - 60
        path.write_text(f"<!-- silly-memory handoff {json.dumps(meta, sort_keys=True)} -->\n{body}", encoding="utf-8")
        self.assertNotIn(RESTORED, self.restore_claude())
        self.assertFalse(path.exists(), "an expired note is removed when its conversation looks for it")

    def test_precompact_stays_inside_hook_budget(self) -> None:
        for n in range(30):
            self.claude("UserPromptSubmit", prompt=f"prompt {n} about routine deploys")
        start = time.monotonic()
        self.claude("PreCompact")
        elapsed = time.monotonic() - start
        self.assertTrue(self.note().exists())
        self.assertLess(elapsed, 10.0, f"preCompact took {elapsed:.1f}s; the Claude Code hook timeout is 10s")

    def test_missing_identity_writes_no_note(self) -> None:
        self.hook({"hook_event_name": "PreCompact", "cwd": str(self.project)}, "claude-code")
        self.hook({"hook_event_name": "preCompact", "workspace_roots": [str(self.project)]})
        self.assertFalse((self.store() / "handoffs").exists())

    def test_busy_worker_keeps_the_earlier_note(self) -> None:
        self.claude("UserPromptSubmit", prompt="first attempt")
        self.claude("PreCompact")
        before = self.note().read_bytes()
        with file_lock(worker._worker_lock(self.store()), timeout=1):
            token = worker.handle_hook_job(
                self.project, "preCompact", source="claude-code", conversation_id="A", handoff_token=None
            )
        self.assertIsNone(token)
        self.assertEqual(self.note().read_bytes(), before)


class TestOpenCodeHandoff(HandoffTestBase):
    def test_ack_reports_the_token_only_for_a_new_note(self) -> None:
        self.opencode("sessionStart")
        ack = self.opencode("preCompact", handoff_token="tok-1")["memory_capture"]
        self.assertEqual(ack["handoff_token"], "tok-1")
        self.assertEqual(self.note_meta(self.note("opencode", "ses_1"))["token"], "tok-1")
        with file_lock(worker._worker_lock(self.store()), timeout=1):
            busy = self.opencode("preCompact", handoff_token="tok-2")["memory_capture"]
        self.assertNotIn("handoff_token", busy)
        self.assertEqual(self.note_meta(self.note("opencode", "ses_1"))["token"], "tok-1")
        without_token = self.opencode("preCompact", "ses_2")["memory_capture"]
        self.assertNotIn("handoff_token", without_token)
        self.assertFalse(self.note("opencode", "ses_2").exists())

    def test_failed_compaction_retains_the_note_until_its_attempt_restores_it(self) -> None:
        self.opencode("beforeSubmitPrompt", prompt="keep working on the importer", message_id="msg_1")
        self.opencode("preCompact", handoff_token="tok-1")
        for token in (None, "tok-0"):
            fields = {"session_source": "compact"} | ({"handoff_token": token} if token else {})
            reply = self.opencode("sessionStart", **fields)
            self.assertNotIn(RESTORED, reply["additional_context"])
            self.assertTrue(self.note("opencode", "ses_1").exists())
        reply = self.opencode("sessionStart", session_source="compact", handoff_token="tok-1")
        self.assertTrue(reply["additional_context"].startswith(RESTORED))
        self.assertIn("keep working on the importer", reply["additional_context"])
        self.assertFalse(self.note("opencode", "ses_1").exists())

    def test_claude_and_opencode_notes_with_the_same_id_stay_separate(self) -> None:
        self.claude("PreCompact", "same-id")
        self.opencode("preCompact", "same-id", handoff_token="tok-1")
        self.assertTrue(self.restore_claude("same-id").startswith(RESTORED))
        self.assertTrue(self.note("opencode", "same-id").exists())


if __name__ == "__main__":
    unittest.main()
