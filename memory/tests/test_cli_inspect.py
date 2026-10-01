# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnusedParameter=false

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
MEM_BIN = Path(__file__).resolve().parents[1] / "bin" / "memory"
sys.path.insert(0, str(MEM_LIB))

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


class _CLIInspectBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.tmp: Path = Path()
        self.workspace: Path = Path()
        self.mem_home: Path = Path()
        self._prev_mem_home: str | None = None

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-inspect-"))
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


class TestInspectEntry(_CLIInspectBase):
    def test_returns_1_when_entry_id_unparseable(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry

        self._seed_bank("learned-memories.md", ["one"])
        captured: list[str] = []
        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="not-a-valid-id",
            output_fn=captured.append,
        )
        self.assertEqual(rc, 1)
        self.assertTrue(any("missing entry" in line for line in captured))

    def test_returns_1_when_bank_file_missing(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry

        self._seed_bank("learned-memories.md", ["one"])
        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="nonexistent.md:1",
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 1)

    def test_returns_1_when_line_out_of_range(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry

        self._seed_bank("learned-memories.md", ["one", "two"])
        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:999",
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 1)

    def test_emits_score_counters_and_last_accessed(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry

        self._seed_bank(
            "learned-memories.md",
            ["#react: use cn() helper"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "score": 0.42,
                    "access_count": 7,
                    "corrections_count": 2,
                    "reinforcements_count": 5,
                    "last_accessed_iso": "2026-06-01T12:00:00Z",
                    "created_iso": "2026-05-01T12:00:00Z",
                },
            },
        )
        captured: list[str] = []
        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured)
        self.assertIn("learned-memories.md", joined)
        self.assertIn("0.42", joined)
        self.assertIn("7", joined)
        self.assertIn("2", joined)
        self.assertIn("5", joined)
        self.assertIn("2026-06-01T12:00:00Z", joined)
        self.assertIn("use cn() helper", joined)

    def test_default_score_when_no_sidecar_entry(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry

        self._seed_bank("learned-memories.md", ["#react: unscored fact"])
        captured: list[str] = []
        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured)
        self.assertIn("0.500", joined)

    @unittest.skipUnless(HAS_NUMPY, "numpy not installed (optional semantic-recall dependency)")
    def test_reports_vector_presence_and_dim(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry
        from memory_system.paths import workspace_store
        from memory_system.storage.vector_store import VectorStore

        self._seed_bank("learned-memories.md", ["#react: use cn()"])
        store = workspace_store(self.workspace)
        VectorStore(store).add("learned-memories.md:1", [0.1, 0.2, 0.3, 0.4])

        captured: list[str] = []
        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured)
        self.assertIn("vector", joined.lower())
        self.assertIn("4", joined)  # dim

    def test_reports_vector_absent_when_not_indexed(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry

        self._seed_bank("learned-memories.md", ["#react: use cn()"])
        captured: list[str] = []
        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured).lower()
        self.assertIn("absent", joined)

    def test_reports_topic_when_topic_model_fit(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry
        from memory_system.learning.topic import TopicModel
        from memory_system.paths import workspace_store

        self._seed_bank(
            "learned-memories.md",
            [
                "#react: use cn() helper from utils",
                "#react: prefer hooks over classes",
                "#react: composition over inheritance",
            ],
        )
        store = workspace_store(self.workspace)
        TopicModel().fit(store)

        captured: list[str] = []
        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured).lower()
        self.assertIn("react", joined)

    def test_reports_topic_unassigned_without_model(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry

        self._seed_bank("learned-memories.md", ["#react: use cn()"])
        captured: list[str] = []
        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured).lower()
        self.assertIn("unassigned", joined)

    def test_json_output_is_valid_and_has_all_fields(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry

        self._seed_bank(
            "learned-memories.md",
            ["#react: use cn()"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "score": 0.42,
                    "access_count": 7,
                    "corrections_count": 2,
                    "reinforcements_count": 5,
                    "last_accessed_iso": "2026-06-01T12:00:00Z",
                    "created_iso": "2026-05-01T12:00:00Z",
                },
            },
        )

        captured: list[str] = []
        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            json_output=True,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        payload = json.loads(captured[0])
        self.assertEqual(payload["entry_id"], "learned-memories.md:1")
        self.assertEqual(payload["line_no"], 1)
        self.assertEqual(payload["score"], 0.42)
        self.assertEqual(payload["access_count"], 7)
        self.assertEqual(payload["corrections_count"], 2)
        self.assertEqual(payload["reinforcements_count"], 5)
        self.assertEqual(payload["last_accessed_iso"], "2026-06-01T12:00:00Z")
        self.assertIn("vector", payload)
        self.assertIn("topic", payload)
        self.assertIn("content", payload)
        self.assertIn("tags", payload)
        self.assertIn("react", payload["tags"])

    def test_does_not_mutate_bank_or_sidecar(self) -> None:
        from memory_system.cli.cli_inspect import inspect_entry
        from memory_system.lifecycle.scoring import load_scores

        _, bank = self._seed_bank(
            "learned-memories.md",
            ["#react: use cn()"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "score": 0.42,
                },
            },
        )
        before_bank = bank.read_text(encoding="utf-8")
        before_scores = json.dumps(load_scores(bank), sort_keys=True)

        rc = inspect_entry(
            workspace=self.workspace,
            entry_id="learned-memories.md:1",
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(bank.read_text(encoding="utf-8"), before_bank)
        self.assertEqual(json.dumps(load_scores(bank), sort_keys=True), before_scores)


class TestExplainContextPack(_CLIInspectBase):
    def test_empty_store_returns_friendly_message(self) -> None:
        from memory_system.cli.cli_inspect import explain_context_pack
        from memory_system.paths import ensure_layout, workspace_store

        ensure_layout(workspace_store(self.workspace))
        captured: list[str] = []
        rc = explain_context_pack(
            workspace=self.workspace,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured).lower()
        self.assertIn("no context pack", joined)

    def test_lists_entries_with_score_components(self) -> None:
        from memory_system.cli.cli_inspect import explain_context_pack

        self._seed_bank(
            "learned-memories.md",
            [
                "#react: use cn() helper",
                "#react: prefer hooks",
            ],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "score": 0.9,
                    "last_accessed_iso": "2026-06-01T12:00:00Z",
                    "created_iso": "2026-06-01T12:00:00Z",
                },
                "learned-memories.md:2": {
                    "id": "learned-memories.md:2",
                    "score": 0.6,
                    "last_accessed_iso": "2026-05-01T12:00:00Z",
                    "created_iso": "2026-05-01T12:00:00Z",
                },
            },
        )

        captured: list[str] = []
        rc = explain_context_pack(
            workspace=self.workspace,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured)
        self.assertIn("learned-memories.md:1", joined)
        self.assertIn("learned-memories.md:2", joined)
        self.assertIn("score=", joined)
        self.assertIn("recency=", joined)
        self.assertIn("composite=", joined)

    def test_json_output_breakdown_per_entry(self) -> None:
        from memory_system.cli.cli_inspect import explain_context_pack

        self._seed_bank(
            "learned-memories.md",
            ["#react: use cn() helper"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "score": 0.9,
                    "last_accessed_iso": "2026-06-01T12:00:00Z",
                    "created_iso": "2026-06-01T12:00:00Z",
                },
            },
        )

        captured: list[str] = []
        rc = explain_context_pack(
            workspace=self.workspace,
            json_output=True,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        payload = json.loads(captured[0])
        self.assertIn("entries", payload)
        self.assertIn("weights", payload)
        self.assertEqual(len(payload["entries"]), 1)
        e0 = payload["entries"][0]
        self.assertEqual(e0["entry_id"], "learned-memories.md:1")
        self.assertIn("components", e0)
        self.assertIn("sidecar_score", e0["components"])
        self.assertIn("recency_factor", e0["components"])
        self.assertIn("composite", e0["components"])
        self.assertIn("topic", e0)
        self.assertIn("vector", e0)

    def test_json_empty_store_emits_empty_entries(self) -> None:
        from memory_system.cli.cli_inspect import explain_context_pack
        from memory_system.paths import ensure_layout, workspace_store

        ensure_layout(workspace_store(self.workspace))
        captured: list[str] = []
        rc = explain_context_pack(
            workspace=self.workspace,
            json_output=True,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        payload = json.loads(captured[0])
        self.assertEqual(payload["entries"], [])

    def test_topic_assignment_from_fit_model(self) -> None:
        from memory_system.cli.cli_inspect import explain_context_pack
        from memory_system.learning.topic import TopicModel
        from memory_system.paths import workspace_store

        self._seed_bank(
            "learned-memories.md",
            [
                "#react: use cn() helper from utils",
                "#react: prefer hooks over classes",
                "#react: composition over inheritance",
            ],
        )
        store = workspace_store(self.workspace)
        TopicModel().fit(store)

        captured: list[str] = []
        rc = explain_context_pack(
            workspace=self.workspace,
            json_output=True,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        payload = json.loads(captured[0])
        topics = {e["topic"] for e in payload["entries"]}
        self.assertIn("react", topics)

    def test_does_not_mutate_bank_or_sidecar(self) -> None:
        from memory_system.cli.cli_inspect import explain_context_pack
        from memory_system.lifecycle.scoring import load_scores

        _, bank = self._seed_bank(
            "learned-memories.md",
            ["#react: use cn()"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "score": 0.42,
                },
            },
        )
        before_bank = bank.read_text(encoding="utf-8")
        before_scores = json.dumps(load_scores(bank), sort_keys=True)

        rc = explain_context_pack(
            workspace=self.workspace,
            output_fn=lambda _s: None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(bank.read_text(encoding="utf-8"), before_bank)
        self.assertEqual(json.dumps(load_scores(bank), sort_keys=True), before_scores)


class TestCLISubprocess(_CLIInspectBase):
    """End-to-end via the `memory` CLI entry script."""

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "SILLY_MEMORY_HOME": str(self.mem_home)}
        return subprocess.run(
            [sys.executable, str(MEM_BIN), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_meminspect_returns_0_for_existing_entry(self) -> None:
        self._seed_bank(
            "learned-memories.md",
            ["#react: use cn()"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "score": 0.42,
                },
            },
        )
        result = self._run_cli(
            "meminspect",
            "learned-memories.md:1",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("learned-memories.md:1", result.stdout)
        self.assertIn("0.42", result.stdout)

    def test_meminspect_returns_1_for_missing_entry(self) -> None:
        self._seed_bank("learned-memories.md", ["#react: use cn()"])
        result = self._run_cli(
            "meminspect",
            "learned-memories.md:999",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(result.returncode, 1, msg=result.stderr)

    def test_meminspect_json_emits_valid_json(self) -> None:
        self._seed_bank(
            "learned-memories.md",
            ["#react: use cn()"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "score": 0.42,
                },
            },
        )
        result = self._run_cli(
            "meminspect",
            "learned-memories.md:1",
            "--workspace",
            str(self.workspace),
            "--json",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["entry_id"], "learned-memories.md:1")

    def test_memwhy_lists_pack_entries(self) -> None:
        self._seed_bank(
            "learned-memories.md",
            ["#react: use cn() helper"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "score": 0.42,
                },
            },
        )
        result = self._run_cli(
            "memwhy",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("learned-memories.md:1", result.stdout)
        self.assertIn("composite=", result.stdout)

    def test_memwhy_json_emits_valid_json(self) -> None:
        self._seed_bank(
            "learned-memories.md",
            ["#react: use cn() helper"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "score": 0.42,
                },
            },
        )
        result = self._run_cli(
            "memwhy",
            "--workspace",
            str(self.workspace),
            "--json",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("entries", payload)
        self.assertEqual(len(payload["entries"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
