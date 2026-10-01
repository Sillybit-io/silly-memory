# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAny=false, reportImplicitOverride=false, reportUnusedCallResult=false

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class _Backend:
    def __init__(self, available: bool = True, response: str = "Can you clarify which category this memory belongs to?") -> None:
        self.available: bool = available
        self.response: str = response
        self.prompts: list[list[str]] = []

    def is_available(self) -> bool:
        return self.available

    def condense(self, observations: list[str]) -> str:
        self.prompts.append(observations)
        return self.response


class ActiveQuestioningTestCase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self.store: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_active_q_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        self.store = Path(self._tmp) / "ws-active-q"
        self.store.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        _ = os.environ.pop("SILLY_MEMORY_HOME", None)


class TestActiveQuestioning(ActiveQuestioningTestCase):
    def test_low_classifier_confidence_triggers_question(self) -> None:
        from memory_system.learning.active_questioning import maybe_active_question

        backend = _Backend(response="Which memory category should this be saved under?")

        question = maybe_active_question(
            self.store,
            session_id="cursor-session-1",
            llm_backend=backend,
            classifier_result=("unknown", 0.21),
            text="ship it maybe as preference or decision",
        )

        self.assertEqual(question, "Which memory category should this be saved under?")
        self.assertEqual(len(backend.prompts), 1)
        self.assertIn("low classifier confidence", backend.prompts[0][0])

    def test_contradiction_signal_triggers_question(self) -> None:
        from memory_system.learning.active_questioning import maybe_active_question

        question = maybe_active_question(
            self.store,
            session_id="cursor-session-1",
            llm_backend=_Backend(response="Should I keep the newer preference or the older one?"),
            contradiction_detected=True,
            text="prefer rest and avoid rest",
        )

        self.assertEqual(question, "Should I keep the newer preference or the older one?")

    def test_repeated_correction_signal_triggers_question(self) -> None:
        from memory_system.learning.active_questioning import maybe_active_question

        question = maybe_active_question(
            self.store,
            session_id="cursor-session-1",
            llm_backend=_Backend(response="What rule should I learn from the repeated corrections?"),
            repeated_correction=True,
            text="corrected twice",
        )

        self.assertEqual(question, "What rule should I learn from the repeated corrections?")

    def test_throttles_to_one_question_per_cursor_session(self) -> None:
        from memory_system.learning.active_questioning import maybe_active_question

        backend = _Backend(response="Clarify?")

        first = maybe_active_question(
            self.store,
            session_id="cursor-session-1",
            llm_backend=backend,
            classifier_result=("unknown", 0.1),
            text="first ambiguous",
        )
        second = maybe_active_question(
            self.store,
            session_id="cursor-session-1",
            llm_backend=backend,
            classifier_result=("unknown", 0.1),
            text="second ambiguous",
        )

        self.assertEqual(first, "Clarify?")
        self.assertIsNone(second)
        self.assertEqual(len(backend.prompts), 1)
        state = cast(dict[str, object], json.loads((self.store / ".active_q_state.json").read_text(encoding="utf-8")))
        self.assertEqual(state["last_question_session_id"], "cursor-session-1")
        self.assertIn("last_question_ts", state)

    def test_option_a_fallback_writes_pending_question_when_backend_unavailable(self) -> None:
        """F1 Option A: backend unavailable + trigger fires → write pending_question.md and return question text."""
        from memory_system.learning.active_questioning import maybe_active_question

        backend = _Backend(available=False)

        question = maybe_active_question(
            self.store,
            session_id="cursor-session-1",
            llm_backend=backend,
            classifier_result=("unknown", 0.1),
            text="ambiguous",
        )

        self.assertIsInstance(question, str)
        assert question is not None
        self.assertGreater(len(question.strip()), 0)
        self.assertEqual(backend.prompts, [])
        pending = self.store / "pending_question.md"
        self.assertTrue(pending.exists(), "Option A must write pending_question.md")
        self.assertIn(question, pending.read_text(encoding="utf-8"))

    def test_option_a_fallback_writes_meta_sidecar(self) -> None:
        """F1 Option A: a structured JSON sidecar must accompany the markdown for memwhy/meminspect."""
        from memory_system.learning.active_questioning import maybe_active_question

        backend = _Backend(available=False)
        question = maybe_active_question(
            self.store,
            session_id="cursor-session-meta",
            llm_backend=backend,
            contradiction_detected=True,
            text="conflicting preference",
        )

        self.assertIsInstance(question, str)
        meta_path = self.store / ".pending_question_meta.json"
        self.assertTrue(meta_path.exists(), "Option A must write .pending_question_meta.json sidecar")
        meta = cast(dict[str, object], json.loads(meta_path.read_text(encoding="utf-8")))
        self.assertIn("ts", meta)
        self.assertIn("reason", meta)
        self.assertIn("question", meta)
        self.assertEqual(meta["question"], question)
        self.assertEqual(meta["reason"], "contradiction detected")

    def test_option_a_fallback_still_throttles_per_session(self) -> None:
        """F1 Option A: second call within the same session must not re-write artifacts."""
        from memory_system.learning.active_questioning import maybe_active_question

        backend = _Backend(available=False)
        first = maybe_active_question(
            self.store,
            session_id="cursor-session-throttle",
            llm_backend=backend,
            classifier_result=("unknown", 0.1),
            text="first ambiguous",
        )
        self.assertIsInstance(first, str)
        pending = self.store / "pending_question.md"
        first_mtime = pending.stat().st_mtime_ns

        second = maybe_active_question(
            self.store,
            session_id="cursor-session-throttle",
            llm_backend=backend,
            classifier_result=("unknown", 0.1),
            text="second ambiguous",
        )
        self.assertIsNone(second)
        self.assertEqual(pending.stat().st_mtime_ns, first_mtime)


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
