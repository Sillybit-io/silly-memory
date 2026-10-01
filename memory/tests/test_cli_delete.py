# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnusedParameter=false

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import importlib.util
import unittest
from pathlib import Path
from typing import Any, Callable

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
MEM_BIN = Path(__file__).resolve().parents[1] / "bin" / "memory"
sys.path.insert(0, str(MEM_LIB))

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


class _CLIDeleteBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.tmp: Path = Path()
        self.workspace: Path = Path()
        self.mem_home: Path = Path()
        self._prev_mem_home: str | None = None

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-delete-"))
        self.mem_home = self.tmp / "memhome"
        self.mem_home.mkdir(parents=True, exist_ok=True)
        self._prev_mem_home = os.environ.get("SILLY_MEMORY_HOME")
        os.environ["SILLY_MEMORY_HOME"] = str(self.mem_home)
        self.workspace = self.tmp / "workspace"
        (self.workspace / ".cursor").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self._prev_mem_home is None:
            os.environ.pop("SILLY_MEMORY_HOME", None)
        else:
            os.environ["SILLY_MEMORY_HOME"] = self._prev_mem_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_bank(
        self,
        filename: str,
        bullets: list[str],
        scores: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[Path, Path]:
        from memory_system.lifecycle.scoring import save_scores
        from memory_system.paths import ensure_layout, workspace_store

        store = workspace_store(self.workspace)
        ensure_layout(store)
        bank = store / "memory-bank" / filename
        text = "\n".join(f"- {b}" for b in bullets) + "\n"
        bank.write_text(text, encoding="utf-8")
        if scores:
            save_scores(bank, scores)
        return store, bank


class TestDeleteEntry(_CLIDeleteBase):
    def test_returns_2_when_confirm_missing(self) -> None:
        from memory_system.cli.cli_delete import delete_entry

        _, _bank = self._seed_bank("learned-memories.md", ["one", "two"])
        rc = delete_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            confirm=False,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 2)

    def test_returns_1_when_entry_id_unparseable(self) -> None:
        from memory_system.cli.cli_delete import delete_entry

        self._seed_bank("learned-memories.md", ["one"])
        rc = delete_entry(
            workspace=self.workspace,
            entry_id="not-a-valid-id",
            confirm=True,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 1)

    def test_returns_1_when_bank_file_missing(self) -> None:
        from memory_system.cli.cli_delete import delete_entry

        self._seed_bank("learned-memories.md", ["one"])
        rc = delete_entry(
            workspace=self.workspace,
            entry_id="nonexistent.md:1",
            confirm=True,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 1)

    def test_returns_1_when_line_out_of_range(self) -> None:
        from memory_system.cli.cli_delete import delete_entry

        self._seed_bank("learned-memories.md", ["one", "two"])
        rc = delete_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:999",
            confirm=True,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 1)

    def test_removes_targeted_bullet_from_bank(self) -> None:
        from memory_system.cli.cli_delete import delete_entry

        _, bank = self._seed_bank(
            "learned-memories.md", ["alpha", "bravo", "charlie"]
        )
        rc = delete_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:2",
            confirm=True,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 0)
        remaining = bank.read_text(encoding="utf-8")
        self.assertNotIn("bravo", remaining)
        self.assertIn("alpha", remaining)
        self.assertIn("charlie", remaining)

    def test_removes_score_for_target_only_preserves_others(self) -> None:
        from memory_system.cli.cli_delete import delete_entry
        from memory_system.lifecycle.scoring import load_scores

        _, bank = self._seed_bank(
            "learned-memories.md",
            ["alpha", "bravo", "charlie"],
            scores={
                "learned-memories.md:1": {"id": "learned-memories.md:1", "score": 0.5},
                "learned-memories.md:2": {"id": "learned-memories.md:2", "score": 0.05},
                "learned-memories.md:3": {"id": "learned-memories.md:3", "score": 0.8},
            },
        )
        rc = delete_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:2",
            confirm=True,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 0)
        scores = load_scores(bank)
        self.assertNotIn("learned-memories.md:2", scores)
        self.assertIn("learned-memories.md:1", scores)
        self.assertIn("learned-memories.md:3", scores)

    @unittest.skipUnless(HAS_NUMPY, "numpy not installed (optional semantic-recall dependency)")
    def test_removes_vector_from_vector_store(self) -> None:
        from memory_system.cli.cli_delete import delete_entry
        from memory_system.paths import workspace_store
        from memory_system.storage.vector_store import VectorStore

        _, _bank = self._seed_bank("learned-memories.md", ["alpha", "bravo"])
        store = workspace_store(self.workspace)
        vs = VectorStore(store)
        vs.add("learned-memories.md:1", [1.0, 0.0, 0.0])
        vs.add("learned-memories.md:2", [0.0, 1.0, 0.0])

        rc = delete_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            confirm=True,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 0)

        hits = vs.search([1.0, 0.0, 0.0], k=5)
        ids = [h[0] for h in hits]
        self.assertNotIn("learned-memories.md:1", ids)
        self.assertIn("learned-memories.md:2", ids)

    def test_dry_run_does_not_modify_anything(self) -> None:
        from memory_system.cli.cli_delete import delete_entry
        from memory_system.lifecycle.scoring import load_scores

        _, bank = self._seed_bank(
            "learned-memories.md",
            ["alpha", "bravo"],
            scores={
                "learned-memories.md:1": {"id": "learned-memories.md:1", "score": 0.05},
            },
        )
        before = bank.read_text(encoding="utf-8")
        rc = delete_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            confirm=True,
            dry_run=True,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(bank.read_text(encoding="utf-8"), before)
        scores = load_scores(bank)
        self.assertIn("learned-memories.md:1", scores)

    def test_echoes_deleted_entry_id_on_success(self) -> None:
        from memory_system.cli.cli_delete import delete_entry

        self._seed_bank("learned-memories.md", ["alpha"])
        captured: list[str] = []
        rc = delete_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            confirm=True,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(
            any("deleted learned-memories.md:1" in line for line in captured),
            f"expected success echo, got {captured!r}",
        )


