from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))

from memory.tests.test_memory import MemoryTestBase


class TestPromoteConcurrency(MemoryTestBase):
    def test_simultaneous_promote_staging_preserves_unique_lines(self) -> None:
        from memory_system.system.config import load_config
        from memory_system.recall.distiller import promote_staging
        from memory_system.paths import bank_path, ensure_layout, workspace_store

        store = workspace_store(self._ws)
        ensure_layout(store)
        cfg = load_config()
        staging = store / "staging" / "pending.jsonl"
        staging.write_text(
            "\n".join(
                json.dumps(item)
                for item in (
                    {"category": "action-item", "body": "ship catalog", "confidence": 0.99},
                    {"category": "decision", "body": "defer limit orders", "confidence": 0.99},
                )
            )
            + "\n",
            encoding="utf-8",
        )

        action_file = bank_path(store, cfg.get("workspace_bank_routes", {}).get("action-item", "activeContext.md"))
        decision_file = bank_path(store, cfg.get("workspace_bank_routes", {}).get("decision", "activeContext.md"))

        original_atomic_write = None
        start = threading.Barrier(3)
        results: list[int] = []

        def slow_atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
            if path.name != "pending.jsonl":
                time.sleep(0.05)
            assert original_atomic_write is not None
            return original_atomic_write(path, content, mode)

        def worker() -> None:
            start.wait()
            results.append(promote_staging(self._ws))

        from memory_system.recall import distiller
        original_atomic_write = getattr(distiller, "atomic_write")
        with patch.object(distiller, "atomic_write", side_effect=slow_atomic_write):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            start.wait()
            t1.join()
            t2.join()

        action_text = action_file.read_text(encoding="utf-8")
        decision_text = decision_file.read_text(encoding="utf-8")
        self.assertEqual(action_text.count("ship catalog"), 1)
        self.assertEqual(decision_text.count("defer limit orders"), 1)
        self.assertFalse((staging.read_text(encoding="utf-8")).strip())
        self.assertEqual(sorted(results), [0, 2])
