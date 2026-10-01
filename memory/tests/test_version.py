from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system.system.version import (  # noqa: E402
    CURRENT_VERSION,
    compare_versions,
    installed_version,
    parse_version,
)


class TestVersion(unittest.TestCase):
    def test_current_version_reads_repo_version(self) -> None:
        repo_version = (Path(__file__).resolve().parents[2] / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(CURRENT_VERSION, repo_version)

    def test_installed_version_reads_home_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            self.assertEqual(installed_version(root), "1.0.0")

    def test_installed_version_missing_falls_back(self) -> None:
        self.assertEqual(installed_version(Path("/tmp/nonexistent")), "0.0.0+unknown")

    def test_compare_versions_ignores_metadata(self) -> None:
        newer = compare_versions("1.0.0", "1.0.1")
        same = compare_versions("1.0.0", "1.0.0+meta")
        self.assertEqual(newer, -1)
        self.assertEqual(same, 0)

    def test_parse_version_valid_and_malformed(self) -> None:
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3, ""))
        self.assertEqual(parse_version("not-a-version"), (0, 0, 0, "unknown"))
