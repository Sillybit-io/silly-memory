# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false, reportExplicitAny=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnusedParameter=false

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
MEM_BIN = Path(__file__).resolve().parents[1] / "bin" / "memory"
sys.path.insert(0, str(MEM_LIB))


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class _LearnStatusBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.tmp: Path = Path()
        self.workspace: Path = Path()
        self.mem_home: Path = Path()
        self._prev_mem_home: str | None = None

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-learn-status-"))
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

    def _ensure_store(self) -> Path:
        from memory_system.paths import ensure_layout, workspace_store

        store = workspace_store(self.workspace)
        ensure_layout(store)
        return store

    def _seed_bank(
        self,
        filename: str,
        bullets: list[str],
        scores: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[Path, Path]:
        from memory_system.lifecycle.scoring import save_scores

        store = self._ensure_store()
        bank = store / "memory-bank" / filename
        text = "\n".join(f"- {b}" for b in bullets) + "\n"
        bank.write_text(text, encoding="utf-8")
        if scores:
            save_scores(bank, scores)
        return store, bank

    def _write_corrections(self, store: Path, records: list[dict[str, Any]]) -> None:
        path = store / "corrections.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def _write_active_q_state(self, store: Path, session_id: str, ts: str) -> None:
        path = store / ".active_q_state.json"
        path.write_text(
            json.dumps(
                {"last_question_session_id": session_id, "last_question_ts": ts},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


class TestCollectLearnStatus(_LearnStatusBase):
    def test_empty_store_returns_zeros(self) -> None:
        from memory_system.cli.cli_learn_status import collect_learn_status

        data = collect_learn_status(self.workspace)
        self.assertEqual(data["corrections"]["total"], 0)
        self.assertEqual(data["corrections"]["last_24h"], 0)
        self.assertEqual(data["reinforced"]["total"], 0)
        self.assertEqual(data["reinforced"]["last_24h"], 0)
        self.assertEqual(data["demoted"]["total"], 0)
        self.assertEqual(data["demoted"]["last_24h"], 0)
        self.assertEqual(data["contradictions_active"], 0)
        self.assertIsNone(data["active_questioning"]["session_id"])
        self.assertIsNone(data["active_questioning"]["ts"])

    def test_corrections_total_and_24h_split(self) -> None:
        from memory_system.cli.cli_learn_status import collect_learn_status

        store = self._ensure_store()
        now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
        recent = now - timedelta(hours=2)
        older = now - timedelta(hours=72)
        self._write_corrections(
            store,
            [
                {"id": "a", "ts": _iso(recent), "ai_response_excerpt": "x", "edit_diff_summary": "x", "confidence": 0.5, "conversation_id": ""},
                {"id": "b", "ts": _iso(recent), "ai_response_excerpt": "y", "edit_diff_summary": "y", "confidence": 0.5, "conversation_id": ""},
                {"id": "c", "ts": _iso(older), "ai_response_excerpt": "z", "edit_diff_summary": "z", "confidence": 0.5, "conversation_id": ""},
            ],
        )
        data = collect_learn_status(self.workspace, now=now)
        self.assertEqual(data["corrections"]["total"], 3)
        self.assertEqual(data["corrections"]["last_24h"], 2)

    def test_corrections_malformed_lines_ignored(self) -> None:
        from memory_system.cli.cli_learn_status import collect_learn_status

        store = self._ensure_store()
        path = store / "corrections.jsonl"
        path.write_text(
            "\n".join([
                "not json",
                json.dumps({"id": "a", "ts": "2026-06-12T10:00:00Z"}),
                "",
                json.dumps(["not", "a", "dict"]),
                json.dumps({"id": "b", "ts": "bad-ts"}),
            ]) + "\n",
            encoding="utf-8",
        )
        now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
        data = collect_learn_status(self.workspace, now=now)
        self.assertEqual(data["corrections"]["total"], 2)
        self.assertEqual(data["corrections"]["last_24h"], 1)

    def test_reinforced_counts_entries_with_positive_counter(self) -> None:
        from memory_system.cli.cli_learn_status import collect_learn_status

        now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
        recent_iso = _iso(now - timedelta(hours=3))
        old_iso = _iso(now - timedelta(days=10))
        self._seed_bank(
            "learned-memories.md",
            ["a", "b", "c"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "reinforcements_count": 3,
                    "last_accessed_iso": recent_iso,
                },
                "learned-memories.md:2": {
                    "id": "learned-memories.md:2",
                    "reinforcements_count": 1,
                    "last_accessed_iso": old_iso,
                },
                "learned-memories.md:3": {
                    "id": "learned-memories.md:3",
                    "reinforcements_count": 0,
                    "last_accessed_iso": recent_iso,
                },
            },
        )
        data = collect_learn_status(self.workspace, now=now)
        self.assertEqual(data["reinforced"]["total"], 2)
        self.assertEqual(data["reinforced"]["last_24h"], 1)

    def test_demoted_counts_entries_with_corrections_counter(self) -> None:
        from memory_system.cli.cli_learn_status import collect_learn_status

        now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
        recent_iso = _iso(now - timedelta(hours=1))
        old_iso = _iso(now - timedelta(days=7))
        self._seed_bank(
            "learned-memories.md",
            ["a", "b", "c"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "corrections_count": 2,
                    "last_accessed_iso": recent_iso,
                },
                "learned-memories.md:2": {
                    "id": "learned-memories.md:2",
                    "corrections_count": 5,
                    "last_accessed_iso": old_iso,
                },
                "learned-memories.md:3": {
                    "id": "learned-memories.md:3",
                    "corrections_count": 0,
                    "last_accessed_iso": recent_iso,
                },
            },
        )
        data = collect_learn_status(self.workspace, now=now)
        self.assertEqual(data["demoted"]["total"], 2)
        self.assertEqual(data["demoted"]["last_24h"], 1)

    def test_contradictions_active_reflects_live_detector(self) -> None:
        from memory_system.cli.cli_learn_status import collect_learn_status

        self._seed_bank(
            "preferences.md",
            [
                "#sass: prefer Sass for styling",
                "#sass: avoid Sass for styling",
            ],
        )
        data = collect_learn_status(self.workspace)
        self.assertGreaterEqual(data["contradictions_active"], 1)

    def test_active_questioning_last_session_reported(self) -> None:
        from memory_system.cli.cli_learn_status import collect_learn_status

        store = self._ensure_store()
        self._write_active_q_state(store, "ses_123", "2026-06-12T10:00:00Z")
        data = collect_learn_status(self.workspace)
        self.assertEqual(data["active_questioning"]["session_id"], "ses_123")
        self.assertEqual(data["active_questioning"]["ts"], "2026-06-12T10:00:00Z")

    def test_active_questioning_malformed_state_returns_null(self) -> None:
        from memory_system.cli.cli_learn_status import collect_learn_status

        store = self._ensure_store()
        (store / ".active_q_state.json").write_text("not json", encoding="utf-8")
        data = collect_learn_status(self.workspace)
        self.assertIsNone(data["active_questioning"]["session_id"])

    def test_does_not_mutate_state_files(self) -> None:
        from memory_system.cli.cli_learn_status import collect_learn_status

        store = self._ensure_store()
        self._write_corrections(
            store,
            [{"id": "a", "ts": "2026-06-12T10:00:00Z"}],
        )
        self._write_active_q_state(store, "ses_abc", "2026-06-12T10:00:00Z")
        self._seed_bank(
            "learned-memories.md",
            ["a"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "reinforcements_count": 2,
                    "last_accessed_iso": "2026-06-12T10:00:00Z",
                },
            },
        )

        before_corr = (store / "corrections.jsonl").read_text(encoding="utf-8")
        before_aq = (store / ".active_q_state.json").read_text(encoding="utf-8")
        before_side = (store / "memory-bank" / "learned-memories.md.score.json").read_text(encoding="utf-8")

        _ = collect_learn_status(self.workspace)

        self.assertEqual((store / "corrections.jsonl").read_text(encoding="utf-8"), before_corr)
        self.assertEqual((store / ".active_q_state.json").read_text(encoding="utf-8"), before_aq)
        self.assertEqual(
            (store / "memory-bank" / "learned-memories.md.score.json").read_text(encoding="utf-8"),
            before_side,
        )


