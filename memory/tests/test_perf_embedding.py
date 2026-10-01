"""Perf: single-text embedding latency for ST and fastembed backends.

Measures `.encode([text])` only (backend instantiated and warmed up once in
setUp). Skips cleanly if:
  - the backend's ML deps aren't installed,
  - weights aren't preseeded (offline-only — we never download here),
  - the backend reports `is_available() == False`.

Fixed seed, N=30 + 5 warmup via PerfHarness. Records to the perf results
log (`perf_harness.perf_results_path()`).

Run:
  MEMORY_ALLOW_NETWORK=0 python3 -m unittest memory.tests.test_perf_embedding -v
"""

from __future__ import annotations

import os
import unittest

from memory.tests.perf_harness import PerfHarness

SHORT_TEXT = "The quick brown fox jumps over the lazy dog."

MEDIUM_TEXT = (
    "The cursor memory system captures chats, edits, and shell activity, then "
    "distills them into durable markdown memory bank files that survive across "
    "sessions. A bounded context pack is injected into every new chat so the "
    "agent remembers project decisions, stakeholders, conventions, and open "
    "action items. The system runs entirely on the local machine: nothing is "
    "uploaded, no API keys are required, and external network calls are "
    "explicitly gated by the MEMORY_ALLOW_NETWORK environment variable. "
    "Distillation is idempotent, so re-running the pipeline never grows the "
    "staging file. Recall combines FTS5 full-text search with dense vector "
    "embeddings supplied by sentence-transformers or fastembed; if neither "
    "installs, FTS-only recall remains fully functional and recall calls do "
    "not crash on special characters such as follow-up or t+2."
)

BUDGET_SHORT_MS = 100.0
BUDGET_MEDIUM_MS = 200.0
COV_LIMIT_PCT = 20.0


def _force_offline() -> None:
    """Pin offline mode so verify_weights raises instead of downloading."""
    os.environ["MEMORY_ALLOW_NETWORK"] = "0"


class _BaseEmbeddingPerf:
    backend_label: str = ""

    def _make_backend(self):
        raise NotImplementedError

    def _verify_weights(self, backend) -> None:
        raise NotImplementedError

    def setUp(self) -> None:  # type: ignore[override]
        _force_offline()
        try:
            backend = self._make_backend()
        except ImportError as e:
            raise unittest.SkipTest(
                f"{self.backend_label}: import failed ({e})"
            )
        except Exception as e:  # pragma: no cover - constructor should not raise
            raise unittest.SkipTest(
                f"{self.backend_label}: instantiation failed ({e})"
            )

        if not backend.is_available():
            raise unittest.SkipTest(
                f"{self.backend_label}: backend reports not available "
                f"(deps missing or weights not preseeded)"
            )

        try:
            self._verify_weights(backend)
        except RuntimeError as e:
            raise unittest.SkipTest(
                f"{self.backend_label}: weight verification failed "
                f"(offline; refusing to download): {e}"
            )

        try:
            _ = backend.encode([SHORT_TEXT])
        except Exception as e:
            raise unittest.SkipTest(
                f"{self.backend_label}: warmup encode failed ({e})"
            )

        self.backend = backend

    def _run_case(self, name: str, text: str, budget_ms: float) -> None:
        harness = PerfHarness(name, budget_ms, seed=42, n=30, warmup=5)
        result = harness.measure(lambda: self.backend.encode([text]))
        result["backend"] = self.backend_label
        result["text_size"] = "short" if text is SHORT_TEXT else "medium"
        harness.record_run(result)

        cov = float(result["cov_pct"])
        if cov >= COV_LIMIT_PCT:
            self.fail(
                f"{name}: CoV {cov:.2f}% >= {COV_LIMIT_PCT:.1f}% "
                f"(p50={result['p50_ms']:.3f}ms, p95={result['p95_ms']:.3f}ms, "
                f"p99={result['p99_ms']:.3f}ms, mean={result['mean_ms']:.3f}ms, "
                f"stdev={result['stdev_ms']:.3f}ms, n={result['n']})"
            )

        if result["status"] != "PASS":
            self.fail(
                f"{name}: p95 {result['p95_ms']:.3f}ms > budget "
                f"{budget_ms:.1f}ms (p50={result['p50_ms']:.3f}ms, "
                f"p99={result['p99_ms']:.3f}ms, mean={result['mean_ms']:.3f}ms, "
                f"cov={cov:.2f}%, n={result['n']})"
            )

    def test_short_text_within_budget(self) -> None:
        self._run_case(
            f"embedding.{self.backend_label}.short",
            SHORT_TEXT,
            BUDGET_SHORT_MS,
        )

    def test_medium_text_within_budget(self) -> None:
        self._run_case(
            f"embedding.{self.backend_label}.medium",
            MEDIUM_TEXT,
            BUDGET_MEDIUM_MS,
        )


class TestSentenceTransformersEmbeddingPerf(_BaseEmbeddingPerf, unittest.TestCase):
    backend_label = "sentence-transformers"

    def _make_backend(self):
        from memory.lib.memory_system.backends.embedding.sentence_transformers_backend import (
            SentenceTransformersBackend,
        )
        return SentenceTransformersBackend()

    def _verify_weights(self, backend) -> None:
        from memory.lib.memory_system.backends.embedding.weights_manifest import (
            verify_weights,
        )
        verify_weights(backend.model_name, backend.model_path)


class TestFastembedEmbeddingPerf(_BaseEmbeddingPerf, unittest.TestCase):
    backend_label = "fastembed"

    def _make_backend(self):
        from memory.lib.memory_system.backends.embedding.fastembed_backend import (
            FastembedBackend,
        )
        return FastembedBackend()

    def _verify_weights(self, backend) -> None:
        from memory.lib.memory_system.backends.embedding.weights_manifest import (
            verify_weights,
        )
        verify_weights(backend.model_id, backend.model_dir)


if __name__ == "__main__":
    unittest.main()
