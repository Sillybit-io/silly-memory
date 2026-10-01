from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Protocol, cast

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class TopicProto(Protocol):
    name: str
    weight: float
    member_tags: tuple[str, ...]


class TopicModelProto(Protocol):
    def fit(self, store: Path) -> None: ...

    def topics(self, top_k: int = 10) -> list[TopicProto]: ...

    def tag_to_topic(self, tag: str) -> str | None: ...


class TopicModuleProto(Protocol):
    TopicModel: type
    Topic: type


def topic_module() -> TopicModuleProto:
    return cast(TopicModuleProto, cast(object, importlib.import_module("memory_system.learning.topic")))


class TopicTestBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self.home: Path = Path()
        self.store: Path = Path()
        self.bank: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_topic_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        self.home = Path(self._tmp)
        self.store = self.home / "workspace-a"
        self.bank = self.store / "memory-bank"
        _ = self.bank.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        _ = os.environ.pop("SILLY_MEMORY_HOME", None)

    def _seed_bank(self) -> None:
        _ = (self.bank / "facts.md").write_text(
            "\n".join(
                [
                    "# Facts",
                    "",
                    "- [2026-06-12] #react: components and hooks render JSX trees.",
                    "- [2026-06-12] #react: prefer functional components over class components.",
                    "- [2026-06-12] #react #ui: JSX template renders DOM elements.",
                    "- [2026-06-12] #database: PostgreSQL chosen for relational data persistence.",
                    "- [2026-06-12] #database: migrations run via alembic against PostgreSQL.",
                    "- [2026-06-12] #database: connection pool sized for production PostgreSQL load.",
                    "- [2026-06-12] #stakeholder: Maya is the PM for platform onboarding.",
                    "",
                ]
            ),
            encoding="utf-8",
        )


class TestTopicModelFit(TopicTestBase):
    def test_fit_writes_cache_with_expected_schema(self) -> None:
        module = topic_module()
        self._seed_bank()
        model = module.TopicModel()
        model.fit(self.store)

        cache = self.store / ".topic_model.json"
        self.assertTrue(cache.exists())
        payload = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertIn("topics", payload)
        self.assertIn("tag_to_topic", payload)
        self.assertIsInstance(payload["topics"], list)
        self.assertIsInstance(payload["tag_to_topic"], dict)

    def test_topics_ranked_by_weight_and_contains_seeded_tags(self) -> None:
        module = topic_module()
        self._seed_bank()
        model = module.TopicModel()
        model.fit(self.store)

        topics = model.topics(top_k=10)
        self.assertGreater(len(topics), 0)
        names = [t.name for t in topics]
        self.assertIn("database", names)
        self.assertIn("react", names)
        self.assertIn("stakeholder", names)

        weights = [t.weight for t in topics]
        self.assertEqual(weights, sorted(weights, reverse=True))
        for t in topics:
            self.assertGreaterEqual(t.weight, 0.0)
            self.assertIn(t.name, t.member_tags)

    def test_tag_to_topic_returns_canonical_for_known_tags(self) -> None:
        module = topic_module()
        self._seed_bank()
        model = module.TopicModel()
        model.fit(self.store)

        for tag in ("react", "database", "stakeholder"):
            mapped = model.tag_to_topic(tag)
            self.assertIsNotNone(mapped)
            self.assertIsInstance(mapped, str)

        self.assertIsNone(model.tag_to_topic("does-not-exist"))

    def test_top_k_limits_results(self) -> None:
        module = topic_module()
        self._seed_bank()
        model = module.TopicModel()
        model.fit(self.store)

        all_topics = model.topics(top_k=100)
        limited = model.topics(top_k=1)
        self.assertEqual(len(limited), 1)
        self.assertEqual(limited[0].name, all_topics[0].name)

    def test_reload_from_cache_round_trips(self) -> None:
        module = topic_module()
        self._seed_bank()
        first = module.TopicModel()
        first.fit(self.store)
        original = first.topics(top_k=10)
        original_mapping = {t.name: first.tag_to_topic(t.name) for t in original}

        reloaded_model = module.TopicModel(cache_path=self.store / ".topic_model.json")
        reloaded = reloaded_model.topics(top_k=10)

        self.assertEqual(
            [(t.name, tuple(t.member_tags)) for t in reloaded],
            [(t.name, tuple(t.member_tags)) for t in original],
        )
        for tag, canonical in original_mapping.items():
            self.assertEqual(reloaded_model.tag_to_topic(tag), canonical)

    def test_empty_store_yields_no_topics_but_writes_cache(self) -> None:
        module = topic_module()
        model = module.TopicModel()
        model.fit(self.store)

        self.assertEqual(model.topics(), [])
        self.assertIsNone(model.tag_to_topic("anything"))
        cache = self.store / ".topic_model.json"
        self.assertTrue(cache.exists())
        payload = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(payload["topics"], [])
        self.assertEqual(payload["tag_to_topic"], {})


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