class TestRenderLearnStatus(_LearnStatusBase):
    def test_text_render_empty_store(self) -> None:
        from memory_system.cli.cli_learn_status import render_learn_status

        captured: list[str] = []
        rc = render_learn_status(
            workspace=self.workspace,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured)
        self.assertIn("Learning Status", joined)
        self.assertIn("total=0", joined)
        self.assertIn("not yet recorded", joined.lower())

    def test_text_render_includes_workspace_name(self) -> None:
        from memory_system.cli.cli_learn_status import render_learn_status

        captured: list[str] = []
        rc = render_learn_status(
            workspace=self.workspace,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured)
        self.assertIn(self.workspace.name, joined)

    def test_text_render_with_data(self) -> None:
        from memory_system.cli.cli_learn_status import render_learn_status

        store = self._ensure_store()
        now = datetime(2026, 6, 12, 12, 0, 0, tzinfo=timezone.utc)
        self._write_corrections(
            store,
            [{"id": "a", "ts": _iso(now - timedelta(hours=1))}],
        )
        self._seed_bank(
            "learned-memories.md",
            ["a"],
            scores={
                "learned-memories.md:1": {
                    "id": "learned-memories.md:1",
                    "reinforcements_count": 3,
                    "corrections_count": 1,
                    "last_accessed_iso": _iso(now - timedelta(hours=2)),
                },
            },
        )
        self._write_active_q_state(store, "ses_xyz", "2026-06-12T10:00:00Z")

        captured: list[str] = []
        rc = render_learn_status(
            workspace=self.workspace,
            now=now,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        joined = "\n".join(captured)
        self.assertIn("Corrections", joined)
        self.assertIn("Memories reinforced", joined)
        self.assertIn("Memories demoted", joined)
        self.assertIn("ses_xyz", joined)

    def test_json_render_empty_store(self) -> None:
        from memory_system.cli.cli_learn_status import render_learn_status

        captured: list[str] = []
        rc = render_learn_status(
            workspace=self.workspace,
            json_output=True,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)
        payload = json.loads(captured[0])
        self.assertEqual(payload["corrections"]["total"], 0)
        self.assertEqual(payload["reinforced"]["total"], 0)
        self.assertEqual(payload["demoted"]["total"], 0)
        self.assertEqual(payload["contradictions_active"], 0)
        self.assertIn("active_questioning", payload)

    def test_json_render_has_all_required_keys(self) -> None:
        from memory_system.cli.cli_learn_status import render_learn_status

        captured: list[str] = []
        rc = render_learn_status(
            workspace=self.workspace,
            json_output=True,
            output_fn=captured.append,
        )
        self.assertEqual(rc, 0)
        payload = json.loads(captured[0])
        for key in ("corrections", "reinforced", "demoted"):
            self.assertIn("total", payload[key])
            self.assertIn("last_24h", payload[key])
        self.assertIn("contradictions_active", payload)
        self.assertIn("active_questioning", payload)
        self.assertIn("workspace", payload)


class TestCLISubprocess(_LearnStatusBase):
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

    def test_memlearn_status_returns_0_on_empty_store(self) -> None:
        _ = self._ensure_store()
        result = self._run_cli(
            "memlearn-status",
            "--workspace",
            str(self.workspace),
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Learning Status", result.stdout)
        self.assertIn("total=0", result.stdout)

    def test_memlearn_status_json_emits_valid_json(self) -> None:
        _ = self._ensure_store()
        result = self._run_cli(
            "memlearn-status",
            "--workspace",
            str(self.workspace),
            "--json",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["corrections"]["total"], 0)
        self.assertIn("contradictions_active", payload)

    def test_memlearn_status_with_data(self) -> None:
        store = self._ensure_store()
        now = datetime.now(timezone.utc)
        self._write_corrections(
            store,
            [{"id": "a", "ts": _iso(now)}, {"id": "b", "ts": _iso(now)}],
        )
        result = self._run_cli(
            "memlearn-status",
            "--workspace",
            str(self.workspace),
            "--json",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["corrections"]["total"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
