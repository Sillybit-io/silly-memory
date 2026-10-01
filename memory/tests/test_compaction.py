# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnusedParameter=false

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


class _SyntheticBackend:
    """Deterministic embedding backend for clustering tests.

    Texts that share a leading "group:<tag>" prefix produce near-identical
    unit vectors (small per-text jitter so they are not bit-equal but cosine
    sim is well above 0.92). Texts in different groups are orthogonal.
    """

    name: ClassVar[str] = "synthetic"
    dim: ClassVar[int] = 64

    @staticmethod
    def _stable_axis(value: str, modulus: int) -> int:
        digest = hashlib.sha1(value.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % modulus

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            tag = text.split(":", 1)[0].strip().lower() if ":" in text else text
            axis = self._stable_axis(tag, self.dim)
            jitter_axis = self._stable_axis(text, self.dim)
            vec = [0.0] * self.dim
            vec[axis] = 1.0
            if jitter_axis != axis:
                vec[jitter_axis] = 0.02
            out.append(vec)
        return out

    def is_available(self) -> bool:
        return True


class _AllZeroBackend:
    name: ClassVar[str] = "zero"
    dim: ClassVar[int] = 4

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dim for _ in texts]

    def is_available(self) -> bool:
        return True


class _CompactionBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.tmp: Path = Path()
        self.bank: Path = Path()

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="compaction-test-"))
        self.bank = self.tmp / "memory-bank"
        self.bank.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_bank(self, filename: str, lines: list[str]) -> Path:
        path = self.bank / filename
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _write_scores(self, bank_file: Path, scores: dict[str, float]) -> None:
        from memory_system.lifecycle.scoring import save_scores

        payload: dict[str, dict[str, object]] = {}
        for entry_id, score in scores.items():
            payload[entry_id] = {
                "id": entry_id,
                "score": float(score),
                "last_accessed_iso": "2026-06-12T00:00:00Z",
                "created_iso": "2026-06-12T00:00:00Z",
                "access_count": 0,
                "corrections_count": 0,
                "reinforcements_count": 0,
            }
        save_scores(bank_file, payload)


