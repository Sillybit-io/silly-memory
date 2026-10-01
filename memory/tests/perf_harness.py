"""Performance measurement harness for memory-system benchmarks.

Fixed-seed, N>=30 sample latency harness. Not auto-discovered by unittest
(filename does not start with `test_`). Intended to be imported by
`test_*.py` benchmarks or used ad-hoc from scripts.

Every recorded run is appended as one JSON line to ``$MEMORY_PERF_RESULTS``
when set (CI uploads that file), else to
``<system tempdir>/silly-memory-perf/perf-results.jsonl`` so running the suite
never drops files into the current directory.
"""

import json
import os
import random
import statistics
import tempfile
import time
from pathlib import Path


def perf_results_path() -> Path:
    override = os.environ.get("MEMORY_PERF_RESULTS")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "silly-memory-perf" / "perf-results.jsonl"


class PerfHarness:
    def __init__(
        self,
        name: str,
        budget_ms: float,
        seed: int = 42,
        n: int = 30,
        warmup: int = 5,
    ):
        self.name = name
        self.budget_ms = float(budget_ms)
        self.seed = seed
        self.n = n
        self.warmup = warmup
        random.seed(self.seed)

    def measure(self, fn) -> dict:
        random.seed(self.seed)

        for _ in range(self.warmup):
            fn()

        samples_ns = []
        for _ in range(self.n):
            t0 = time.perf_counter_ns()
            fn()
            samples_ns.append(time.perf_counter_ns() - t0)

        samples_ms = [ns / 1_000_000 for ns in samples_ns]

        if len(samples_ms) >= 2:
            quantiles = statistics.quantiles(samples_ms, n=100)
            p50_ms = quantiles[49]
            p95_ms = quantiles[94]
            p99_ms = quantiles[98]
            stdev_ms = statistics.stdev(samples_ms)
        else:
            only = samples_ms[0] if samples_ms else 0.0
            p50_ms = only
            p95_ms = only
            p99_ms = only
            stdev_ms = 0.0

        mean_ms = statistics.fmean(samples_ms) if samples_ms else 0.0
        cov_pct = (stdev_ms / mean_ms) * 100 if mean_ms else 0.0
        status = "PASS" if p95_ms <= self.budget_ms else "FAIL"

        return {
            "name": self.name,
            "n": self.n,
            "p50_ms": p50_ms,
            "p95_ms": p95_ms,
            "p99_ms": p99_ms,
            "mean_ms": mean_ms,
            "stdev_ms": stdev_ms,
            "cov_pct": cov_pct,
            "budget_ms": self.budget_ms,
            "status": status,
        }

    def assert_within_budget(self, fn) -> dict:
        result = self.measure(fn)
        if result["status"] != "PASS":
            raise AssertionError(
                f"perf budget exceeded for {self.name!r}: "
                f"p95={result['p95_ms']:.3f}ms > budget={result['budget_ms']:.3f}ms "
                f"(p50={result['p50_ms']:.3f}ms, p99={result['p99_ms']:.3f}ms, "
                f"mean={result['mean_ms']:.3f}ms, stdev={result['stdev_ms']:.3f}ms, "
                f"cov={result['cov_pct']:.2f}%, n={result['n']})"
            )
        return result

    def record_run(self, result: dict) -> Path:
        out_path = perf_results_path()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
        return out_path