class TestListPruneCandidates(_CLIDeleteBase):
    def test_returns_empty_when_no_bank_files(self) -> None:
        from memory_system.cli.cli_delete import list_prune_candidates
        from memory_system.paths import ensure_layout, workspace_store

        ensure_layout(workspace_store(self.workspace))
        out = list_prune_candidates(workspace=self.workspace)
        self.assertEqual(out, [])

    def test_returns_only_entries_at_or_below_floor(self) -> None:
        from memory_system.cli.cli_delete import list_prune_candidates

        self._seed_bank(
            "learned-memories.md",
            ["low", "mid", "high"],
            scores={
                "learned-memories.md:1": {"id": "learned-memories.md:1", "score": 0.05},
                "learned-memories.md:2": {"id": "learned-memories.md:2", "score": 0.5},
                "learned-memories.md:3": {"id": "learned-memories.md:3", "score": 0.95},
            },
        )
        out = list_prune_candidates(workspace=self.workspace, floor=0.1)
        ids = [c.entry_id for c in out]
        self.assertEqual(ids, ["learned-memories.md:1"])

    def test_sorted_ascending_by_score(self) -> None:
        from memory_system.cli.cli_delete import list_prune_candidates

        self._seed_bank(
            "learned-memories.md",
            ["a", "b", "c"],
            scores={
                "learned-memories.md:1": {"id": "learned-memories.md:1", "score": 0.10},
                "learned-memories.md:2": {"id": "learned-memories.md:2", "score": 0.05},
                "learned-memories.md:3": {"id": "learned-memories.md:3", "score": 0.07},
            },
        )
        out = list_prune_candidates(workspace=self.workspace, floor=0.5)
        scores = [c.score for c in out]
        self.assertEqual(scores, sorted(scores))

    def test_skips_orphan_sidecar_entries_with_no_bank_line(self) -> None:
        from memory_system.cli.cli_delete import list_prune_candidates

        # Sidecar references line 5 but the bank only has 2 bullets.
        self._seed_bank(
            "learned-memories.md",
            ["a", "b"],
            scores={
                "learned-memories.md:5": {"id": "learned-memories.md:5", "score": 0.05},
            },
        )
        out = list_prune_candidates(workspace=self.workspace, floor=0.1)
        self.assertEqual(out, [])

    def test_preview_strips_bullet_marker(self) -> None:
        from memory_system.cli.cli_delete import list_prune_candidates

        self._seed_bank(
            "learned-memories.md",
            ["a fact about decay"],
            scores={
                "learned-memories.md:1": {"id": "learned-memories.md:1", "score": 0.05},
            },
        )
        out = list_prune_candidates(workspace=self.workspace, floor=0.1)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].preview.startswith("- "))
        self.assertIn("fact about decay", out[0].preview)