class TestCompactionLazyImport(unittest.TestCase):
    """Module-level numpy import is forbidden; must be lazy inside method bodies."""

    def test_numpy_not_imported_at_module_top_level(self) -> None:
        src = (
            Path(__file__).resolve().parents[1]
            / "lib"
            / "memory_system"
            / "lifecycle"
            / "compaction.py"
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


@unittest.skipUnless(HAS_NUMPY, "numpy not installed (optional semantic-recall dependency)")
class TestProposeCompaction(_CompactionBase):
    def test_clusters_near_duplicates_above_threshold(self) -> None:
        """Three near-duplicate entries should form one cluster with 2 fold-ins."""
        from memory_system.lifecycle.compaction import propose_compaction

        self._write_bank(
            "preferences.md",
            [
                "# Preferences",
                "",
                "- cn-helper: Use cn() for class names",
                "- cn-helper: Always use cn() to merge class names",
                "- cn-helper: Use the cn() helper for tailwind classes",
            ],
        )

        clusters = propose_compaction(self.tmp, _SyntheticBackend(), threshold=0.92)

        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertEqual(len(cluster.fold_in_ids) + 1, 3)  # keep + 2 fold-ins
        all_ids = [cluster.keep_id, *cluster.fold_in_ids]
        for entry_id in all_ids:
            self.assertTrue(entry_id.startswith("preferences.md:"))

    def test_dissimilar_entries_produce_no_clusters(self) -> None:
        """Three entries in distinct semantic groups → empty list."""
        from memory_system.lifecycle.compaction import propose_compaction

        self._write_bank(
            "decisions.md",
            [
                "- groupA: alpha decision text",
                "- groupB: beta decision text",
                "- groupC: gamma decision text",
            ],
        )

        clusters = propose_compaction(self.tmp, _SyntheticBackend(), threshold=0.92)
        self.assertEqual(clusters, [])

    def test_threshold_gate_excludes_borderline_pairs(self) -> None:
        """Threshold of 1.5 (impossible for cosine) returns no clusters even on duplicates."""
        from memory_system.lifecycle.compaction import propose_compaction

        self._write_bank(
            "preferences.md",
            [
                "- cn-helper: Use cn() for class names",
                "- cn-helper: Use cn() for class names",
            ],
        )

        clusters = propose_compaction(self.tmp, _SyntheticBackend(), threshold=1.5)
        self.assertEqual(clusters, [])

    def test_keep_id_is_highest_scoring_entry(self) -> None:
        """Within a cluster, the entry with the highest sidecar score wins as keep_id."""
        from memory_system.lifecycle.compaction import propose_compaction

        bank_file = self._write_bank(
            "preferences.md",
            [
                "- cn-helper: variant one",
                "- cn-helper: variant two",
                "- cn-helper: variant three",
            ],
        )
        # Lines 1, 2, 3 → bank ids preferences.md:1, :2, :3
        self._write_scores(
            bank_file,
            {
                "preferences.md:1": 0.30,
                "preferences.md:2": 0.95,  # the winner
                "preferences.md:3": 0.40,
            },
        )

        clusters = propose_compaction(self.tmp, _SyntheticBackend(), threshold=0.92)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].keep_id, "preferences.md:2")
        self.assertEqual(
            sorted(clusters[0].fold_in_ids),
            ["preferences.md:1", "preferences.md:3"],
        )

    def test_default_threshold_is_0_92(self) -> None:
        """Calling without an explicit threshold uses 0.92."""
        from memory_system.lifecycle.compaction import propose_compaction

        self._write_bank(
            "preferences.md",
            ["- cn-helper: a", "- cn-helper: b"],
        )
        # Implicit threshold
        clusters = propose_compaction(self.tmp, _SyntheticBackend())
        self.assertEqual(len(clusters), 1)

    def test_singletons_excluded_from_result(self) -> None:
        """A unique entry alongside a duplicate pair must NOT appear as a 1-element cluster."""
        from memory_system.lifecycle.compaction import propose_compaction

        self._write_bank(
            "mixed.md",
            [
                "- groupA: dup one",
                "- groupA: dup two",
                "- groupB: unique entry",
            ],
        )

        clusters = propose_compaction(self.tmp, _SyntheticBackend(), threshold=0.92)
        self.assertEqual(len(clusters), 1)
        all_ids = {clusters[0].keep_id, *clusters[0].fold_in_ids}
        # The unique groupB entry is not part of the cluster
        self.assertEqual(len(all_ids), 2)
        for entry_id in all_ids:
            self.assertTrue(entry_id.startswith("mixed.md:"))

    def test_empty_bank_returns_empty_list(self) -> None:
        from memory_system.lifecycle.compaction import propose_compaction

        clusters = propose_compaction(self.tmp, _SyntheticBackend(), threshold=0.92)
        self.assertEqual(clusters, [])

    def test_zero_norm_vectors_produce_no_clusters(self) -> None:
        """NoopEmbeddingBackend-style all-zero vectors yield nan cosine → safe empty."""
        from memory_system.lifecycle.compaction import propose_compaction

        self._write_bank(
            "preferences.md",
            ["- one", "- two", "- three"],
        )

        clusters = propose_compaction(self.tmp, _AllZeroBackend(), threshold=0.92)
        self.assertEqual(clusters, [])

    def test_does_not_delete_or_modify_bank_file(self) -> None:
        """Propose-only: original bank file content must remain byte-identical."""
        from memory_system.lifecycle.compaction import propose_compaction

        bank_file = self._write_bank(
            "preferences.md",
            [
                "- cn-helper: variant one",
                "- cn-helper: variant two",
            ],
        )
        before = bank_file.read_bytes()
        _ = propose_compaction(self.tmp, _SyntheticBackend(), threshold=0.92)
        after = bank_file.read_bytes()
        self.assertEqual(before, after)

    def test_cluster_is_json_serializable(self) -> None:
        """CompactionPlan/Cluster is a value object that survives a JSON round-trip."""
        from memory_system.lifecycle.compaction import cluster_to_dict, propose_compaction

        self._write_bank(
            "preferences.md",
            ["- cn-helper: a", "- cn-helper: b", "- cn-helper: c"],
        )
        clusters = propose_compaction(self.tmp, _SyntheticBackend(), threshold=0.92)
        encoded = json.dumps([cluster_to_dict(c) for c in clusters])
        decoded = json.loads(encoded)
        self.assertEqual(len(decoded), 1)
        self.assertIn("keep_id", decoded[0])
        self.assertIn("fold_in_ids", decoded[0])
        self.assertIn("similarity_min", decoded[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
