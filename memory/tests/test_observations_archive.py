from __future__ import annotations

import datetime
import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))

from memory_system.paths import workspace_store


class TestObservationsArchive(unittest.TestCase):
    _tmp: str = ""
    ws: Path = Path()

    def setUp(self) -> None:  # pyright: ignore[reportImplicitOverride]
        self._tmp = tempfile.mkdtemp(prefix="memtest_home_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        self.ws = Path(tempfile.mkdtemp(prefix="memtest_ws_"))
        _ = (self.ws / ".git").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:  # pyright: ignore[reportImplicitOverride]
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self.ws, ignore_errors=True)
        _ = os.environ.pop("SILLY_MEMORY_HOME", None)

    def _store(self) -> Path:
        return workspace_store(self.ws)

    def _write_obs(self, text: str) -> Path:
        store = self._store()
        _ = (store / "observations.md").write_text(text, encoding="utf-8")
        return store

    def test_archive_creates_month_file(self) -> None:
        from memory_system.storage.observations_archive import archive_observations

        self._write_obs("line1\nline2\n")
        with patch("memory_system.storage.observations_archive._utcnow", return_value=datetime.datetime(2026, 6, 11, 12, 0, 0)):
            archive_observations(self.ws)

        archive = self._store() / "observations-archive" / "2026-06.md"
        self.assertTrue(archive.exists())
        self.assertIn("line1", archive.read_text(encoding="utf-8"))

    def test_append_within_month(self) -> None:
        from memory_system.storage.observations_archive import archive_observations

        self._write_obs("first\n")
        with patch("memory_system.storage.observations_archive._utcnow", return_value=datetime.datetime(2026, 6, 11, 12, 0, 0)):
            archive_observations(self.ws)

        self._write_obs("second\n")
        with patch("memory_system.storage.observations_archive._utcnow", return_value=datetime.datetime(2026, 6, 12, 12, 0, 0)):
            archive_observations(self.ws)

        archive_dir = self._store() / "observations-archive"
        files = sorted(p.name for p in archive_dir.glob("*.md"))
        self.assertEqual(files, ["2026-06.md"])
        text = (archive_dir / "2026-06.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## Archived "), 2)
        self.assertIn("first", text)
        self.assertIn("second", text)

    def test_read_archive(self) -> None:
        from memory_system.storage.observations_archive import archive_observations, read_archive

        self._write_obs("june\n")
        with patch("memory_system.storage.observations_archive._utcnow", return_value=datetime.datetime(2026, 6, 11, 12, 0, 0)):
            archive_observations(self.ws)

        self._write_obs("july\n")
        with patch("memory_system.storage.observations_archive._utcnow", return_value=datetime.datetime(2026, 7, 11, 12, 0, 0)):
            archive_observations(self.ws)

        text = read_archive(self.ws)
        self.assertIn("june", text)
        self.assertIn("july", text)
