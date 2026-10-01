from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


@dataclass(frozen=True)
class _PairLike:
    tag: str
    line_a: str
    line_b: str
    polarity_score: float
    file_path: str


class ContradictionModule(Protocol):
    def detect_contradictions(self, bank_files: list[Path]) -> list[_PairLike]: ...


def contradiction_module() -> ContradictionModule:
    return cast(
        ContradictionModule,
        cast(object, importlib.import_module("memory_system.learning.contradiction")),
    )


class _ContradictionTestBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self.tmp: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_contradiction_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        self.tmp = Path(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        _ = os.environ.pop("SILLY_MEMORY_HOME", None)

    def write_bank(self, name: str, lines: list[str]) -> Path:
        path = self.tmp / name
        _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


class TestContradictionDetector(_ContradictionTestBase):
    def test_prefer_vs_avoid_same_tag_is_flagged(self) -> None:
        """Two lines under #react with opposing signal words must produce a pair."""
        module = contradiction_module()
        bank = self.write_bank(
            "preferences.md",
            [
                "# Preferences",
                "",
                "- [2026-06-11] #react: prefer functional components for new screens.",
                "- [2026-06-11] #react: avoid functional components for new screens.",
            ],
        )

        pairs = module.detect_contradictions([bank])

        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual(pair.tag, "react")
        self.assertEqual(pair.file_path, str(bank))
        self.assertGreater(pair.polarity_score, 0.0)
        self.assertLessEqual(pair.polarity_score, 1.0)
        combined = pair.line_a + pair.line_b
        self.assertIn("prefer", combined)
        self.assertIn("avoid", combined)

    def test_dont_use_vs_use_same_tag_is_flagged(self) -> None:
        """Negation phrase \"don't use\" inverts the positive signal."""
        module = contradiction_module()
        bank = self.write_bank(
            "decisions.md",
            [
                "- [2026-06-11] #database: use Postgres for primary storage.",
                "- [2026-06-11] #database: don't use Postgres, pick SQLite instead.",
            ],
        )

        pairs = module.detect_contradictions([bank])

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].tag, "database")
        self.assertGreater(pairs[0].polarity_score, 0.0)

    def test_same_polarity_same_tag_not_flagged(self) -> None:
        """Two positive lines under the same tag must NOT be flagged."""
        module = contradiction_module()
        bank = self.write_bank(
            "preferences.md",
            [
                "- [2026-06-11] #react: prefer functional components everywhere.",
                "- [2026-06-11] #react: use functional components in shared modules.",
            ],
        )

        pairs = module.detect_contradictions([bank])

        self.assertEqual(pairs, [])

    def test_no_signal_words_not_flagged(self) -> None:
        """Lines without polarity signal words must NOT be flagged."""
        module = contradiction_module()
        bank = self.write_bank(
            "facts.md",
            [
                "- [2026-06-11] #notes: design review is on Tuesday.",
                "- [2026-06-11] #notes: payment flow ships in v2.",
            ],
        )

        pairs = module.detect_contradictions([bank])

        self.assertEqual(pairs, [])

    def test_different_tags_not_flagged_even_with_opposite_polarity(self) -> None:
        """Opposite polarity lines under different tags must NOT be paired."""
        module = contradiction_module()
        bank = self.write_bank(
            "multi.md",
            [
                "- [2026-06-11] #react: prefer functional components.",
                "- [2026-06-11] #vue: avoid functional components.",
            ],
        )

        pairs = module.detect_contradictions([bank])

        self.assertEqual(pairs, [])

    def test_empty_input_returns_empty(self) -> None:
        """Empty bank_files input must return an empty list, not crash."""
        module = contradiction_module()
        self.assertEqual(module.detect_contradictions([]), [])

    def test_missing_file_is_skipped(self) -> None:
        """A path that does not exist must be silently skipped."""
        module = contradiction_module()
        missing = self.tmp / "does-not-exist.md"
        self.assertEqual(module.detect_contradictions([missing]), [])

    def test_low_topic_overlap_not_flagged(self) -> None:
        """Opposite polarity but unrelated content (no shared topic) must not flag."""
        module = contradiction_module()
        bank = self.write_bank(
            "pref.md",
            [
                "- [2026-06-11] #lang: prefer rust for new backend services.",
                "- [2026-06-11] #lang: avoid javascript bundlers entirely.",
            ],
        )

        pairs = module.detect_contradictions([bank])

        self.assertEqual(pairs, [])

    def test_confidence_scored_not_binary(self) -> None:
        """Polarity score must be a non-binary float between 0 and 1."""
        module = contradiction_module()
        bank_strong = self.write_bank(
            "strong.md",
            [
                "- [2026-06-11] #api: prefer rest for the public surface.",
                "- [2026-06-11] #api: avoid rest for the public surface.",
            ],
        )
        bank_weak = self.write_bank(
            "weak.md",
            [
                "- [2026-06-11] #api: use rest for legacy clients.",
                "- [2026-06-11] #api: skip rest, switch to grpc.",
            ],
        )

        strong = module.detect_contradictions([bank_strong])
        weak = module.detect_contradictions([bank_weak])

        self.assertEqual(len(strong), 1)
        self.assertEqual(len(weak), 1)
        self.assertGreater(strong[0].polarity_score, weak[0].polarity_score)
        for pair in (strong[0], weak[0]):
            self.assertGreater(pair.polarity_score, 0.0)
            self.assertLessEqual(pair.polarity_score, 1.0)

    def test_multiple_bank_files_each_scanned_independently(self) -> None:
        """Pairs are produced per file and never cross file boundaries."""
        module = contradiction_module()
        bank_a = self.write_bank(
            "a.md",
            [
                "- [2026-06-11] #api: prefer rest for the public surface.",
                "- [2026-06-11] #api: avoid rest for the public surface.",
            ],
        )
        bank_b = self.write_bank(
            "b.md",
            [
                "- [2026-06-11] #cache: use redis for the session cache.",
                "- [2026-06-11] #cache: deprecated redis for the session cache.",
            ],
        )

        pairs = module.detect_contradictions([bank_a, bank_b])

        self.assertEqual(len(pairs), 2)
        self.assertEqual({p.file_path for p in pairs}, {str(bank_a), str(bank_b)})
        self.assertEqual({p.tag for p in pairs}, {"api", "cache"})

    def test_non_memory_lines_are_ignored(self) -> None:
        """Headings, blank lines, and non-tag bullets must be silently skipped."""
        module = contradiction_module()
        bank = self.write_bank(
            "mixed.md",
            [
                "# Heading",
                "",
                "Some prose with prefer and avoid words.",
                "- not a tagged bullet, just text",
                "- [2026-06-11] #react: prefer functional components for new screens.",
                "- [2026-06-11] #react: avoid functional components for new screens.",
            ],
        )

        pairs = module.detect_contradictions([bank])

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].tag, "react")


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
