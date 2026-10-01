"""TDD tests for recall_baseline harness.

Written BEFORE the implementation per TDD discipline.

Run with:
    PYTHONPATH=memory/lib python3 -m unittest memory.tests.test_recall_baseline
    # or from project root:
    PYTHONPATH=memory/lib python3 -m unittest discover -s memory/tests -p "test_recall_baseline.py"
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class TestRecallBaselineContract(unittest.TestCase):
    """Verifies harness returns the required output shape on synthetic fixtures."""

    @classmethod
    def setUpClass(cls) -> None:
        from memory_system.eval.recall_baseline import _load_fixtures, run_synthetic
        cls._fixtures = _load_fixtures()
        cls._result = run_synthetic(cls._fixtures)

    def test_output_has_mrr_at_10(self) -> None:
        self.assertIn("mrr_at_10", self._result)

    def test_output_has_hit_at_3(self) -> None:
        self.assertIn("hit_at_3", self._result)

    def test_output_has_query_count(self) -> None:
        self.assertIn("query_count", self._result)

    def test_output_has_latency_ms_p50(self) -> None:
        self.assertIn("latency_ms_p50", self._result)

    def test_query_count_is_10(self) -> None:
        self.assertEqual(self._result["query_count"], 10)

    def test_mrr_in_unit_interval(self) -> None:
        self.assertGreaterEqual(self._result["mrr_at_10"], 0.0)
        self.assertLessEqual(self._result["mrr_at_10"], 1.0)

    def test_hit_at_3_in_unit_interval(self) -> None:
        self.assertGreaterEqual(self._result["hit_at_3"], 0.0)
        self.assertLessEqual(self._result["hit_at_3"], 1.0)

    def test_latency_ms_p50_non_negative(self) -> None:
        self.assertGreaterEqual(self._result["latency_ms_p50"], 0.0)

    def test_synthetic_hit_at_3_is_perfect(self) -> None:
        """Synthetic store is seeded with every fixture's keywords — all must hit top-3."""
        self.assertEqual(
            self._result["hit_at_3"],
            1.0,
            "Synthetic store should find each seeded fixture in top-3 results",
        )

    def test_synthetic_mrr_is_perfect(self) -> None:
        """Each query uniquely matches its seeded document — rank-1 always correct."""
        self.assertEqual(
            self._result["mrr_at_10"],
            1.0,
            "Synthetic store should rank each seeded document at position 1",
        )

    def test_result_is_json_serializable(self) -> None:
        serialized = json.dumps(self._result)
        self.assertIsInstance(serialized, str)
        parsed = json.loads(serialized)
        self.assertEqual(parsed["query_count"], 10)


class TestFixtureSchema(unittest.TestCase):
    """Validates baseline_queries.json format and completeness."""

    @classmethod
    def setUpClass(cls) -> None:
        from memory_system.eval.recall_baseline import _load_fixtures
        cls._fixtures = _load_fixtures()

    def test_exactly_10_fixtures(self) -> None:
        self.assertEqual(len(self._fixtures), 10)

    def test_every_fixture_has_query_key(self) -> None:
        for i, f in enumerate(self._fixtures):
            with self.subTest(i=i):
                self.assertIn("query", f)
                self.assertIsInstance(f["query"], str)
                self.assertTrue(f["query"].strip(), f"fixture[{i}]['query'] is empty")

    def test_every_fixture_has_keywords_key(self) -> None:
        for i, f in enumerate(self._fixtures):
            with self.subTest(i=i):
                self.assertIn("expected_memory_keywords", f)
                self.assertIsInstance(f["expected_memory_keywords"], list)
                self.assertGreater(
                    len(f["expected_memory_keywords"]),
                    0,
                    f"fixture[{i}] has empty keywords list",
                )

    def test_all_keywords_are_strings(self) -> None:
        for i, f in enumerate(self._fixtures):
            for j, kw in enumerate(f["expected_memory_keywords"]):
                with self.subTest(i=i, j=j):
                    self.assertIsInstance(kw, str)


if __name__ == "__main__":
    unittest.main()
