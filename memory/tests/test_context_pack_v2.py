# pyright: reportExplicitAny=false, reportAny=false, reportImplicitOverride=false, reportUnusedCallResult=false

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Protocol, cast

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class ContextPackV2ModuleProto(Protocol):
    DEFAULT_PER_TOPIC_LIMIT: int
    DEFAULT_TOKEN_BUDGET: int

    def build_context_pack_v2(
        self,
        store: Path,
        *,
        token_budget_chars: int = ...,
        per_topic_limit: int = ...,
    ) -> str: ...


def ctxpack_module() -> ContextPackV2ModuleProto:
    return cast(
        ContextPackV2ModuleProto,
        cast(object, importlib.import_module("memory_system.recall.context_pack_v2")),
    )


def topic_module() -> Any:
    return importlib.import_module("memory_system.learning.topic")


def scoring_module() -> Any:
    return importlib.import_module("memory_system.lifecycle.scoring")


class ContextPackV2TestBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self.home: Path = Path()
        self.store: Path = Path()
        self.bank: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_ctxpack_v2_")
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

    def _fit_topics(self) -> None:
        module = topic_module()
        model = module.TopicModel()
        model.fit(self.store)


class TestContextPackV2TopicsAware(ContextPackV2TestBase):
    def test_emits_topic_headers_after_fit(self) -> None:
        ctx = ctxpack_module()
        self._seed_bank()
        self._fit_topics()

        pack = ctx.build_context_pack_v2(self.store)
        self.assertIn("## database", pack)
        self.assertIn("## react", pack)
        self.assertIn("## stakeholder", pack)
        self.assertIn("PostgreSQL", pack)
        self.assertIn("functional components", pack)

    def test_deterministic_ordering_across_runs(self) -> None:
        ctx = ctxpack_module()
        self._seed_bank()
        self._fit_topics()

        pack_a = ctx.build_context_pack_v2(self.store)
        pack_b = ctx.build_context_pack_v2(self.store)
        self.assertEqual(pack_a, pack_b)

    def test_per_topic_limit_enforced(self) -> None:
        ctx = ctxpack_module()
        lines = ["# Facts", ""]
        for i in range(12):
            lines.append(f"- [2026-06-12] #react: fact number {i} about react components.")
        _ = (self.bank / "facts.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._fit_topics()

        pack = ctx.build_context_pack_v2(self.store, per_topic_limit=5)
        react_section_start = pack.index("## react")
        end_marker = pack.find("##", react_section_start + 2)
        section = pack[react_section_start:end_marker] if end_marker != -1 else pack[react_section_start:]
        bullet_count = sum(1 for line in section.splitlines() if line.startswith("- "))
        self.assertEqual(bullet_count, 5)

    def test_score_weighted_ordering_within_topic(self) -> None:
        ctx = ctxpack_module()
        scoring = scoring_module()
        _ = (self.bank / "facts.md").write_text(
            "\n".join(
                [
                    "# Facts",
                    "",
                    "- [2026-06-12] #react: alpha fact about react.",
                    "- [2026-06-12] #react: beta fact about react.",
                    "- [2026-06-12] #react: gamma fact about react.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._fit_topics()

        scoring.save_scores(
            self.bank / "facts.md",
            {
                "facts.md:3": {"id": "facts.md:3", "score": 0.1, "last_accessed_iso": "2026-06-12T00:00:00Z"},
                "facts.md:4": {"id": "facts.md:4", "score": 0.9, "last_accessed_iso": "2026-06-12T00:00:00Z"},
                "facts.md:5": {"id": "facts.md:5", "score": 0.5, "last_accessed_iso": "2026-06-12T00:00:00Z"},
            },
        )

        pack = ctx.build_context_pack_v2(self.store)
        beta_idx = pack.index("beta fact")
        gamma_idx = pack.index("gamma fact")
        alpha_idx = pack.index("alpha fact")
        self.assertLess(beta_idx, gamma_idx)
        self.assertLess(gamma_idx, alpha_idx)


class TestContextPackV2Budget(ContextPackV2TestBase):
    def test_token_budget_cap_enforced(self) -> None:
        ctx = ctxpack_module()
        lines = ["# Facts", ""]
        for i in range(60):
            lines.append(f"- [2026-06-12] #react: a moderately long fact number {i} about react components and hooks.")
        for i in range(60):
            lines.append(f"- [2026-06-12] #database: a moderately long fact number {i} about Postgres migrations and pooling.")
        _ = (self.bank / "facts.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._fit_topics()

        small_budget = 200
        pack = ctx.build_context_pack_v2(self.store, token_budget_chars=small_budget, per_topic_limit=50)
        self.assertLessEqual(len(pack), small_budget + 32)

    def test_default_budget_returns_non_empty_on_seeded_store(self) -> None:
        ctx = ctxpack_module()
        self._seed_bank()
        self._fit_topics()

        pack = ctx.build_context_pack_v2(self.store)
        self.assertTrue(pack.strip())
        self.assertLessEqual(len(pack), ctx.DEFAULT_TOKEN_BUDGET)


class TestContextPackV2Fallback(ContextPackV2TestBase):
    def test_falls_back_when_topic_model_not_fit(self) -> None:
        ctx = ctxpack_module()
        self._seed_bank()
        pack = ctx.build_context_pack_v2(self.store)
        self.assertTrue(pack.strip())
        self.assertNotIn("## react", pack)
        self.assertNotIn("## database", pack)
        self.assertIn("PostgreSQL", pack)
        self.assertIn("functional components", pack)

    def test_empty_store_returns_empty(self) -> None:
        ctx = ctxpack_module()
        pack = ctx.build_context_pack_v2(self.store)
        self.assertEqual(pack, "")

    def test_dedup_identical_snippets(self) -> None:
        ctx = ctxpack_module()
        _ = (self.bank / "a.md").write_text(
            "\n".join(
                [
                    "# A",
                    "",
                    "- [2026-06-12] #react: duplicate snippet body.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        _ = (self.bank / "b.md").write_text(
            "\n".join(
                [
                    "# B",
                    "",
                    "- [2026-06-12] #react: duplicate snippet body.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._fit_topics()
        pack = ctx.build_context_pack_v2(self.store)
        self.assertEqual(pack.count("duplicate snippet body"), 1)


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
