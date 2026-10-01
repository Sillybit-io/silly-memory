"""Tests for memory_system.learning.ai_text_log.

The AI-text log is the source of truth for downstream learning components
(e.g. the correction detector at task 15). Each afterAgentResponse must land a
full record — never lossy-truncated — so replay-from-id is deterministic.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class AiTextLogTestBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self.store: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_aitextlog_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        self.store = Path(self._tmp) / "ws-a"
        self.store.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        os.environ.pop("SILLY_MEMORY_HOME", None)


class TestAppendAiText(AiTextLogTestBase):
    def test_append_writes_full_record(self) -> None:
        mod = importlib.import_module("memory_system.learning.ai_text_log")
        mod.append_ai_text(
            self.store,
            conversation_id="c1",
            prompt="How do I use Tailwind?",
            response_text="Use the cn() helper from utils.",
            ts_iso="2026-06-12T12:00:00+00:00",
        )
        log_path = self.store / mod._AI_TEXT_LOG_FILENAME
        self.assertTrue(log_path.exists())
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual(rec["conversation_id"], "c1")
        self.assertEqual(rec["prompt"], "How do I use Tailwind?")
        self.assertEqual(rec["response_text"], "Use the cn() helper from utils.")
        self.assertEqual(rec["ts"], "2026-06-12T12:00:00+00:00")
        self.assertIn("id", rec)
        self.assertTrue(rec["id"])

    def test_append_redacts_secrets_in_response(self) -> None:
        mod = importlib.import_module("memory_system.learning.ai_text_log")
        mod.append_ai_text(
            self.store,
            conversation_id="c2",
            prompt="show me",
            response_text="Your api_key=hunter2supersecret is leaked",
            ts_iso="2026-06-12T12:01:00+00:00",
        )
        rec = json.loads((self.store / mod._AI_TEXT_LOG_FILENAME).read_text(encoding="utf-8").strip())
        self.assertNotIn("hunter2supersecret", rec["response_text"])
        self.assertIn("[REDACTED]", rec["response_text"])

    def test_response_not_lossy_truncated_below_sanitize_cap(self) -> None:
        mod = importlib.import_module("memory_system.learning.ai_text_log")
        long_text = "A" * 6000
        mod.append_ai_text(self.store, "cL", "ask", long_text, "2026-06-12T12:02:00+00:00")
        rec = json.loads((self.store / mod._AI_TEXT_LOG_FILENAME).read_text(encoding="utf-8").strip())
        self.assertEqual(len(rec["response_text"]), 6000)

    def test_unique_ids_per_record(self) -> None:
        mod = importlib.import_module("memory_system.learning.ai_text_log")
        for i in range(5):
            mod.append_ai_text(self.store, f"c{i}", f"p{i}", f"r{i}", f"t{i}")
        lines = [ln for ln in (self.store / mod._AI_TEXT_LOG_FILENAME).read_text(encoding="utf-8").splitlines() if ln.strip()]
        ids = {json.loads(ln)["id"] for ln in lines}
        self.assertEqual(len(ids), 5)


class TestReadAiTextSince(AiTextLogTestBase):
    def test_read_missing_log_returns_empty(self) -> None:
        mod = importlib.import_module("memory_system.learning.ai_text_log")
        self.assertEqual(mod.read_ai_text_since(self.store, None), [])

    def test_read_since_none_returns_all_in_order(self) -> None:
        mod = importlib.import_module("memory_system.learning.ai_text_log")
        for i in range(3):
            mod.append_ai_text(self.store, f"c{i}", f"p{i}", f"r{i}", f"2026-06-12T12:0{i}:00+00:00")
        out = mod.read_ai_text_since(self.store, None)
        self.assertEqual([r["conversation_id"] for r in out], ["c0", "c1", "c2"])

    def test_read_since_id_returns_strictly_after(self) -> None:
        mod = importlib.import_module("memory_system.learning.ai_text_log")
        for i in range(3):
            mod.append_ai_text(self.store, f"c{i}", f"p{i}", f"r{i}", f"t{i}")
        log_path = self.store / mod._AI_TEXT_LOG_FILENAME
        recs = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        first_id = recs[0]["id"]
        out = mod.read_ai_text_since(self.store, first_id)
        self.assertEqual([r["conversation_id"] for r in out], ["c1", "c2"])

    def test_read_since_unknown_id_replays_all(self) -> None:
        mod = importlib.import_module("memory_system.learning.ai_text_log")
        for i in range(2):
            mod.append_ai_text(self.store, f"c{i}", f"p{i}", f"r{i}", f"t{i}")
        out = mod.read_ai_text_since(self.store, "ffffffffffffffffffffffffffffffff")
        self.assertEqual(len(out), 2)


class TestConcurrentAppend(AiTextLogTestBase):
    def test_concurrent_appends_preserve_all_records(self) -> None:
        mod = importlib.import_module("memory_system.learning.ai_text_log")

        def worker(i: int) -> None:
            mod.append_ai_text(self.store, f"c{i}", f"p{i}", f"r{i}", f"t{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        log_path = self.store / mod._AI_TEXT_LOG_FILENAME
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 20)
        recs = [json.loads(ln) for ln in lines]
        self.assertEqual(len({r["id"] for r in recs}), 20)
        self.assertEqual({r["conversation_id"] for r in recs}, {f"c{i}" for i in range(20)})


class TestObserverIntegration(unittest.TestCase):
    """Observer must sibling-write ai-text-log on every afterAgentResponse."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self._ws: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_observer_aitext_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        self._ws = Path(tempfile.mkdtemp(prefix="memtest_observer_aitext_ws_"))
        (self._ws / ".git").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._ws, ignore_errors=True)
        os.environ.pop("SILLY_MEMORY_HOME", None)

    def test_observer_writes_ai_text_log_on_after_agent_response(self) -> None:
        from memory_system.events import append_event
        from memory_system.events.observer import run_observer
        from memory_system.paths import workspace_store

        ws = self._ws
        append_event(ws, "beforeSubmitPrompt", {"conversation_id": "c1", "prompt": "How do I use Tailwind?"})
        append_event(ws, "afterAgentResponse", {"conversation_id": "c1", "text": "Use the cn() helper from utils."})
        _ = run_observer(ws, force=True)

        store = workspace_store(ws)
        mod = importlib.import_module("memory_system.learning.ai_text_log")
        log_path = store / mod._AI_TEXT_LOG_FILENAME
        self.assertTrue(log_path.exists(), "observer must sibling-write ai-text-log")
        recs = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["conversation_id"], "c1")
        self.assertEqual(recs[0]["prompt"], "How do I use Tailwind?")
        self.assertIn("cn()", recs[0]["response_text"])

    def test_observer_does_not_change_observations_when_agent_responses_disabled(self) -> None:
        """ai-text-log write is independent of observe_agent_responses config."""
        from memory_system.system.config import load_config
        from memory_system.events import append_event
        from memory_system.events.observer import run_observer
        from memory_system.paths import workspace_store

        # Disable observations.md agent-response bullets but still expect ai-text-log capture.
        cfg = load_config()
        cfg_path = Path(self._tmp) / "config.json"
        cfg["observe_agent_responses"] = False
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        ws = self._ws
        append_event(ws, "beforeSubmitPrompt", {"conversation_id": "c9", "prompt": "test prompt"})
        append_event(ws, "afterAgentResponse", {"conversation_id": "c9", "text": "test response"})
        _ = run_observer(ws, force=True)

        store = workspace_store(ws)
        mod = importlib.import_module("memory_system.learning.ai_text_log")
        log_path = store / mod._AI_TEXT_LOG_FILENAME
        self.assertTrue(log_path.exists())
        recs = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["conversation_id"], "c9")


if __name__ == "__main__":
    unittest.main(verbosity=2)
