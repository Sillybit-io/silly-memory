"""Smoke tests for memory.tests.perf_harness.PerfHarness."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from memory.tests.perf_harness import PerfHarness


class PerfHarnessSmokeTests(unittest.TestCase):
    def test_noop_measure_shape(self):
        result = PerfHarness("noop", 50).measure(lambda: None)
        required = {
            "name",
            "n",
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "mean_ms",
            "stdev_ms",
            "cov_pct",
            "budget_ms",
            "status",
        }
        self.assertTrue(
            required.issubset(result.keys()),
            f"missing keys: {required - set(result.keys())}",
        )
        self.assertGreaterEqual(result["p95_ms"], 0)
        self.assertEqual(result["name"], "noop")
        self.assertEqual(result["budget_ms"], 50)

    def test_assert_within_budget_rejects_slow(self):
        harness = PerfHarness("slow", 1, n=5, warmup=1)
        with self.assertRaises(AssertionError):
            harness.assert_within_budget(lambda: time.sleep(0.01))

    def test_record_run_never_writes_into_cwd(self):
        """Perf runs used to create .omo/evidence/ in whatever dir the suite ran from."""
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as tmp:
            prev_cwd = os.getcwd()
            prev_env = os.environ.pop("MEMORY_PERF_RESULTS", None)
            prev_tmpdir = tempfile.tempdir
            tempfile.tempdir = tmp
            os.chdir(cwd)
            try:
                out = PerfHarness("noop", 50).record_run({"name": "noop"})
            finally:
                os.chdir(prev_cwd)
                tempfile.tempdir = prev_tmpdir
                if prev_env is not None:
                    os.environ["MEMORY_PERF_RESULTS"] = prev_env
            self.assertEqual(os.listdir(cwd), [])
            self.assertTrue(out.resolve().is_relative_to(Path(tmp).resolve()))

    def test_record_run_honors_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "perf.jsonl"
            prev_env = os.environ.get("MEMORY_PERF_RESULTS")
            os.environ["MEMORY_PERF_RESULTS"] = str(target)
            try:
                out = PerfHarness("noop", 50).record_run({"name": "noop"})
            finally:
                if prev_env is None:
                    os.environ.pop("MEMORY_PERF_RESULTS", None)
                else:
                    os.environ["MEMORY_PERF_RESULTS"] = prev_env
            self.assertEqual(out, target)
            self.assertEqual(json.loads(target.read_text().strip()), {"name": "noop"})


if __name__ == "__main__":
    unittest.main()
