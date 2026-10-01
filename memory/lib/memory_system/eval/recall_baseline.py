"""Recall quality baseline harness — measures MRR@10, Hit@3, and p50 latency.

Read this first if you are new to the codebase:
  - Two fixture modes. ``--fixtures synthetic`` seeds a throwaway temp
    store with one markdown file per fixture and measures search quality
    end-to-end. ``--fixtures real-readonly`` searches the user's real
    ``memory_home()`` stores (default ``~/.silly-memory/``) read-only and
    never writes.
  - The fixture data lives at ``eval/fixtures/baseline_queries.json``. Each
    fixture is a ``{query, expected_memory_keywords}`` pair; a recall hit
    counts when any keyword appears in the result's path, section, or
    snippet.
  - Run via ``python3 -m memory_system.eval.recall_baseline --fixtures
    synthetic`` (PYTHONPATH=memory/lib) or with the
    ``memory.lib.memory_system.eval.recall_baseline`` dotted path from
    the repo root.

Public interface (imported elsewhere): ``run_synthetic``,
    ``run_real_readonly``, ``main``.
Depends on: index (lazy), paths (lazy).
Used by: invoked directly as a script; no other modules import from it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import tempfile
import time
from typing import Any, cast
from pathlib import Path

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "baseline_queries.json"

Fixture = dict[str, Any]
Result = dict[str, Any]


def _load_fixtures() -> list[Fixture]:
    return cast(list[Fixture], json.loads(FIXTURES_PATH.read_text(encoding="utf-8")))


def _match(result: Result, keywords: list[str]) -> bool:
    haystack = " ".join(
        str(result.get(k, "")) for k in ("path", "section", "snippet")
    ).lower()
    return any(kw.lower() in haystack for kw in keywords)


def _compute_metrics(
    fixtures: list[Fixture],
    results_per_query: list[list[Result]],
    latencies_ms: list[float],
) -> dict[str, Any]:
    reciprocal_ranks: list[float] = []
    hits: list[int] = []

    for fixture, results in zip(fixtures, results_per_query):
        keywords = fixture["expected_memory_keywords"]

        rr = 0.0
        for rank, r in enumerate(results, start=1):
            if _match(r, keywords):
                rr = 1.0 / rank
                break
        reciprocal_ranks.append(rr)

        hits.append(1 if any(_match(r, keywords) for r in results[:3]) else 0)

    n = len(fixtures)
    return {
        "mrr_at_10": round(sum(reciprocal_ranks) / n, 4) if n else 0.0,
        "hit_at_3": round(sum(hits) / n, 4) if n else 0.0,
        "query_count": n,
        "latency_ms_p50": round(statistics.median(latencies_ms), 2) if latencies_ms else 0.0,
    }


def _seed_store(store: Path, fixtures: list[Fixture]) -> None:
    from ..index import rebuild_index

    bank = store / "memory-bank"
    bank.mkdir(parents=True, exist_ok=True)
    for i, fixture in enumerate(fixtures):
        kws = fixture["expected_memory_keywords"]
        lines = [
            f"## {fixture['query']}",
            "",
            f"Keywords: {', '.join(kws)}",
            f"This synthetic memory entry is about {kws[0]} and {kws[-1]}.",
        ]
        (bank / f"synthetic_{i:02d}.md").write_text("\n".join(lines), encoding="utf-8")
    rebuild_index(store, store.name)


def run_synthetic(fixtures: list[Fixture]) -> dict[str, Any]:
    """Seed a temp store with fixture content then measure recall quality."""
    tmp = Path(tempfile.mkdtemp(prefix="recall_baseline_"))
    try:
        _seed_store(tmp, fixtures)
        from ..index import recall

        results_per_query: list[list[Result]] = []
        latencies_ms: list[float] = []
        for fixture in fixtures:
            t0 = time.perf_counter()
            results = recall(tmp, fixture["query"], limit=10)
            latencies_ms.append((time.perf_counter() - t0) * 1000)
            results_per_query.append(results)

        return _compute_metrics(fixtures, results_per_query, latencies_ms)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_real_readonly(fixtures: list[Fixture]) -> dict[str, Any]:
    """Search the actual memory store read-only and measure recall quality."""
    from ..index import recall
    from ..paths import memory_home

    home = memory_home()
    if not home.exists():
        return {
            "mrr_at_10": 0.0,
            "hit_at_3": 0.0,
            "query_count": len(fixtures),
            "latency_ms_p50": 0.0,
            "warning": f"memory home not found: {home}",
        }

    stores = sorted(d for d in home.iterdir() if d.is_dir())
    if not stores:
        return {
            "mrr_at_10": 0.0,
            "hit_at_3": 0.0,
            "query_count": len(fixtures),
            "latency_ms_p50": 0.0,
            "warning": f"no stores under {home}",
        }

    results_per_query: list[list[Result]] = []
    latencies_ms: list[float] = []
    for fixture in fixtures:
        t0 = time.perf_counter()
        merged: list[Result] = []
        for store in stores:
            try:
                merged.extend(recall(store, fixture["query"], limit=10))
            except Exception:
                pass
        latencies_ms.append((time.perf_counter() - t0) * 1000)
        results_per_query.append(merged[:10])

    return _compute_metrics(fixtures, results_per_query, latencies_ms)


def main() -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Recall quality baseline harness")
    parser.add_argument(
        "--fixtures",
        choices=["synthetic", "real-readonly"],
        required=True,
        help="'synthetic' seeds a temp store; 'real-readonly' searches the memory home, default ~/.silly-memory/ (never writes)",
    )
    args = parser.parse_args()

    fixtures = _load_fixtures()
    return (
        run_synthetic(fixtures)
        if args.fixtures == "synthetic"
        else run_real_readonly(fixtures)
    )


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
