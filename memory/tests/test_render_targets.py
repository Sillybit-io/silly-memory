"""Project rules for every wired tool, lifecycle refresh, keyed handoff restore.

Guards:
  - ``render_rule_file`` writes one rule per tool in ``config.json:tools``:
    Cursor's ``.mdc`` with frontmatter, Claude Code's plain ``.md``, nothing
    for OpenCode, and never creates ``.claude/`` for a Cursor-only setup.
  - A Cursor ``sessionEnd`` refreshes the stored pack and every rule from the
    persisted bank without ``memory render``; ``stop`` only queues.
  - The rule write waits at most 0.1 s for the store lock, and the hook fails
    open when it cannot get it.
  - ``process_queue`` reports whether the pass completed, and ``memory
    process`` exits 75 when it did not.
  - A compact sessionStart restores only that conversation's handoff note,
    once, within 24 hours and 9,000 characters; OpenCode also needs the
    note's generation token.

Isolation: every test uses a throwaway HOME, memory home, and project.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

MEM_HOME = Path(__file__).resolve().parents[1]
MEM_LIB = MEM_HOME / "lib"
CLI = MEM_HOME / "bin" / "memory"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system.events import worker  # noqa: E402
from memory_system.paths import bank_path, ensure_layout, lock_path, workspace_store  # noqa: E402
from memory_system.recall import handoff  # noqa: E402
from memory_system.recall.context_pack import render_rule_file, session_start_top_up  # noqa: E402
from memory_system.safety import LockTimeout, file_lock  # noqa: E402

_ENV_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN")
CURSOR_RULE = Path(".cursor") / "rules" / "_memory-context.mdc"
CLAUDE_RULE = Path(".claude") / "rules" / "_memory-context.md"


class RenderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_render_")).resolve()
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        self.home = self.tmp / "stores"
        self.home.mkdir()
        os.environ["SILLY_MEMORY_HOME"] = str(self.home)
        self.project = self.tmp / "project"
        (self.project / ".git").mkdir(parents=True)
        self.set_tools(["cursor"])

    def tearDown(self) -> None:
        for key in _ENV_VARS:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in self._saved.items() if v is not None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_tools(self, tools: list[str]) -> None:
        config = json.loads((MEM_HOME / "config.json").read_text(encoding="utf-8"))
        config["tools"] = tools
        (self.home / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def store(self) -> Path:
        store = workspace_store(self.project)
        ensure_layout(store)
        return store

    def seed_fact(self, fact: str) -> None:
        bank_path(self.store(), "domainContext.md").write_text(
            f"# Domain\n\n- [2026-09-29] #decision: {fact}\n", encoding="utf-8"
        )

    def hook(self, event: str, **fields: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        env.update(
            {
                "SILLY_MEMORY_HOME": str(self.home),
                "HOME": str(self.tmp / "user-home"),
                "MEMORY_EMBEDDING_BACKEND": "noop",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        payload = {"hook_event_name": event, "workspace_roots": [str(self.project)], "conversation_id": "c1", **fields}
        return subprocess.run(
            [sys.executable, str(CLI), "hook"], input=json.dumps(payload), env=env, capture_output=True, text=True, timeout=120
        )

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        env.update({"SILLY_MEMORY_HOME": str(self.home), "HOME": str(self.tmp / "user-home"), "PYTHONDONTWRITEBYTECODE": "1"})
        return subprocess.run([sys.executable, str(CLI), *args], env=env, capture_output=True, text=True, timeout=120)


class TestRenderTargets(RenderTestBase):
    def test_cursor_only_writes_mdc_only(self) -> None:
        self.seed_fact("deploy target is zephyr-stage")
        self.assertEqual(render_rule_file(self.project), self.project / CURSOR_RULE)
        self.assertIn("zephyr-stage", (self.project / CURSOR_RULE).read_text(encoding="utf-8"))
        self.assertFalse((self.project / ".claude").exists())

    def test_cursor_and_claude_code_write_both_targets(self) -> None:
        self.set_tools(["cursor", "claude-code"])
        self.seed_fact("deploy target is zephyr-stage")
        render_rule_file(self.project)
        for rule in (CURSOR_RULE, CLAUDE_RULE):
            self.assertIn("zephyr-stage", (self.project / rule).read_text(encoding="utf-8"), rule)
        pack = (self.store() / "context-pack.md").read_text(encoding="utf-8")
        self.assertTrue((self.project / CLAUDE_RULE).read_text(encoding="utf-8").endswith(pack))

    def test_claude_rule_has_no_frontmatter(self) -> None:
        self.set_tools(["claude-code"])
        self.seed_fact("deploy target is zephyr-stage")
        self.assertEqual(render_rule_file(self.project), self.project / CLAUDE_RULE)
        claude = (self.project / CLAUDE_RULE).read_text(encoding="utf-8")
        self.assertTrue(claude.startswith("# Memory Context\n"), claude[:80])
        self.assertNotIn("alwaysApply", claude)
        self.assertNotIn("Cursor Memory", claude)
        self.assertFalse((self.project / ".cursor").exists())

    def test_cursor_rule_keeps_frontmatter_with_the_new_name(self) -> None:
        render_rule_file(self.project)
        cursor = (self.project / CURSOR_RULE).read_text(encoding="utf-8")
        self.assertTrue(cursor.startswith("---\ndescription:"), cursor[:80])
        self.assertIn("alwaysApply: true", cursor)
        self.assertIn("silly-memory", cursor)
        self.assertNotIn("Cursor Memory", cursor)

    def test_claude_gitignore_lists_generated_rule(self) -> None:
        self.set_tools(["cursor", "claude-code"])
        render_rule_file(self.project)
        render_rule_file(self.project)
        ignore = (self.project / ".claude" / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(ignore.count("rules/_memory-context.md"), 1)
        self.assertIn("rules/_memory-context.mdc", (self.project / ".cursor" / ".gitignore").read_text(encoding="utf-8"))

    def test_no_project_rule_without_a_rule_tool(self) -> None:
        for tools in ([], ["opencode"]):
            with self.subTest(tools=tools):
                self.set_tools(tools)
                self.seed_fact("deploy target is zephyr-stage")
                result = render_rule_file(self.project)
                self.assertEqual(result, self.store() / "context-pack.md")
                self.assertIn("zephyr-stage", result.read_text(encoding="utf-8"))
                self.assertFalse((self.project / ".cursor").exists())
                self.assertFalse((self.project / ".claude").exists())

    def test_rule_write_lock_timeout_is_bounded(self) -> None:
        store = self.store()
        with file_lock(lock_path(store), timeout=1):
            start = time.monotonic()
            with self.assertRaises(LockTimeout):
                render_rule_file(self.project)
            self.assertLess(time.monotonic() - start, 1.0)
            start = time.monotonic()
            proc = self.hook("sessionStart")
            elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The rule write is skipped; the session still gets its top-up context.
        self.assertLessEqual(set(json.loads(proc.stdout)), {"additional_context"})
        self.assertLess(elapsed, 15.0, "the sessionStart hook must fail open quickly")
        self.assertFalse((self.project / CURSOR_RULE).exists())


class TestLifecycleRefresh(RenderTestBase):
    def test_session_end_refreshes_all_rules_from_persisted_bank(self) -> None:
        self.set_tools(["cursor", "claude-code"])
        self.seed_fact("deploy target is zephyr-stage")
        proc = self.hook("sessionEnd")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "{}"), proc.stderr)
        for artifact in (self.project / CURSOR_RULE, self.project / CLAUDE_RULE, self.store() / "context-pack.md"):
            self.assertIn("zephyr-stage", artifact.read_text(encoding="utf-8"), artifact)
        # A changed persisted fact reaches every target on the next boundary, still without `memory render`.
        self.seed_fact("deploy target is aurora-prod")
        self.hook("sessionEnd")
        cursor = (self.project / CURSOR_RULE).read_text(encoding="utf-8")
        claude = (self.project / CLAUDE_RULE).read_text(encoding="utf-8")
        for text in (cursor, claude, (self.store() / "context-pack.md").read_text(encoding="utf-8")):
            self.assertIn("aurora-prod", text)
            self.assertNotIn("zephyr-stage", text)
        self.assertIn("alwaysApply", cursor)
        self.assertNotIn("alwaysApply", claude)

    def test_stop_remains_queue_only(self) -> None:
        self.set_tools(["cursor", "claude-code"])
        self.seed_fact("deploy target is zephyr-stage")
        store = self.store()
        observations = (store / "observations.md").read_text(encoding="utf-8")
        proc = self.hook("stop")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "{}"), proc.stderr)
        self.assertFalse((self.project / CURSOR_RULE).exists())
        self.assertFalse((self.project / CLAUDE_RULE).exists())
        self.assertNotIn("zephyr-stage", (store / "context-pack.md").read_text(encoding="utf-8"))
        self.assertEqual((store / "observations.md").read_text(encoding="utf-8"), observations)
        jobs = [json.loads(line)["type"] for line in (store / "queues" / "pending.jsonl").read_text().splitlines()]
        self.assertEqual(jobs, ["stop"])

    def test_process_queue_reports_completion(self) -> None:
        store = self.store()
        self.assertTrue(worker.process_queue(self.project))
        worker.handle_hook_job(self.project, "observe")
        with file_lock(worker._worker_lock(store), timeout=1):
            self.assertFalse(worker.process_queue(self.project))
        self.assertTrue((store / "queues" / "pending.jsonl").read_text().strip(), "a busy pass must keep queued work")

    def test_memory_process_exits_75_when_busy(self) -> None:
        store = self.store()
        with file_lock(worker._worker_lock(store), timeout=1):
            busy = self.cli("process", "--workspace", str(self.project))
        self.assertEqual(busy.returncode, 75)
        self.assertIn("queued work is kept", busy.stderr)
        done = self.cli("process", "--workspace", str(self.project))
        self.assertEqual(done.returncode, 0, done.stderr)


class TestHandoffTopUp(RenderTestBase):
    def note(
        self,
        source: str = "claude-code",
        conversation: str = "A",
        body: str = "- open task: ship 3.0",
        token: str | None = None,
        now: float | None = None,
    ) -> Path:
        handoff.write_handoff(self.store(), source, conversation, body, token=token, now=now)
        return handoff.handoff_path(self.store(), source, conversation)

    def test_top_up_restores_handoff_on_compact_and_deletes_it(self) -> None:
        path = self.note()
        restored = session_start_top_up(self.project, "compact", "claude-code", "A")
        self.assertEqual(restored, "Context restored after compaction\n\n- open task: ship 3.0")
        self.assertFalse(path.exists())
        self.assertIn("pack refreshed", session_start_top_up(self.project, "compact", "claude-code", "A") or "")

    def test_top_up_ignores_handoff_on_startup(self) -> None:
        path = self.note()
        for session_source in ("startup", "resume", "clear"):
            text = session_start_top_up(self.project, session_source, "claude-code", "A") or ""
            self.assertIn("silly-memory pack refreshed", text)
            self.assertNotIn("Context restored", text)
        self.assertIn("silly-memory pack refreshed", session_start_top_up(self.project) or "")
        self.assertTrue(path.exists())

    def test_missing_identity_or_other_session_never_consumes(self) -> None:
        mine = self.note(conversation="A")
        theirs = self.note(conversation="B", body="- B's work")
        other_tool = self.note(source="cursor", conversation="A", body="- cursor's work")
        for args in (("compact", None, "A"), ("compact", "claude-code", None), ("compact", "claude-code", "C")):
            self.assertNotIn("Context restored", session_start_top_up(self.project, *args) or "")
        self.assertIn("- open task", session_start_top_up(self.project, "compact", "claude-code", "A") or "")
        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())
        self.assertTrue(other_tool.exists())

    def test_two_consumers_restore_the_note_once(self) -> None:
        self.note()
        results: list[str | None] = []
        barrier = threading.Barrier(4)

        def consume() -> None:
            barrier.wait()
            results.append(handoff.consume_handoff(self.store(), "claude-code", "A"))

        threads = [threading.Thread(target=consume) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sum(1 for r in results if r), 1, results)

    def test_expired_note_is_removed_and_not_restored(self) -> None:
        stale = self.note(now=time.time() - handoff.HANDOFF_MAX_AGE_SECONDS - 60)
        other_stale = self.note(conversation="B", now=time.time() - handoff.HANDOFF_MAX_AGE_SECONDS - 60)
        fresh = self.note(conversation="C")
        self.assertNotIn("Context restored", session_start_top_up(self.project, "compact", "claude-code", "A") or "")
        self.assertFalse(stale.exists())
        self.assertFalse(other_stale.exists())
        self.assertTrue(fresh.exists())

    def test_opencode_needs_the_matching_generation_token(self) -> None:
        path = self.note(source="opencode", conversation="ses_1", token="tok-2")
        for token in (None, "tok-1"):
            text = session_start_top_up(self.project, "compact", "opencode", "ses_1", token) or ""
            self.assertNotIn("Context restored", text)
            self.assertTrue(path.exists())
        text = session_start_top_up(self.project, "compact", "opencode", "ses_1", "tok-2") or ""
        self.assertTrue(text.startswith("Context restored after compaction"))
        self.assertFalse(path.exists())

    def test_note_replaced_after_the_token_was_read_is_left_in_place(self) -> None:
        old = handoff.write_handoff(self.store(), "opencode", "ses_1", "- attempt one")
        new = handoff.write_handoff(self.store(), "opencode", "ses_1", "- attempt two")
        self.assertNotEqual(old, new)
        self.assertIsNone(handoff.consume_handoff(self.store(), "opencode", "ses_1", token=old, require_token=True))
        restored = handoff.consume_handoff(self.store(), "opencode", "ses_1", token=new, require_token=True)
        self.assertIn("- attempt two", restored or "")

    def test_restoration_is_capped_at_9000_characters(self) -> None:
        self.note(body="- " + "x" * 20_000)
        restored = session_start_top_up(self.project, "compact", "claude-code", "A") or ""
        self.assertTrue(restored.startswith("Context restored after compaction"))
        self.assertLessEqual(len(restored), 9000)
        self.assertGreater(len(restored), 8900)

    def test_unreadable_note_metadata_is_not_restored(self) -> None:
        path = handoff.handoff_path(self.store(), "claude-code", "A")
        path.parent.mkdir(parents=True)
        path.write_text("no metadata line\n- body\n", encoding="utf-8")
        self.assertNotIn("Context restored", session_start_top_up(self.project, "compact", "claude-code", "A") or "")


if __name__ == "__main__":
    unittest.main()
