from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import importlib
from pathlib import Path
from typing import Callable, Optional, cast
from unittest.mock import patch


MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class TestFTS5Preflight(unittest.TestCase):
    def test_check_fts5_available_returns_true_when_probe_succeeds(self) -> None:
        check_fts5_available = cast(
            Callable[[], tuple[bool, Optional[str]]],
            importlib.import_module("memory_system.preflight").check_fts5_available,
        )

        ok, err = check_fts5_available()

        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_check_fts5_available_returns_false_when_fts5_missing(self) -> None:
        check_fts5_available = cast(
            Callable[[], tuple[bool, Optional[str]]],
            importlib.import_module("memory_system.preflight").check_fts5_available,
        )

        class FakeConn:
            def execute(self, *_args: object, **_kwargs: object) -> None:
                raise sqlite3.OperationalError("no such module: fts5")

            def close(self) -> None:
                pass

        with patch("memory_system.preflight.sqlite3.connect", return_value=FakeConn()):
            ok, err = check_fts5_available()

        self.assertFalse(ok)
        self.assertIsInstance(err, str)
        assert err is not None
        self.assertIn("FTS5", err)

    def test_cli_exits_2_when_preflight_fails(self) -> None:
        env = {
            **os.environ,
            "SILLY_MEMORY_HOME": tempfile.mkdtemp(prefix="memtest_home_"),
            "PYTHONPATH": str(MEM_LIB),
            "MEMORY_FORCE_NO_FTS5": "1",
        }
        r = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "memory"), "status"],
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(r.returncode, 2)
        self.assertIn("FTS5", r.stderr)


class TestSyncFilesystemPreflight(unittest.TestCase):
    def test_check_sync_filesystem_returns_true_for_normal_memory_home(self) -> None:
        check_sync_filesystem = cast(
            Callable[[Path], tuple[bool, Optional[str]]],
            importlib.import_module("memory_system.preflight").check_sync_filesystem,
        )

        with tempfile.TemporaryDirectory(prefix="memtest_home_") as tmp:
            memory_home = Path(tmp) / ".silly-memory"
            _ = memory_home.mkdir(parents=True)

            ok, err = check_sync_filesystem(memory_home)

        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_check_sync_filesystem_warns_for_sync_locations(self) -> None:
        check_sync_filesystem = cast(
            Callable[[Path], tuple[bool, Optional[str]]],
            importlib.import_module("memory_system.preflight").check_sync_filesystem,
        )

        cases = [
            Path("/Users/test/Library/Mobile Documents/cursor-memory"),
            Path("/Users/test/Documents/cursor-memory"),
            Path("/Users/test/Desktop/cursor-memory"),
            Path("/Users/test/Dropbox/cursor-memory"),
            Path("/Users/test/Google Drive/cursor-memory"),
            Path("/Users/test/OneDrive/cursor-memory"),
            Path("/Users/test/My Sync Folder/cursor-memory"),
            Path("/Users/test/Drive/cursor-memory"),
        ]

        for memory_home in cases:
            with self.subTest(memory_home=memory_home):
                ok, err = check_sync_filesystem(memory_home)
                self.assertFalse(ok)
                self.assertIsInstance(err, str)
                assert err is not None
                self.assertIn("sync", err.lower())

    def test_cli_warns_but_keeps_running_on_sync_filesystem(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memtest_sync_home_") as tmp:
            memory_home = Path(tmp) / "Dropbox" / "memory"
            _ = memory_home.mkdir(parents=True)
            env = {
                **os.environ,
                "SILLY_MEMORY_HOME": str(memory_home),
                "PYTHONPATH": str(MEM_LIB),
            }
            r = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "bin" / "memory"), "status"],
                capture_output=True,
                text=True,
                env=env,
            )

        self.assertEqual(r.returncode, 0)
        self.assertIn("sync", r.stderr.lower())


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
