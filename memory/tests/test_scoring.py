# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnusedImport=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnusedParameter=false

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class ScoringSidecarTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="scoring_test_"))
        self.bank_dir = self._tmp / "memory-bank"
        self.bank_dir.mkdir(parents=True, exist_ok=True)
        self.bank_file = self.bank_dir / "learned-memories.md"
        _ = self.bank_file.write_text("- entry one\n- entry two\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_load_scores_empty_when_no_sidecar(self) -> None:
        from memory_system.lifecycle.scoring import load_scores

        scores = load_scores(self.bank_file)
        self.assertEqual(scores, {})

    def test_sidecar_path_is_adjacent_to_bank_file(self) -> None:
        from memory_system.lifecycle.scoring import save_scores

        save_scores(self.bank_file, {"e1": {"id": "e1", "score": 0.5}})
        sidecar = self.bank_file.parent / f"{self.bank_file.name}.score.json"
        self.assertTrue(sidecar.exists(), f"sidecar not found at {sidecar}")

    def test_save_then_load_round_trip_preserves_data(self) -> None:
        from memory_system.lifecycle.scoring import load_scores, save_scores

        payload = {
            "e1": {
                "id": "e1",
                "score": 0.42,
                "last_accessed_iso": "2026-06-11T00:00:00Z",
                "created_iso": "2026-06-10T00:00:00Z",
                "access_count": 3,
                "corrections_count": 1,
                "reinforcements_count": 2,
            }
        }
        save_scores(self.bank_file, payload)
        loaded = load_scores(self.bank_file)
        self.assertEqual(loaded, payload)

    def test_update_score_creates_entry_with_defaults(self) -> None:
        from memory_system.lifecycle.scoring import load_scores, update_score

        update_score(self.bank_file, "e1", delta=0.1, reason="reinforcement")
        scores = load_scores(self.bank_file)
        entry = scores["e1"]
        self.assertEqual(entry["id"], "e1")
        self.assertAlmostEqual(entry["score"], 0.6)
        self.assertEqual(entry["reinforcements_count"], 1)
        self.assertEqual(entry["corrections_count"], 0)
        self.assertEqual(entry["access_count"], 0)
        self.assertIn("created_iso", entry)
        self.assertIn("last_accessed_iso", entry)

    def test_update_score_correction_reason_increments_correction_count(self) -> None:
        from memory_system.lifecycle.scoring import load_scores, update_score

        update_score(self.bank_file, "e1", delta=-0.2, reason="correction")
        entry = load_scores(self.bank_file)["e1"]
        self.assertAlmostEqual(entry["score"], 0.3)
        self.assertEqual(entry["corrections_count"], 1)
        self.assertEqual(entry["reinforcements_count"], 0)

    def test_update_score_clamps_floor_at_005(self) -> None:
        from memory_system.lifecycle.scoring import load_scores, update_score

        update_score(self.bank_file, "e1", delta=-10.0, reason="correction")
        entry = load_scores(self.bank_file)["e1"]
        self.assertAlmostEqual(entry["score"], 0.05)

    def test_update_score_clamps_ceiling_at_1(self) -> None:
        from memory_system.lifecycle.scoring import load_scores, update_score

        update_score(self.bank_file, "e1", delta=5.0, reason="reinforcement")
        entry = load_scores(self.bank_file)["e1"]
        self.assertAlmostEqual(entry["score"], 1.0)

    def test_update_score_never_deletes_entry(self) -> None:
        from memory_system.lifecycle.scoring import load_scores, update_score

        update_score(self.bank_file, "e1", delta=-10.0, reason="correction")
        update_score(self.bank_file, "e1", delta=-10.0, reason="correction")
        scores = load_scores(self.bank_file)
        self.assertIn("e1", scores)
        self.assertAlmostEqual(scores["e1"]["score"], 0.05)
        self.assertEqual(scores["e1"]["corrections_count"], 2)

    def test_bump_access_touches_timestamp_and_count(self) -> None:
        from memory_system.lifecycle.scoring import bump_access, load_scores, update_score

        update_score(self.bank_file, "e1", delta=0.0, reason="seed")
        first = load_scores(self.bank_file)["e1"]
        first_ts = first["last_accessed_iso"]
        first_count = first["access_count"]

        bump_access(self.bank_file, "e1")
        bump_access(self.bank_file, "e1")
        entry = load_scores(self.bank_file)["e1"]
        self.assertEqual(entry["access_count"], first_count + 2)
        self.assertGreaterEqual(entry["last_accessed_iso"], first_ts)
        self.assertAlmostEqual(entry["score"], first["score"])

    def test_bump_access_creates_default_entry_if_missing(self) -> None:
        from memory_system.lifecycle.scoring import bump_access, load_scores

        bump_access(self.bank_file, "fresh")
        entry = load_scores(self.bank_file)["fresh"]
        self.assertEqual(entry["id"], "fresh")
        self.assertEqual(entry["access_count"], 1)
        self.assertAlmostEqual(entry["score"], 0.5)

    def test_save_scores_uses_atomic_write(self) -> None:
        """No partial temp file remains after save_scores succeeds."""
        from memory_system.lifecycle.scoring import save_scores

        save_scores(self.bank_file, {"e1": {"id": "e1", "score": 0.5}})
        leftovers = [p for p in self.bank_dir.iterdir() if p.name.startswith(f".{self.bank_file.name}.score.json.")]
        self.assertEqual(leftovers, [])

    def test_concurrent_updates_do_not_lose_data(self) -> None:
        """Filelock must prevent lost updates across threads."""
        from memory_system.lifecycle.scoring import load_scores, update_score

        threads = [
            threading.Thread(target=update_score, args=(self.bank_file, "e1"), kwargs={"delta": 0.01, "reason": "reinforcement"})
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entry = load_scores(self.bank_file)["e1"]
        self.assertEqual(entry["reinforcements_count"], 10)
        self.assertAlmostEqual(entry["score"], 0.6, places=5)

    def test_sidecar_is_valid_json(self) -> None:
        from memory_system.lifecycle.scoring import save_scores

        save_scores(self.bank_file, {"e1": {"id": "e1", "score": 0.5}})
        sidecar = self.bank_file.parent / f"{self.bank_file.name}.score.json"
        parsed = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(parsed["e1"]["score"], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
