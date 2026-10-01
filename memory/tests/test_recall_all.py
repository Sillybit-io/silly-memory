"""``memory recall --all`` and ``search_all``: every tracked workspace plus ``_global``.

Guards:
  - Every tracked store is searched before the result limit applies, so the
    only match in the least recently active of 21 workspaces still comes back.
  - Global memory is searched once, even with no tracked workspaces.
  - Hits carry their workspace label; identical text in two stores stays two hits.
  - One embedding backend per call; the noop backend gets keyword-only search.
  - An unreadable store is a named error, never a silently partial result.
  - Plain ``memory recall`` keeps its output and current-workspace scope.
  - Warm latency over five stores and 1,000 entries is measured against a
    500 ms diagnostic target; only the timing assertion may skip.

Isolation: every test uses a throwaway HOME, memory home, and projects.
"""
from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MEM_HOME = Path(__file__).resolve().parents[1]
MEM_LIB = MEM_HOME / "lib"
CLI = MEM_HOME / "bin" / "memory"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system.backends import factory  # noqa: E402
from memory_system.index import recall_all  # noqa: E402
from memory_system.paths import ensure_layout, global_store, workspace_store  # noqa: E402
from memory_system.recall import recall_hybrid  # noqa: E402
from memory_system.recall.recall_hybrid import StoreSearchError, search_all  # noqa: E402
from memory_system.status.main import iter_workspaces  # noqa: E402

_ENV_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN", "MEMORY_EMBEDDING_BACKEND")


class RecallAllTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_recall_all_")).resolve()
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        self.home = self.tmp / "stores"
        self.home.mkdir()
        shutil.copy2(MEM_HOME / "config.json", self.home / "config.json")
        os.environ["SILLY_MEMORY_HOME"] = str(self.home)
        os.environ["MEMORY_EMBEDDING_BACKEND"] = "noop"
        self._clock = time.time() - 100_000

    def tearDown(self) -> None:
        for path in self.tmp.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        for key in _ENV_VARS:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in self._saved.items() if v is not None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def workspace(self, name: str, *facts: str) -> tuple[Path, Path]:
        """A tracked project whose activity is later than every earlier one."""
        project = self.tmp / "projects" / name
        (project / ".git").mkdir(parents=True)
        store = workspace_store(project)
        ensure_layout(store)
        bank = store / "memory-bank" / "domainContext.md"
        bank.write_text("# Domain\n\n" + "".join(f"- {fact}\n" for fact in facts), encoding="utf-8")
        self._clock += 60
        os.utime(store / "observations.md", (self._clock, self._clock))
        return project, store

    def global_facts(self, *facts: str) -> Path:
        store = global_store()
        ensure_layout(store)
        bank = store / "memory-bank" / "learned-memories.md"
        bank.write_text("# Learned\n\n" + "".join(f"- {fact}\n" for fact in facts), encoding="utf-8")
        return store

    def cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        env.update(
            {
                "HOME": str(self.tmp / "user-home"),
                "SILLY_MEMORY_HOME": str(self.home),
                "MEMORY_EMBEDDING_BACKEND": "noop",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=cwd or self.tmp,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )


class TestSearchAll(RecallAllTestBase):
    def test_all_merges_hits_across_two_workspaces_and_global(self) -> None:
        project_a, store_a = self.workspace("alpha", "alpha deploys to zephyr")
        project_b, store_b = self.workspace("beta", "beta deploys to zephyr")
        self.global_facts("global rule for zephyr deploys")
        hits = search_all("zephyr")
        by_store = {hit["workspace_id"]: hit for hit in hits}
        self.assertEqual(set(by_store), {store_a.name, store_b.name, "_global"})
        self.assertEqual(by_store[store_a.name]["workspace_root"], str(project_a))
        self.assertEqual(by_store[store_a.name]["workspace_label"], str(project_a))
        self.assertEqual(by_store[store_b.name]["workspace_label"], str(project_b))
        self.assertEqual(by_store["_global"]["workspace_label"], "_global")
        self.assertIsNone(by_store["_global"]["workspace_root"])
        for hit in hits:
            for field in ("scope", "path", "section", "snippet", "score"):
                self.assertIn(field, hit)

    def test_all_prefixes_each_hit_with_workspace_label(self) -> None:
        project_a, _ = self.workspace("alpha", "alpha deploys to zephyr")
        project_b, _ = self.workspace("beta", "beta deploys to zephyr")
        self.global_facts("global rule for zephyr deploys")
        proc = self.cli("recall", "zephyr", "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        headers = [line for line in proc.stdout.splitlines() if line.startswith("[")]
        labels = {line[1 : line.index("]")] for line in headers}
        self.assertEqual(labels, {str(project_a), str(project_b), "_global"})
        self.assertTrue(all(" / " in line for line in headers), headers)

    def test_all_finds_unique_hit_in_oldest_of_21_workspaces(self) -> None:
        oldest, oldest_store = self.workspace("ws00", "oldestonlydeploy lives only here", "shared deploy note")
        projects = [self.workspace(f"ws{n:02d}", "shared deploy note")[0] for n in range(1, 21)]
        order = [row["id"] for row in iter_workspaces()]
        self.assertEqual(len(order), 21)
        self.assertEqual(order[-1], oldest_store.name, "fixture must make ws00 the least recently active")

        proc = self.cli("recall", "oldestonlydeploy", "--all", "--limit", "1", cwd=projects[-1])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith(f"[{oldest}] domainContext.md"), proc.stdout)
        self.assertEqual(proc.stdout.count("\n["), 0, "limit 1 must return one hit")
        self.assertIn("oldestonlydeploy", proc.stdout)

    def test_limit_applies_after_every_store_is_searched(self) -> None:
        for name in ("a", "b", "c"):
            self.workspace(name, *[f"release note {n} for {name}" for n in range(5)])
        self.assertEqual(len(search_all("release", limit=100)), 3)
        self.assertEqual(len(search_all("release", limit=2)), 2)
        self.assertEqual(search_all("release", limit=0), [])

    def test_limit_keeps_the_best_hits_from_later_stores(self) -> None:
        _, older = self.workspace("older", "kraken in the older project")
        _, newer = self.workspace("newer")
        (newer / "memory-bank" / "domainContext.md").write_text(
            "# Domain\n\n## One\n- kraken one\n- kraken again\n\n## Two\n- kraken two\n", encoding="utf-8"
        )
        self.assertEqual([row["id"] for row in iter_workspaces()], [newer.name, older.name])
        hits = search_all("kraken", limit=2)
        self.assertEqual({hit["workspace_id"] for hit in hits}, {newer.name, older.name})

    def test_identical_entries_in_two_stores_stay_separate(self) -> None:
        _, store_a = self.workspace("a", "the same sentence about kraken")
        _, store_b = self.workspace("b", "the same sentence about kraken")
        hits = search_all("kraken")
        self.assertEqual(sorted(hit["workspace_id"] for hit in hits), sorted([store_a.name, store_b.name]))
        self.assertEqual(hits, search_all("kraken"), "ordering must be deterministic")

    def test_all_searches_global_without_workspaces(self) -> None:
        self.global_facts("globalonlyfact for every project")
        hits = search_all("globalonlyfact")
        self.assertEqual([(h["workspace_id"], h["workspace_label"]) for h in hits], [("_global", "_global")])
        proc = self.cli("recall", "globalonlyfact", "--all")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("[_global] learned-memories.md"), proc.stdout)

    def test_empty_home_prints_no_matches(self) -> None:
        self.assertEqual(search_all("anything"), [])
        proc = self.cli("recall", "anything", "--all")
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "No matches."))

    def test_noop_backend_uses_keyword_search(self) -> None:
        self.workspace("a", "first kraken fact", "second kraken fact")
        hits = search_all("kraken")
        self.assertTrue(hits)
        self.assertTrue(all("signals" not in hit for hit in hits))
        self.assertEqual(hits[0]["score"], 1.0)

    def test_all_constructs_backend_once(self) -> None:
        for name in ("a", "b", "c"):
            self.workspace(name, f"kraken fact in {name}")
        self.global_facts("global kraken fact")

        class StubBackend:
            name = "stub"

            def is_available(self) -> bool:
                return True

            def encode(self, texts: list[str]) -> list[list[float]]:
                return [[0.0] * 4 for _ in texts]

        class EmptyVectorStore:
            def __init__(self, store: Path) -> None:
                self.store = store

            def search(self, vector: list[float], k: int) -> list[tuple[str, float]]:
                return []

        with mock.patch.object(factory, "get_embedding_backend", return_value=StubBackend()) as build, mock.patch.object(
            recall_hybrid, "VectorStore", EmptyVectorStore
        ):
            hits = search_all("kraken")
        self.assertEqual(build.call_count, 1)
        self.assertEqual(len({hit["workspace_id"] for hit in hits}), 4)
        self.assertTrue(all("signals" in hit for hit in hits), "a real backend must use hybrid recall")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read every file")
    def test_all_reports_unreadable_store(self) -> None:
        self.workspace("a", "kraken fact a")
        _, store_b = self.workspace("b", "kraken fact b")
        self.assertEqual(len(search_all("kraken")), 2)
        (store_b / "memory.sqlite").chmod(0)
        with self.assertRaises(StoreSearchError) as caught:
            search_all("kraken")
        self.assertIn(store_b.name, str(caught.exception))
        proc = self.cli("recall", "kraken", "--all")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f"cannot search store {store_b.name}", proc.stderr)
        self.assertNotIn("kraken fact", proc.stdout, "no partial result on a store error")

    def test_workspace_recall_output_is_unchanged(self) -> None:
        project_a, _ = self.workspace("alpha", "alpha deploys to zephyr")
        self.workspace("beta", "beta deploys to zephyr")
        self.global_facts("global rule for zephyr deploys")
        expected = ""
        for r in recall_all(project_a, "zephyr", limit=10):
            expected += f"[{r.get('scope')}] {r.get('path')} / {r.get('section')}\n"
            expected += f"  {r.get('snippet')}\n"
        proc = self.cli("recall", "zephyr", "--workspace", str(project_a))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(expected)
        self.assertEqual(proc.stdout, expected)
        self.assertNotIn("beta", proc.stdout)

    def test_all_warm_budget_500ms_or_skip(self) -> None:
        for n in range(5):
            facts = [f"entry {i} about subsystem {n} and routine deploys" for i in range(199)]
            facts.append(f"needle{n} marks store {n}" if n == 3 else "routine deploy entry")
            self.workspace(f"perf{n}", *facts)
        self.assertEqual([h["workspace_label"] for h in search_all("needle3")], [str(self.tmp / "projects" / "perf3")])
        samples = []
        for _ in range(12):
            start = time.perf_counter()
            hits = search_all("routine deploys", limit=10)
            samples.append((time.perf_counter() - start) * 1000)
            # Keyword hits are per bank section, one per store here.
            self.assertEqual(len({hit["workspace_id"] for hit in hits}), 5)
        p95 = statistics.quantiles(samples, n=20)[-1]
        if p95 > 500:
            self.skipTest(f"diagnostic target missed: warm p95 {p95:.1f} ms > 500 ms over 5 stores / 1,000 entries")


if __name__ == "__main__":
    unittest.main()
