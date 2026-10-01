# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnusedParameter=false

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


class _UnavailableBackend:
    dim = 2

    def is_available(self) -> bool:
        return False

    def encode(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("fallback must not encode when backend is unavailable")


class _MappingBackend:
    dim = 2

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping

    def is_available(self) -> bool:
        return True

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._mapping[text] for text in texts]


class HybridRecallTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hybrid-recall-test-"))
        self.store = self.tmp / "store"
        self.bank = self.store / "memory-bank"
        self.bank.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_bank_file(self, name: str, lines: list[str]) -> Path:
        path = self.bank / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _rebuild_fts(self) -> None:
        from memory_system.index import rebuild_index

        rebuild_index(self.store, self.store.name)

    def test_falls_back_to_fts_only_when_backend_unavailable(self) -> None:
        self._write_bank_file(
            "learned-memories.md",
            ["## Preferences", "", "- Alpha recall preference uses dark mode"],
        )
        self._rebuild_fts()

        from memory_system.recall.recall_hybrid import hybrid_recall

        with self.assertLogs("memory_system.recall.recall_hybrid", level=logging.WARNING) as logs:
            results = hybrid_recall(
                self.store,
                "Alpha recall preference",
                backend=_UnavailableBackend(),
                limit=5,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["path"], "learned-memories.md")
        self.assertEqual(results[0]["score"], 1.0)
        self.assertEqual(results[0]["signals"]["dense"], 0.0)
        self.assertIn("falling back to FTS5-only", "\n".join(logs.output))

    @unittest.skipUnless(HAS_NUMPY, "numpy not installed (optional semantic-recall dependency)")
    def test_dense_signal_can_promote_semantic_match_over_fts_only_hit(self) -> None:
        self._write_bank_file("semantic.md", ["- Rover database migration playbook"])
        self._write_bank_file("lexical.md", ["- Alpha placeholder unrelated note"])
        self._rebuild_fts()

        from memory_system.lifecycle.scoring import save_scores
        from memory_system.recall.recall_hybrid import hybrid_recall
        from memory_system.storage.vector_store import VectorStore

        VectorStore(self.store).add("semantic.md:1", [1.0, 0.0])
        VectorStore(self.store).add("lexical.md:1", [0.0, 1.0])
        save_scores(self.bank / "semantic.md", {"semantic.md:1": {"id": "semantic.md:1", "score": 1.0}})
        save_scores(self.bank / "lexical.md", {"lexical.md:1": {"id": "lexical.md:1", "score": 0.1}})

        results = hybrid_recall(
            self.store,
            "Alpha",
            backend=_MappingBackend({"Alpha": [1.0, 0.0]}),
            limit=2,
        )

        self.assertGreaterEqual(len(results), 2)
        self.assertEqual(results[0]["entry_id"], "semantic.md:1")
        self.assertGreater(results[0]["signals"]["dense"], results[1]["signals"]["dense"])
        self.assertGreaterEqual(results[0]["score"], results[1]["score"])

    @unittest.skipUnless(HAS_NUMPY, "numpy not installed (optional semantic-recall dependency)")
    def test_configured_weights_can_make_sidecar_score_decisive(self) -> None:
        self._write_bank_file("low.md", ["- Alpha low value lexical hit"])
        self._write_bank_file("high.md", ["- Alpha high value lexical hit"])
        self._rebuild_fts()

        (self.store / "config.json").write_text(
            json.dumps({"hybrid_recall_weights": {"fts5": 0.0, "dense": 0.0, "score": 1.0}}),
            encoding="utf-8",
        )

        from memory_system.lifecycle.scoring import save_scores
        from memory_system.recall.recall_hybrid import hybrid_recall
        from memory_system.storage.vector_store import VectorStore

        save_scores(self.bank / "low.md", {"low.md:1": {"id": "low.md:1", "score": 0.1}})
        save_scores(self.bank / "high.md", {"high.md:1": {"id": "high.md:1", "score": 1.0}})
        VectorStore(self.store).add("low.md:1", [1.0, 0.0])
        VectorStore(self.store).add("high.md:1", [1.0, 0.0])

        results = hybrid_recall(
            self.store,
            "Alpha lexical hit",
            backend=_MappingBackend({"Alpha lexical hit": [1.0, 0.0]}),
            limit=2,
        )

        self.assertEqual(results[0]["entry_id"], "high.md:1")

    @unittest.skipUnless(HAS_NUMPY, "numpy not installed (optional semantic-recall dependency)")
    def test_10k_synthetic_store_latency_under_200ms_with_available_backend(self) -> None:
        self._write_bank_file("needle.md", ["- Alpha target synthetic memory"])
        self._rebuild_fts()

        import numpy

        from memory_system.recall.recall_hybrid import hybrid_recall

        vectors = numpy.zeros((10000, 2), dtype=numpy.float32)
        vectors[0] = [1.0, 0.0]
        vectors[1:, 1] = 1.0
        with (self.store / "vectors.npy").open("wb") as fh:
            numpy.save(fh, vectors)
        ids = ["needle.md:1"] + [f"noise.md:{i}" for i in range(9999)]
        (self.store / "vectors.index.json").write_text(
            json.dumps({"dim": 2, "ids": ids}),
            encoding="utf-8",
        )

        backend = _MappingBackend({"Alpha target": [1.0, 0.0]})
        latencies: list[float] = []
        for _ in range(5):
            t0 = time.perf_counter()
            results = hybrid_recall(self.store, "Alpha target", backend=backend, limit=10)
            latencies.append((time.perf_counter() - t0) * 1000)
            self.assertEqual(results[0]["entry_id"], "needle.md:1")

        latencies.sort()
        self.assertLessEqual(latencies[len(latencies) // 2], 200.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
