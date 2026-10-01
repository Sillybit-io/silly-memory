# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnusedImport=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnusedParameter=false

from __future__ import annotations

import datetime as _datetime
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


def _iso(dt: _datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class DecayStepPureTestCase(unittest.TestCase):
    """decay_step is a pure function: dict in, dict out."""

    def test_untouched_entry_decays_toward_floor(self) -> None:
        from memory_system.lifecycle.decay import decay_step

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        old = _iso(now - _datetime.timedelta(days=60))  # 2 half-lives
        scores = {
            "e1": {
                "id": "e1",
                "score": 0.8,
                "last_accessed_iso": old,
                "created_iso": old,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            }
        }
        out = decay_step(scores, now_iso=_iso(now), half_life_days=30.0)
        # Untouched entries decay aggressively. With 2 half-lives + aggressive
        # multiplier the score must be well below the input but >= floor.
        self.assertLess(out["e1"]["score"], 0.8)
        self.assertGreaterEqual(out["e1"]["score"], 0.05)

    def test_reinforced_entry_decays_more_slowly_than_untouched(self) -> None:
        from memory_system.lifecycle.decay import decay_step

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        old = _iso(now - _datetime.timedelta(days=30))  # 1 half-life
        base = {
            "id": "x",
            "score": 0.8,
            "last_accessed_iso": old,
            "created_iso": old,
            "access_count": 0,
            "corrections_count": 0,
            "reinforcements_count": 0,
        }
        untouched = {"u": dict(base, id="u")}
        reinforced = {
            "r": dict(base, id="r", reinforcements_count=5),
        }
        out_u = decay_step(untouched, now_iso=_iso(now), half_life_days=30.0)
        out_r = decay_step(reinforced, now_iso=_iso(now), half_life_days=30.0)
        self.assertGreater(out_r["r"]["score"], out_u["u"]["score"])

    def test_floor_005_never_breached(self) -> None:
        from memory_system.lifecycle.decay import decay_step

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        ancient = _iso(now - _datetime.timedelta(days=10_000))
        scores = {
            "e1": {
                "id": "e1",
                "score": 0.05,
                "last_accessed_iso": ancient,
                "created_iso": ancient,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            }
        }
        out = decay_step(scores, now_iso=_iso(now), half_life_days=30.0)
        self.assertGreaterEqual(out["e1"]["score"], 0.05)

    def test_floor_005_when_starting_higher(self) -> None:
        from memory_system.lifecycle.decay import decay_step

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        ancient = _iso(now - _datetime.timedelta(days=10_000))
        scores = {
            "e1": {
                "id": "e1",
                "score": 1.0,
                "last_accessed_iso": ancient,
                "created_iso": ancient,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            }
        }
        out = decay_step(scores, now_iso=_iso(now), half_life_days=30.0)
        self.assertGreaterEqual(out["e1"]["score"], 0.05)

    def test_access_count_bonus_offsets_decay(self) -> None:
        """Touched entries (access_count > 0) get a small bonus that offsets some decay."""
        from memory_system.lifecycle.decay import decay_step

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        old = _iso(now - _datetime.timedelta(days=30))
        no_access = {
            "n": {
                "id": "n",
                "score": 0.8,
                "last_accessed_iso": old,
                "created_iso": old,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            }
        }
        accessed = {
            "a": dict(no_access["n"], id="a", access_count=20),
        }
        out_n = decay_step(no_access, now_iso=_iso(now), half_life_days=30.0)
        out_a = decay_step(accessed, now_iso=_iso(now), half_life_days=30.0)
        self.assertGreater(out_a["a"]["score"], out_n["n"]["score"])

    def test_zero_days_since_access_barely_decays(self) -> None:
        from memory_system.lifecycle.decay import decay_step

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        scores = {
            "e1": {
                "id": "e1",
                "score": 0.8,
                "last_accessed_iso": _iso(now),
                "created_iso": _iso(now),
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            }
        }
        out = decay_step(scores, now_iso=_iso(now), half_life_days=30.0)
        self.assertAlmostEqual(out["e1"]["score"], 0.8, places=4)

    def test_missing_last_accessed_iso_uses_created_iso(self) -> None:
        from memory_system.lifecycle.decay import decay_step

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        old = _iso(now - _datetime.timedelta(days=30))
        scores = {
            "e1": {
                "id": "e1",
                "score": 0.8,
                "created_iso": old,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            }
        }
        out = decay_step(scores, now_iso=_iso(now), half_life_days=30.0)
        self.assertLess(out["e1"]["score"], 0.8)
        self.assertGreaterEqual(out["e1"]["score"], 0.05)

    def test_decay_never_deletes_entries(self) -> None:
        from memory_system.lifecycle.decay import decay_step

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        ancient = _iso(now - _datetime.timedelta(days=10_000))
        scores = {f"e{i}": {
            "id": f"e{i}",
            "score": 0.2,
            "last_accessed_iso": ancient,
            "created_iso": ancient,
            "access_count": 0,
            "corrections_count": 0,
            "reinforcements_count": 0,
        } for i in range(5)}
        out = decay_step(scores, now_iso=_iso(now), half_life_days=30.0)
        self.assertEqual(set(out.keys()), set(scores.keys()))

    def test_decay_is_pure_does_not_mutate_input(self) -> None:
        from memory_system.lifecycle.decay import decay_step

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        old = _iso(now - _datetime.timedelta(days=30))
        scores = {
            "e1": {
                "id": "e1",
                "score": 0.8,
                "last_accessed_iso": old,
                "created_iso": old,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            }
        }
        original_score = scores["e1"]["score"]
        _ = decay_step(scores, now_iso=_iso(now), half_life_days=30.0)
        self.assertEqual(scores["e1"]["score"], original_score)


class ApplyDecayDiskTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="decay_test_"))
        self.bank_dir = self._tmp / "memory-bank"
        self.bank_dir.mkdir(parents=True, exist_ok=True)
        self.bank_file = self.bank_dir / "learned-memories.md"
        _ = self.bank_file.write_text("- entry one\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_apply_decay_returns_count_of_modified_entries(self) -> None:
        from memory_system.lifecycle.decay import apply_decay
        from memory_system.lifecycle.scoring import save_scores

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        old = _iso(now - _datetime.timedelta(days=60))
        save_scores(self.bank_file, {
            "e1": {
                "id": "e1",
                "score": 0.8,
                "last_accessed_iso": old,
                "created_iso": old,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            },
            "e2": {
                "id": "e2",
                "score": 0.6,
                "last_accessed_iso": old,
                "created_iso": old,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            },
        })
        decayed = apply_decay(self.bank_file, now_iso=_iso(now))
        self.assertEqual(decayed, 2)

    def test_apply_decay_persists_to_sidecar(self) -> None:
        from memory_system.lifecycle.decay import apply_decay
        from memory_system.lifecycle.scoring import load_scores, save_scores

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        old = _iso(now - _datetime.timedelta(days=60))
        save_scores(self.bank_file, {
            "e1": {
                "id": "e1",
                "score": 0.8,
                "last_accessed_iso": old,
                "created_iso": old,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            },
        })
        _ = apply_decay(self.bank_file, now_iso=_iso(now))
        after = load_scores(self.bank_file)
        self.assertLess(after["e1"]["score"], 0.8)
        self.assertGreaterEqual(after["e1"]["score"], 0.05)

    def test_apply_decay_no_sidecar_returns_zero(self) -> None:
        from memory_system.lifecycle.decay import apply_decay

        decayed = apply_decay(self.bank_file)
        self.assertEqual(decayed, 0)

    def test_apply_decay_uses_atomic_write(self) -> None:
        """No partial temp file remains after apply_decay succeeds."""
        from memory_system.lifecycle.decay import apply_decay
        from memory_system.lifecycle.scoring import save_scores

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        old = _iso(now - _datetime.timedelta(days=60))
        save_scores(self.bank_file, {
            "e1": {
                "id": "e1",
                "score": 0.8,
                "last_accessed_iso": old,
                "created_iso": old,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            },
        })
        _ = apply_decay(self.bank_file, now_iso=_iso(now))
        leftovers = [
            p for p in self.bank_dir.iterdir()
            if p.name.startswith(f".{self.bank_file.name}.score.json.")
        ]
        self.assertEqual(leftovers, [])

    def test_apply_decay_floor_005_on_disk(self) -> None:
        """Repeated apply_decay must never drop any entry below the 0.05 floor."""
        from memory_system.lifecycle.decay import apply_decay
        from memory_system.lifecycle.scoring import load_scores, save_scores

        now = _datetime.datetime(2026, 6, 12, tzinfo=_datetime.timezone.utc)
        ancient = _iso(now - _datetime.timedelta(days=10_000))
        save_scores(self.bank_file, {
            "e1": {
                "id": "e1",
                "score": 1.0,
                "last_accessed_iso": ancient,
                "created_iso": ancient,
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            },
        })
        for _ in range(10):
            _ = apply_decay(self.bank_file, now_iso=_iso(now))
        after = load_scores(self.bank_file)
        self.assertIn("e1", after)
        self.assertGreaterEqual(after["e1"]["score"], 0.05)


if __name__ == "__main__":
    unittest.main(verbosity=2)
