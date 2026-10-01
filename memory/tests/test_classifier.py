from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Protocol, cast

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class ClassifierModule(Protocol):
    def classify(self, line: str, min_confidence: float | None = None) -> tuple[str, float]: ...

    def reset_classifier_for_tests(self) -> None: ...


def classifier_module() -> ClassifierModule:
    return cast(ClassifierModule, cast(object, importlib.import_module("memory_system.learning.classifier")))


class ClassifierTestBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self.home: Path = Path()
        self.bank: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_classifier_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        self.home = Path(self._tmp)
        self.bank = self.home / "workspace-a" / "memory-bank"
        _ = self.bank.mkdir(parents=True)
        _ = (self.bank / "facts.md").write_text(
            "\n".join(
                [
                    "# Facts",
                    "",
                    "- [2026-06-11] #decision: Decided to use PostgreSQL for customer records.",
                    "- [2026-06-11] #decision: Team agreed the API remains REST for v1.",
                    "- [2026-06-11] #action-item: Follow up with Ana about launch checklist; owner: Ana; due: Friday.",
                    "- [2026-06-11] #action-item: Action item to update billing tests before release.",
                    "- [2026-06-11] #preference: Prefer small focused modules and no generated wrappers.",
                    "- [2026-06-11] #preference: Always write unittest coverage before implementation.",
                    "- [2026-06-11] #stakeholder: Maya is the PM for billing migration.",
                    "- [2026-06-11] #stakeholder: Jordan is the engineering lead for platform.",
                    "- [2026-06-11] #milestone: Delivered the onboarding migration on Tuesday.",
                    "- [2026-06-11] #milestone: Completed phase two of the rollout.",
                    "- [2026-06-11] #active-focus: Current focus is stabilizing checkout auth.",
                    "- [2026-06-11] #active-focus: Active work is memory classifier hardening.",
                    "- [2026-06-11] #observation: The repository uses stdlib unittest for regression tests.",
                    "- [2026-06-11] #observation: Memory bank files store dated markdown bullets.",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        try:
            classifier_module().reset_classifier_for_tests()
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)
        _ = os.environ.pop("SILLY_MEMORY_HOME", None)


class TestTfidfClassifier(ClassifierTestBase):
    def test_classifies_held_out_examples_with_confidence(self) -> None:
        classify = classifier_module().classify

        examples = {
            "decision": "Agreed to keep Redis out of the first release.",
            "action-item": "Follow-up with Priya on the audit notes; owner: Priya; due: Monday.",
            "preference": "Prefer explicit config files over hidden defaults.",
            "stakeholder": "Nina is the director for enterprise programs.",
            "milestone": "Completed the partner import milestone yesterday.",
            "active-focus": "Current focus is fixing classifier cache behavior.",
            "observation": "The test suite runs with python unittest only.",
        }

        for expected, text in examples.items():
            with self.subTest(expected=expected):
                label, confidence = classify(text, min_confidence=0.4)
                self.assertEqual(label, expected)
                self.assertGreater(confidence, 0.5)

    def test_min_confidence_returns_unknown_with_raw_confidence(self) -> None:
        classify = classifier_module().classify

        label, confidence = classify("asdf qwer zxcv", min_confidence=0.4)

        self.assertEqual(label, "unknown")
        self.assertLess(confidence, 0.4)

    def test_keyword_boosts_are_additive_not_exclusive(self) -> None:
        classify = classifier_module().classify

        label, confidence = classify("I never mind, let's decide on the API design", min_confidence=0.4)

        self.assertEqual(label, "decision")
        self.assertGreater(confidence, 0.5)

    def test_classifier_cache_is_written_and_reused(self) -> None:
        module = classifier_module()
        classify = module.classify
        reset_classifier_for_tests = module.reset_classifier_for_tests

        label, _ = classify("Decided to use SQLite for the prototype", min_confidence=0.4)
        self.assertEqual(label, "decision")
        cache_path = self.home / "_classifier" / "tfidf.pkl"
        self.assertTrue(cache_path.exists())
        before = cache_path.stat().st_mtime_ns

        reset_classifier_for_tests()
        shutil.rmtree(self.home / "workspace-a")
        label, confidence = classify("Decided to keep the cached classifier", min_confidence=0.4)

        self.assertEqual(label, "decision")
        self.assertGreater(confidence, 0.4)
        self.assertEqual(cache_path.stat().st_mtime_ns, before)


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
