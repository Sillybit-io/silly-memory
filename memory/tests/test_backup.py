# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnusedImport=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnusedParameter=false

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class BackupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="backup_test_"))
        self.memory_home = self._tmp / "memory"
        self.memory_home.mkdir(parents=True, exist_ok=True)
        self.store = self.memory_home / "ws1"
        self.store.mkdir(parents=True, exist_ok=True)
        _ = (self.store / "learned-memories.md").write_text("fact1\n", encoding="utf-8")
        _ = (self.store / "scratch.sqlite-wal").write_text("skip\n", encoding="utf-8")
        _ = (self.store / "scratch.sqlite-shm").write_text("skip\n", encoding="utf-8")
        _ = (self.store / "scratch.lock").write_text("skip\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_snapshot_store_creates_backup_dir_and_lists_latest_first(self) -> None:
        from memory_system.lifecycle.backup import list_snapshots, snapshot_store

        first = snapshot_store(self.memory_home, "alpha")
        second = snapshot_store(self.memory_home, "beta")

        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(list_snapshots(self.memory_home)[0], second)
        self.assertEqual(list_snapshots(self.memory_home)[1], first)

    def test_snapshot_store_writes_valid_manifest_with_expected_fields(self) -> None:
        from memory_system.lifecycle.backup import snapshot_store

        snapshot = snapshot_store(self.memory_home, "manifest")
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["tag"], "manifest")
        self.assertEqual(manifest["source_path"], str(self.memory_home))
        self.assertGreaterEqual(manifest["files_copied"], 1)
        self.assertIn("python_version", manifest)
        self.assertIn("ts", manifest)
        self.assertFalse((snapshot / "ws1" / "scratch.sqlite-wal").exists())
        self.assertFalse((snapshot / "ws1" / "scratch.sqlite-shm").exists())
        self.assertFalse((snapshot / "ws1" / "scratch.lock").exists())

    def test_restore_snapshot_requires_explicit_token(self) -> None:
        from memory_system.lifecycle.backup import restore_snapshot, snapshot_store

        snapshot = snapshot_store(self.memory_home, "restore")

        with self.assertRaises(TypeError):
            restore_snapshot(self.memory_home, snapshot)
        with self.assertRaises(ValueError):
            restore_snapshot(self.memory_home, snapshot, confirm_token="NOPE")

    def test_snapshot_store_cleans_partial_directory_on_failure(self) -> None:
        import memory_system.lifecycle.backup as backup

        partial = self.memory_home / ".backup-0000-00-00-000000-fail.partial"
        final = self.memory_home / ".backup-0000-00-00-000000-fail"

        original_copytree = backup.shutil.copytree

        def boom(*args, **kwargs):  # noqa: ANN001, ANN002
            raise RuntimeError("copy failed")

        backup.shutil.copytree = boom
        try:
            with self.assertRaises(RuntimeError):
                backup.snapshot_store(self.memory_home, "fail")
        finally:
            backup.shutil.copytree = original_copytree

        self.assertFalse(partial.exists())
        self.assertFalse(final.exists())
