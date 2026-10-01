# pyright: reportMissingImports=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportAny=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportUnknownParameterType=false

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class ReinforcementTestCase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self.store: Path = Path()
        self.bank_file: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_reinforce_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        self.store = Path(self._tmp) / "ws-reinforce"
        (self.store / "memory-bank").mkdir(parents=True)
        self.bank_file = self.store / "memory-bank" / "learned-memories.md"
        # Line layout (1-indexed):
        #   1: "# Learned Memories"
        #   2: ""
        #   3: "- Use def add(a, b): return a + b helper."
        #   4: "- Cats are reptiles forever."
        self.bank_file.write_text(
            "# Learned Memories\n\n"
            "- Use def add(a, b): return a + b helper.\n"
            "- Cats are reptiles forever.\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        os.environ.pop("SILLY_MEMORY_HOME", None)

    def _append_correction(
        self,
        *,
        event_id: str,
        ai_excerpt: str,
        edit_summary: str = "math.py: return a - b",
    ) -> None:
        path = self.store / "corrections.jsonl"
        record = {
            "id": event_id,
            "ts": "2026-06-12T12:02:00+00:00",
            "conversation_id": "conv-1",
            "ai_response_excerpt": ai_excerpt,
            "edit_diff_summary": edit_summary,
            "confidence": 0.7,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


class TestApplyCorrection(ReinforcementTestCase):
    def test_demotes_matching_entry(self) -> None:
        from memory_system.learning.reinforcement import apply_correction
        from memory_system.lifecycle.scoring import load_scores

        event = {
            "id": "c1",
            "ts": "2026-06-12T12:02:00+00:00",
            "conversation_id": "conv-1",
            "ai_response_excerpt": "def add(a, b): return a + b helper for math",
            "edit_diff_summary": "math.py: return a - b",
            "confidence": 0.7,
        }

        demoted = apply_correction(self.store, event)

        self.assertEqual(demoted, ["learned-memories.md:3"])
        scores = load_scores(self.bank_file)
        entry = scores["learned-memories.md:3"]
        self.assertAlmostEqual(entry["score"], 0.4)
        self.assertEqual(entry["corrections_count"], 1)

    def test_no_match_returns_empty_and_writes_no_sidecar(self) -> None:
        from memory_system.learning.reinforcement import apply_correction
        from memory_system.lifecycle.scoring import load_scores

        event = {
            "id": "c2",
            "ai_response_excerpt": "unrelated topic about quantum chromodynamics zxcvbn",
            "edit_diff_summary": "",
            "confidence": 0.7,
        }
        demoted = apply_correction(self.store, event)
        self.assertEqual(demoted, [])
        self.assertEqual(load_scores(self.bank_file), {})

    def test_empty_excerpt_is_noop(self) -> None:
        from memory_system.learning.reinforcement import apply_correction
        from memory_system.lifecycle.scoring import load_scores

        demoted = apply_correction(self.store, {"id": "c3", "ai_response_excerpt": "   "})

        self.assertEqual(demoted, [])
        self.assertEqual(load_scores(self.bank_file), {})

    def test_floor_respected_on_repeated_demotion(self) -> None:
        from memory_system.learning.reinforcement import apply_correction
        from memory_system.lifecycle.scoring import load_scores, update_score

        # Drive entry near the floor before further correction.
        update_score(self.bank_file, "learned-memories.md:3", delta=-0.4, reason="seed")
        event = {
            "id": "c4",
            "ai_response_excerpt": "def add(a, b): return a + b helper",
            "edit_diff_summary": "",
        }
        apply_correction(self.store, event)
        entry = load_scores(self.bank_file)["learned-memories.md:3"]
        self.assertAlmostEqual(entry["score"], 0.05)


class TestApplyReinforcement(ReinforcementTestCase):
    def test_default_magnitude_boosts_score(self) -> None:
        from memory_system.learning.reinforcement import apply_reinforcement
        from memory_system.lifecycle.scoring import load_scores

        apply_reinforcement(self.store, "learned-memories.md:3")

        entry = load_scores(self.bank_file)["learned-memories.md:3"]
        self.assertAlmostEqual(entry["score"], 0.55)
        self.assertEqual(entry["reinforcements_count"], 1)
        self.assertEqual(entry["corrections_count"], 0)

    def test_custom_magnitude(self) -> None:
        from memory_system.learning.reinforcement import apply_reinforcement
        from memory_system.lifecycle.scoring import load_scores

        apply_reinforcement(self.store, "learned-memories.md:3", magnitude=0.2)

        entry = load_scores(self.bank_file)["learned-memories.md:3"]
        self.assertAlmostEqual(entry["score"], 0.7)

    def test_ceiling_respected(self) -> None:
        from memory_system.learning.reinforcement import apply_reinforcement
        from memory_system.lifecycle.scoring import load_scores

        apply_reinforcement(self.store, "learned-memories.md:3", magnitude=5.0)

        entry = load_scores(self.bank_file)["learned-memories.md:3"]
        self.assertAlmostEqual(entry["score"], 1.0)

    def test_missing_bank_file_is_silent_noop(self) -> None:
        from memory_system.learning.reinforcement import apply_reinforcement
        from memory_system.lifecycle.scoring import load_scores

        apply_reinforcement(self.store, "nonexistent-bank.md:99")

        # The known bank file's sidecar remains untouched.
        self.assertEqual(load_scores(self.bank_file), {})


class TestReplayCorrections(ReinforcementTestCase):
    def test_processes_all_events_on_first_run(self) -> None:
        from memory_system.learning.reinforcement import replay_corrections
        from memory_system.lifecycle.scoring import load_scores

        self._append_correction(event_id="c1", ai_excerpt="def add(a, b): return a + b helper")
        self._append_correction(event_id="c2", ai_excerpt="cats are reptiles forever fact")

        processed = replay_corrections(self.store)

        self.assertEqual(processed, 2)
        scores = load_scores(self.bank_file)
        self.assertEqual(scores["learned-memories.md:3"]["corrections_count"], 1)
        self.assertEqual(scores["learned-memories.md:4"]["corrections_count"], 1)

    def test_idempotent_second_run_is_noop(self) -> None:
        from memory_system.learning.reinforcement import replay_corrections
        from memory_system.lifecycle.scoring import load_scores

        self._append_correction(event_id="c1", ai_excerpt="def add(a, b): return a + b helper")
        first = replay_corrections(self.store)
        first_scores = json.dumps(load_scores(self.bank_file), sort_keys=True)

        second = replay_corrections(self.store)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        second_scores = json.dumps(load_scores(self.bank_file), sort_keys=True)
        self.assertEqual(first_scores, second_scores)

    def test_cursor_persisted_to_state_file(self) -> None:
        from memory_system.learning.reinforcement import replay_corrections

        self._append_correction(event_id="c1", ai_excerpt="def add(a, b): return a + b")
        self._append_correction(event_id="c2", ai_excerpt="cats are reptiles forever")
        replay_corrections(self.store)

        state_path = self.store / ".reinforcement_state.json"
        self.assertTrue(state_path.exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_event_id"], "c2")

    def test_since_id_overrides_cursor(self) -> None:
        from memory_system.learning.reinforcement import replay_corrections
        from memory_system.lifecycle.scoring import load_scores

        self._append_correction(event_id="c1", ai_excerpt="def add(a, b): return a + b helper")
        self._append_correction(event_id="c2", ai_excerpt="cats are reptiles forever fact")

        processed = replay_corrections(self.store, since_id="c1")

        # Only events strictly after c1 should be applied.
        self.assertEqual(processed, 1)
        scores = load_scores(self.bank_file)
        self.assertNotIn("learned-memories.md:3", scores)
        self.assertEqual(scores["learned-memories.md:4"]["corrections_count"], 1)

    def test_replay_resumes_after_persisted_cursor(self) -> None:
        from memory_system.learning.reinforcement import replay_corrections
        from memory_system.lifecycle.scoring import load_scores

        self._append_correction(event_id="c1", ai_excerpt="def add(a, b): return a + b helper")
        replay_corrections(self.store)
        self._append_correction(event_id="c2", ai_excerpt="cats are reptiles forever fact")

        processed = replay_corrections(self.store)

        self.assertEqual(processed, 1)
        scores = load_scores(self.bank_file)
        self.assertEqual(scores["learned-memories.md:3"]["corrections_count"], 1)
        self.assertEqual(scores["learned-memories.md:4"]["corrections_count"], 1)

    def test_empty_corrections_returns_zero(self) -> None:
        from memory_system.learning.reinforcement import replay_corrections

        self.assertEqual(replay_corrections(self.store), 0)
        self.assertFalse((self.store / ".reinforcement_state.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
