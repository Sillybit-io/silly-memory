# pyright: reportMissingImports=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportAny=false, reportImplicitOverride=false, reportUnusedCallResult=false

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


class CorrectionDetectorTestCase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self.store: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_corrections_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        self.store = Path(self._tmp) / "ws-corrections"
        self.store.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        os.environ.pop("SILLY_MEMORY_HOME", None)

    def _append_jsonl(self, filename: str, record: dict[str, object]) -> None:
        path = self.store / filename
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _append_ai(self, *, response_text: str, ts: str = "2026-06-12T12:00:00+00:00", prompt: str | None = "make add") -> None:
        record: dict[str, object] = {
            "id": "ai-1",
            "ts": ts,
            "conversation_id": "conv-1",
            "response_text": response_text,
        }
        if prompt is not None:
            record["prompt"] = prompt
        self._append_jsonl("ai-text-log.jsonl", record)

    def _append_edit(self, *, diff: str, ts: str = "2026-06-12T12:02:00+00:00") -> None:
        self._append_jsonl(
            "events.jsonl",
            {
                "id": "edit-1",
                "ts": ts,
                "hook": "afterFileEdit",
                "conversation_id": "conv-1",
                "payload": {"file_path": "/tmp/math.py", "diff": diff},
            },
        )


class TestCorrectionDetector(CorrectionDetectorTestCase):
    def test_detects_divergent_edit_within_default_window(self) -> None:
        from memory_system.learning.correction_detector import detect_corrections

        self._append_ai(response_text="Use `def add(a, b):\n    return a + b` for the helper.")
        self._append_edit(diff="def add(a, b):\n    return a - b\n")

        corrections = detect_corrections(self.store)

        self.assertEqual(len(corrections), 1)
        event = corrections[0]
        self.assertEqual(event.conversation_id, "conv-1")
        self.assertIn("def add", event.ai_response_excerpt)
        self.assertIn("math.py", event.edit_diff_summary)
        self.assertGreater(event.confidence, 0.0)
        persisted = [json.loads(ln) for ln in (self.store / "corrections.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["id"], event.id)

    def test_matching_edit_is_not_a_false_positive(self) -> None:
        from memory_system.learning.correction_detector import detect_corrections

        suggestion = "def add(a, b):\n    return a + b\n"
        self._append_ai(response_text=suggestion)
        self._append_edit(diff=suggestion)

        corrections = detect_corrections(self.store)

        self.assertEqual(corrections, [])
        self.assertFalse((self.store / "corrections.jsonl").exists())

    def test_respects_configured_window(self) -> None:
        from memory_system.learning.correction_detector import detect_corrections

        (Path(self._tmp) / "config.json").write_text(json.dumps({"correction_window_seconds": 60}), encoding="utf-8")
        self._append_ai(response_text="Use `def add(a, b):\n    return a + b`.")
        self._append_edit(diff="def add(a, b):\n    return a - b\n", ts="2026-06-12T12:02:30+00:00")

        corrections = detect_corrections(self.store)

        self.assertEqual(corrections, [])
        self.assertFalse((self.store / "corrections.jsonl").exists())

    def test_missing_prompt_is_handled_gracefully(self) -> None:
        from memory_system.learning.correction_detector import detect_corrections

        self._append_ai(response_text="Use `def add(a, b):\n    return a + b`.", prompt=None)
        self._append_edit(diff="def add(a, b):\n    return a - b\n")

        corrections = detect_corrections(self.store)

        self.assertEqual(len(corrections), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
