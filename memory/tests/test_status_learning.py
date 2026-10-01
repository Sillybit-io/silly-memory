# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false, reportUnknownParameterType=false

"""Tests for the Learning Status section appended to ``memory status``.

T29 adds a "Learning Status" section to the existing ``render_status`` output
without disturbing any pre-existing dashboard sections. The block surfaces:

* per-bank score distribution (<0.3 decay candidates / 0.3-0.7 / >0.7)
* total corrections + total reinforcements counts (from sidecars)
* recent contradictions count (last 24h, computed live)
* last AI-text-log timestamp (from ``ai-text-log.jsonl``)

Each test pins one of those four behaviors so they cannot regress.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
MEM_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MEM_LIB))


class StatusLearningTestBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self._ws: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_status_learning_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        real_cfg = MEM_HOME / "config.json"
        if real_cfg.exists():
            shutil.copy2(real_cfg, Path(self._tmp) / "config.json")
        self._ws = Path(tempfile.mkdtemp(prefix="memtest_status_ws_"))
        (self._ws / ".git").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._ws, ignore_errors=True)
        os.environ.pop("SILLY_MEMORY_HOME", None)

    def _store(self) -> Path:
        from memory_system.paths import ensure_layout, workspace_store

        store = workspace_store(self._ws)
        ensure_layout(store)
        return store

    def _write_bank(self, store: Path, filename: str, body: str) -> Path:
        from memory_system.paths import bank_path

        p = bank_path(store, filename)
        p.write_text(body, encoding="utf-8")
        return p

    def _write_sidecar(self, bank_file: Path, scores: dict) -> None:
        sidecar = bank_file.parent / f"{bank_file.name}.score.json"
        sidecar.write_text(json.dumps(scores, indent=2), encoding="utf-8")


class TestLearningSectionAppended(StatusLearningTestBase):
    def test_learning_status_section_appended_with_empty_store(self) -> None:
        """Fresh store: section renders 'not yet recorded' rather than crashing."""
        from memory_system.status import render_status

        _ = self._store()
        out = render_status(self._ws)
        self.assertIn("Learning Status", out)
        # Pre-existing dashboard markers must remain untouched.
        self.assertIn("Capture (raw \u2192 derived)", out)
        self.assertIn("Durable memory \u2014 workspace bank", out)
        self.assertIn("Injected context pack", out)
        # Empty-store defaults must not raise: zeros / placeholders only.
        self.assertIn("corrections", out.lower())
        self.assertIn("reinforcements", out.lower())
        self.assertIn("contradictions", out.lower())

    def test_learning_section_appears_after_existing_sections(self) -> None:
        from memory_system.status import render_status

        _ = self._store()
        out = render_status(self._ws)
        injected_idx = out.find("Injected context pack")
        learning_idx = out.find("Learning Status")
        self.assertGreater(injected_idx, -1)
        self.assertGreater(learning_idx, -1)
        self.assertGreater(
            learning_idx,
            injected_idx,
            "Learning Status must be appended AFTER the existing dashboard",
        )


class TestScoreDistribution(StatusLearningTestBase):
    def test_score_distribution_counts_low_mid_high(self) -> None:
        from memory_system.status import render_status

        store = self._store()
        bank_file = self._write_bank(
            store,
            "learned-memories.md",
            "# Learned\n\n- entry a\n- entry b\n- entry c\n- entry d\n",
        )
        # 1 low (<0.3), 2 mid (0.3-0.7), 1 high (>0.7)
        self._write_sidecar(
            bank_file,
            {
                "learned-memories.md:3": {"id": "learned-memories.md:3", "score": 0.10},
                "learned-memories.md:4": {"id": "learned-memories.md:4", "score": 0.45},
                "learned-memories.md:5": {"id": "learned-memories.md:5", "score": 0.65},
                "learned-memories.md:6": {"id": "learned-memories.md:6", "score": 0.90},
            },
        )
        out = render_status(self._ws)
        # Expect a row showing low/mid/high counts for that bank.
        self.assertIn("learned-memories.md", out)
        # The format: "low:1 mid:2 high:1" (counts present and bank named).
        self.assertIn("low:1", out)
        self.assertIn("mid:2", out)
        self.assertIn("high:1", out)


class TestReinforcementCounts(StatusLearningTestBase):
    def test_sums_corrections_and_reinforcements_from_sidecars(self) -> None:
        from memory_system.status import render_status

        store = self._store()
        bf1 = self._write_bank(store, "learned-memories.md", "# A\n\n- one\n")
        bf2 = self._write_bank(store, "actionItems.md", "# B\n\n- two\n")
        self._write_sidecar(
            bf1,
            {
                "learned-memories.md:3": {
                    "id": "learned-memories.md:3",
                    "score": 0.5,
                    "corrections_count": 2,
                    "reinforcements_count": 3,
                },
            },
        )
        self._write_sidecar(
            bf2,
            {
                "actionItems.md:3": {
                    "id": "actionItems.md:3",
                    "score": 0.5,
                    "corrections_count": 1,
                    "reinforcements_count": 4,
                },
            },
        )
        out = render_status(self._ws)
        # Totals from sidecars: 3 corrections, 7 reinforcements.
        self.assertIn("corrections applied: 3", out)
        self.assertIn("reinforcements applied: 7", out)


class TestContradictionsLive(StatusLearningTestBase):
    def test_contradictions_count_reflects_detected_pairs(self) -> None:
        from memory_system.status import render_status

        store = self._store()
        # Tagged bullets that the rule-based detector reliably flags as
        # contradictory (shared #pref tag, opposing polarity, same topic).
        _ = self._write_bank(
            store,
            "learned-memories.md",
            "# Learned\n\n"
            "- #pref: prefer pytest for python testing\n"
            "- #pref: avoid pytest for python testing\n",
        )
        out = render_status(self._ws)
        # The line should report at least one contradiction.
        self.assertRegex(out, r"contradictions[^\n]*:\s*[1-9]")


class TestAITextLogTimestamp(StatusLearningTestBase):
    def test_last_ai_text_log_timestamp_shown(self) -> None:
        from memory_system.status import render_status

        store = self._store()
        log = store / "ai-text-log.jsonl"
        log.write_text(
            json.dumps(
                {
                    "id": "abc",
                    "ts": "2026-06-12T15:30:00Z",
                    "conversation_id": "c",
                    "prompt": "p",
                    "response_text": "r",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        out = render_status(self._ws)
        self.assertIn("AI-text log", out)
        self.assertIn("2026-06-12", out)

    def test_missing_ai_text_log_shows_placeholder(self) -> None:
        from memory_system.status import render_status

        _ = self._store()
        out = render_status(self._ws)
        self.assertIn("AI-text log", out)
        # Placeholder rather than a crash or blank.
        self.assertIn("not yet recorded", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