class TestPruneReview(_CLIDeleteBase):
    def test_no_candidates_returns_0_with_friendly_message(self) -> None:
        from memory_system.cli.cli_delete import prune_review

        captured: list[str] = []
        rc = prune_review(
            workspace=self.workspace,
            confirm_all=True,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(any("no decay candidates" in line for line in captured))

    def test_confirm_all_deletes_every_candidate(self) -> None:
        from memory_system.cli.cli_delete import prune_review
        from memory_system.lifecycle.scoring import load_scores

        _, bank = self._seed_bank(
            "learned-memories.md",
            ["a", "b", "c"],
            scores={
                "learned-memories.md:1": {"id": "learned-memories.md:1", "score": 0.05},
                "learned-memories.md:2": {"id": "learned-memories.md:2", "score": 0.06},
                "learned-memories.md:3": {"id": "learned-memories.md:3", "score": 0.95},
            },
        )

        rc = prune_review(
            workspace=self.workspace,
            confirm_all=True,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 0)
        scores = load_scores(bank)
        self.assertNotIn("learned-memories.md:1", scores)
        self.assertNotIn("learned-memories.md:2", scores)
        self.assertIn("learned-memories.md:3", scores)

    def test_interactive_y_deletes_n_skips(self) -> None:
        from memory_system.cli.cli_delete import prune_review
        from memory_system.lifecycle.scoring import load_scores

        _, bank = self._seed_bank(
            "learned-memories.md",
            ["a", "b"],
            scores={
                "learned-memories.md:1": {"id": "learned-memories.md:1", "score": 0.05},
                "learned-memories.md:2": {"id": "learned-memories.md:2", "score": 0.06},
            },
        )

        answers = iter(["y", "n"])

        def fake_input(_prompt: str) -> str:
            return next(answers)

        rc = prune_review(
            workspace=self.workspace,
            input_fn=fake_input,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 0)
        scores = load_scores(bank)
        self.assertNotIn("learned-memories.md:1", scores)
        self.assertIn("learned-memories.md:2", scores)

    def test_interactive_all_deletes_remaining(self) -> None:
        from memory_system.cli.cli_delete import prune_review
        from memory_system.lifecycle.scoring import load_scores

        _, bank = self._seed_bank(
            "learned-memories.md",
            ["a", "b", "c"],
            scores={
                "learned-memories.md:1": {"id": "learned-memories.md:1", "score": 0.05},
                "learned-memories.md:2": {"id": "learned-memories.md:2", "score": 0.06},
                "learned-memories.md:3": {"id": "learned-memories.md:3", "score": 0.07},
            },
        )

        answers = iter(["n", "all"])

        def fake_input(_prompt: str) -> str:
            return next(answers)

        rc = prune_review(
            workspace=self.workspace,
            input_fn=fake_input,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 0)
        scores = load_scores(bank)
        self.assertIn("learned-memories.md:1", scores)
        self.assertNotIn("learned-memories.md:2", scores)
        self.assertNotIn("learned-memories.md:3", scores)

    def test_dry_run_lists_but_does_not_delete(self) -> None:
        from memory_system.cli.cli_delete import prune_review
        from memory_system.lifecycle.scoring import load_scores

        _, bank = self._seed_bank(
            "learned-memories.md",
            ["a"],
            scores={
                "learned-memories.md:1": {"id": "learned-memories.md:1", "score": 0.05},
            },
        )
        captured: list[str] = []
        rc = prune_review(
            workspace=self.workspace,
            dry_run=True,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        scores = load_scores(bank)
        self.assertIn("learned-memories.md:1", scores)
        self.assertTrue(any("dry-run" in line for line in captured))


class TestCLISubprocess(_CLIDeleteBase):
    """End-to-end via the `memory` CLI entry script."""

    def _run_cli(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "SILLY_MEMORY_HOME": str(self.mem_home)}
        return subprocess.run(
            [sys.executable, str(MEM_BIN), *args],
            capture_output=True,
            text=True,
            env=env,
            input=stdin,
            timeout=30,
        )

    def test_memdelete_without_confirm_exits_2(self) -> None:
        self._seed_bank("learned-memories.md", ["alpha"])
        result = self._run_cli(
            "memdelete",
            "learned-memories.md:1",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(result.returncode, 2, msg=result.stderr)

    def test_memdelete_with_confirm_removes_entry(self) -> None:
        _, bank = self._seed_bank("learned-memories.md", ["alpha", "bravo"])
        result = self._run_cli(
            "memdelete",
            "learned-memories.md:1",
            "--workspace",
            str(self.workspace),
            "--confirm",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("deleted learned-memories.md:1", result.stdout)
        self.assertNotIn("alpha", bank.read_text(encoding="utf-8"))

    def test_memprune_review_confirm_all_deletes(self) -> None:
        from memory_system.lifecycle.scoring import load_scores

        _, bank = self._seed_bank(
            "learned-memories.md",
            ["a", "b"],
            scores={
                "learned-memories.md:1": {"id": "learned-memories.md:1", "score": 0.05},
                "learned-memories.md:2": {"id": "learned-memories.md:2", "score": 0.06},
            },
        )
        result = self._run_cli(
            "memprune-review",
            "--workspace",
            str(self.workspace),
            "--confirm-all",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        scores = load_scores(bank)
        self.assertNotIn("learned-memories.md:1", scores)
        self.assertNotIn("learned-memories.md:2", scores)


if __name__ == "__main__":
    unittest.main(verbosity=2)
