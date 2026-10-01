from __future__ import annotations

import json
import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
MEM_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MEM_LIB))


class MemoryTestBase(unittest.TestCase):
    _tmp: str = ""
    _ws: Path = Path(".")

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_home_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        real_cfg = MEM_HOME / "config.json"
        if real_cfg.exists():
            _ = shutil.copy2(real_cfg, Path(self._tmp) / "config.json")
        self._ws = Path(tempfile.mkdtemp(prefix="memtest_ws_"))
        (self._ws / ".git").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._ws, ignore_errors=True)
        _ = os.environ.pop("SILLY_MEMORY_HOME", None)


def _module(name: str):
    return importlib.import_module(name)


class TestEventPathDenylist(MemoryTestBase):
    def _events_path(self) -> Path:
        return _module("memory_system.paths").workspace_store(self._ws) / "events.jsonl"

    def _suppressed_path(self) -> Path:
        return _module("memory_system.paths").workspace_store(self._ws) / "events.jsonl-suppressed.log"

    def test_after_file_edit_under_memory_home_is_suppressed_and_logged(self) -> None:
        memory_home = Path(os.environ["SILLY_MEMORY_HOME"]).resolve()
        linked_home = self._ws / "linked-memory"
        linked_home.symlink_to(memory_home)

        result = _module("memory_system.events").append_event(
            self._ws, "afterFileEdit", {"path": str(linked_home / "nested" / "foo.md")}
        )

        self.assertFalse(result)
        self.assertTrue(self._suppressed_path().exists())
        suppressed = [ln for ln in self._suppressed_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(suppressed), 1)
        record = json.loads(suppressed[0])
        self.assertEqual(record["hook"], "afterFileEdit")
        self.assertIn("linked-memory", record["payload"]["path"])
        self.assertFalse(self._events_path().read_text(encoding="utf-8").strip())

    def test_after_file_edit_under_hooks_is_suppressed_and_logged(self) -> None:
        hook_path = Path.home() / ".cursor" / "hooks" / "memory-hook.sh"

        result = _module("memory_system.events").append_event(self._ws, "afterFileEdit", {"path": str(hook_path)})

        self.assertFalse(result)
        suppressed = [ln for ln in self._suppressed_path().read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(suppressed), 1)
        self.assertIn("hooks", json.loads(suppressed[0])["payload"]["path"])

    def test_after_file_edit_outside_memory_home_is_recorded(self) -> None:
        result = _module("memory_system.events").append_event(self._ws, "afterFileEdit", {"path": "/tmp/somewhere.py"})

        self.assertTrue(result)
        self.assertTrue(self._events_path().exists())
        self.assertIn("/tmp/somewhere.py", self._events_path().read_text(encoding="utf-8"))
        if self._suppressed_path().exists():
            self.assertEqual(self._suppressed_path().read_text(encoding="utf-8").strip(), "")
