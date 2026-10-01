from __future__ import annotations

import ast
import importlib.util
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


class TestVectorStoreLazyImport(unittest.TestCase):
    """Module-level numpy import is forbidden; must be lazy inside methods."""

    def test_numpy_not_imported_at_module_top_level(self) -> None:
        src = (
            Path(__file__).resolve().parents[1]
            / "lib"
            / "memory_system"
            / "storage"
            / "vector_store.py"
        )
        tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
        top: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.append(node.module)
        self.assertNotIn(
            "numpy", top, "numpy must be lazy-imported inside method bodies"
        )

    def test_module_imports_without_numpy_in_sys_modules(self) -> None:
        # If numpy was already imported by a previous test we cannot unload it
        # safely, but we *can* assert it is not pulled in by the vector_store
        # module itself when freshly re-imported via importlib.
        import importlib

        name = "memory_system.storage.vector_store"
        loaded = sys.modules.pop(name, None)
        try:
            before = "numpy" in sys.modules
            importlib.import_module(name)
            after = "numpy" in sys.modules
        finally:
            # Other modules hold the original; keep one copy for the rest of the run.
            if loaded is not None:
                sys.modules[name] = loaded
                setattr(sys.modules["memory_system.storage"], "vector_store", loaded)
        # Only meaningful if numpy wasn't already loaded.
        if not before:
            self.assertFalse(after, "importing vector_store must not load numpy")


