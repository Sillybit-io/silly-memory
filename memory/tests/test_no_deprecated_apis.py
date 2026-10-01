"""No calls to stdlib APIs that are deprecated and scheduled for removal.

``datetime.utcnow()`` warns on Python 3.12+ and is slated for removal; backup
dir names and archive timestamps were built from it.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
UTCNOW_CALL_RE = re.compile(r"\.utcnow\(\)")


class TestNoDeprecatedApis(unittest.TestCase):
    def test_no_datetime_utcnow_calls(self) -> None:
        """Deprecated datetime.utcnow() crept back into the library."""
        offenders = [
            f"{path.relative_to(MEM_LIB)}:{lineno}"
            for path in sorted(MEM_LIB.rglob("*.py"))
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if UTCNOW_CALL_RE.search(line)
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    _ = unittest.main()