@unittest.skipUnless(HAS_NUMPY, "numpy not installed (optional semantic-recall dependency)")
class _RoundTripBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.tmp: Path = Path()

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vs-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestVectorStoreRoundTrip(_RoundTripBase):
    def test_add_then_search_returns_exact_match_at_rank_0(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        vs.add("a", [1.0, 0.0, 0.0])
        vs.add("b", [0.0, 1.0, 0.0])
        vs.add("c", [0.0, 0.0, 1.0])

        hits = vs.search([1.0, 0.0, 0.0], k=3)
        self.assertEqual(hits[0][0], "a")
        self.assertAlmostEqual(hits[0][1], 1.0, places=5)

    def test_search_empty_store_returns_empty_list(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        hits = vs.search([1.0, 0.0, 0.0], k=10)
        self.assertEqual(hits, [])

    def test_remove_drops_entry_from_subsequent_search(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        vs.add("a", [1.0, 0.0])
        vs.add("b", [0.0, 1.0])
        vs.remove("a")

        hits = vs.search([1.0, 0.0], k=2)
        returned_ids = [h[0] for h in hits]
        self.assertNotIn("a", returned_ids)
        self.assertIn("b", returned_ids)

    def test_remove_unknown_id_is_noop(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        vs.add("a", [1.0, 0.0])
        vs.remove("does-not-exist")  # must not raise
        hits = vs.search([1.0, 0.0], k=1)
        self.assertEqual(hits[0][0], "a")

    def test_persistence_across_instances(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs1 = VectorStore(self.tmp)
        vs1.add("x", [0.6, 0.8])
        del vs1

        vs2 = VectorStore(self.tmp)
        hits = vs2.search([0.6, 0.8], k=1)
        self.assertEqual(hits[0][0], "x")
        self.assertAlmostEqual(hits[0][1], 1.0, places=5)

    def test_overwrite_existing_entry_id(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        vs.add("a", [1.0, 0.0])
        vs.add("a", [0.0, 1.0])  # overwrite
        hits = vs.search([0.0, 1.0], k=2)
        # Only one row stored, still ranked first
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], "a")

    def test_files_created_in_store_dir(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        vs.add("a", [1.0, 0.0, 0.0])
        self.assertTrue((self.tmp / "vectors.npy").exists())
        self.assertTrue((self.tmp / "vectors.index.json").exists())
        # No leftover .partial after a clean write
        leftovers = list(self.tmp.glob("vectors.npy.partial*"))
        self.assertEqual(leftovers, [])

    def test_index_json_is_valid_json_with_ids_array(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        vs.add("a", [1.0, 0.0])
        vs.add("b", [0.0, 1.0])
        payload = json.loads((self.tmp / "vectors.index.json").read_text("utf-8"))
        self.assertEqual(payload["ids"], ["a", "b"])
        self.assertEqual(payload["dim"], 2)


class TestVectorStoreCosineSimilarity(_RoundTripBase):
    """Hand-computed cosine similarity expected values."""

    def test_cosine_similarity_matches_hand_computation(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        vs.add("axis", [1.0, 0.0, 0.0])
        vs.add("diag", [1.0, 1.0, 0.0])
        vs.add("ortho", [0.0, 1.0, 0.0])

        hits = vs.search([1.0, 0.0, 0.0], k=3)
        scores = {h[0]: h[1] for h in hits}

        # cosine(axis, query) = (1*1) / (1 * 1) = 1.0
        self.assertAlmostEqual(scores["axis"], 1.0, places=5)
        # cosine(diag, query) = 1 / (sqrt(2) * 1) = 1/sqrt(2)
        self.assertAlmostEqual(scores["diag"], 1.0 / math.sqrt(2.0), places=5)
        # cosine(ortho, query) = 0 / (1 * 1) = 0.0
        self.assertAlmostEqual(scores["ortho"], 0.0, places=5)

    def test_results_sorted_descending(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        vs.add("a", [1.0, 0.0])
        vs.add("b", [0.7071, 0.7071])
        vs.add("c", [0.0, 1.0])

        hits = vs.search([1.0, 0.0], k=3)
        scores = [h[1] for h in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_k_caps_results(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        for i in range(5):
            vs.add(f"id{i}", [float(i) + 1.0, 1.0])
        hits = vs.search([1.0, 1.0], k=2)
        self.assertEqual(len(hits), 2)

    def test_zero_query_vector_returns_empty(self) -> None:
        from memory_system.storage.vector_store import VectorStore

        vs = VectorStore(self.tmp)
        vs.add("a", [1.0, 0.0])
        hits = vs.search([0.0, 0.0], k=1)
        self.assertEqual(hits, [])


class TestRebuildFromBank(_RoundTripBase):
    def setUp(self) -> None:
        super().setUp()
        (self.tmp / "memory-bank").mkdir(parents=True, exist_ok=True)

    def test_rebuild_from_bank_indexes_bullet_lines(self) -> None:
        from memory_system.backends.embedding.noop_backend import (
            NoopEmbeddingBackend,
        )
        from memory_system.storage.vector_store import rebuild_from_bank

        bank = self.tmp / "memory-bank" / "preferences.md"
        bank.write_text(
            "# Preferences\n\n"
            "- Likes dark mode\n"
            "- Prefers vim\n"
            "- Uses tabs not spaces\n",
            encoding="utf-8",
        )

        backend = NoopEmbeddingBackend()
        added = rebuild_from_bank(self.tmp, backend)
        self.assertEqual(added, 3)

        payload = json.loads((self.tmp / "vectors.index.json").read_text("utf-8"))
        self.assertEqual(len(payload["ids"]), 3)
        self.assertEqual(payload["dim"], 384)
        # Entry ids carry source provenance
        for entry_id in payload["ids"]:
            self.assertTrue(entry_id.startswith("preferences.md:"))

    def test_rebuild_from_bank_with_no_bank_dir_returns_zero(self) -> None:
        from memory_system.backends.embedding.noop_backend import (
            NoopEmbeddingBackend,
        )
        from memory_system.storage.vector_store import rebuild_from_bank

        # No memory-bank dir at all
        empty = Path(tempfile.mkdtemp(prefix="vs-empty-"))
        try:
            added = rebuild_from_bank(empty, NoopEmbeddingBackend())
            self.assertEqual(added, 0)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_rebuild_replaces_existing_entries(self) -> None:
        from memory_system.backends.embedding.noop_backend import (
            NoopEmbeddingBackend,
        )
        from memory_system.storage.vector_store import VectorStore, rebuild_from_bank

        # Seed an unrelated entry
        vs = VectorStore(self.tmp)
        vs.add("stale", [1.0] + [0.0] * 383)

        bank = self.tmp / "memory-bank" / "decisions.md"
        bank.write_text("- Decision one\n- Decision two\n", encoding="utf-8")

        rebuild_from_bank(self.tmp, NoopEmbeddingBackend())
        payload = json.loads((self.tmp / "vectors.index.json").read_text("utf-8"))
        self.assertNotIn("stale", payload["ids"])
        self.assertEqual(len(payload["ids"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
