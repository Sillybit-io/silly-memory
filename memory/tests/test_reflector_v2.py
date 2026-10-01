from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))


class TestReflectorV2(unittest.TestCase):
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
        from memory_system.paths import workspace_store

        return workspace_store(self.ws)

    def _write_obs(self, text: str) -> Path:
        from memory_system.paths import ensure_layout

        store = self._store()
        ensure_layout(store)
        _ = (store / "observations.md").write_text(text, encoding="utf-8")
        return store

    def test_returns_false_when_no_bullets(self) -> None:
        """Empty / header-only observations is a no-op even with force=True."""
        from memory_system.reflection.reflector_v2 import run_reflector_v2

        self._write_obs("# Observations\n\n")
        self.assertFalse(run_reflector_v2(self.ws, force=True))

    def test_skips_when_below_threshold_and_not_forced(self) -> None:
        """Threshold gate keeps small obs files untouched until forced."""
        from memory_system.reflection.reflector_v2 import run_reflector_v2

        self._write_obs("- [2026-06-12] #obs: small line\n")
        self.assertFalse(run_reflector_v2(self.ws, force=False))

    def test_archives_before_condensing(self) -> None:
        """Archive must contain ALL pre-condense lines (Metis finding #10)."""
        from memory_system.reflection.reflector_v2 import run_reflector_v2

        lines = [f"- [2026-06-12] #decision: decided thing {i}" for i in range(25)]
        self._write_obs("# Observations\n\n" + "\n".join(lines) + "\n")

        ran = run_reflector_v2(self.ws, force=True, top_n=20)
        self.assertTrue(ran)

        archive_dir = self._store() / "observations-archive"
        archives = sorted(p for p in archive_dir.glob("*.md") if p.name != ".gitignore")
        self.assertEqual(len(archives), 1)
        archived = archives[0].read_text(encoding="utf-8")
        for i in range(25):
            self.assertIn(f"decided thing {i}", archived)

    def test_condenses_to_top_n_per_topic(self) -> None:
        """top_n caps lines kept *per topic* (preserves topic distribution)."""
        from memory_system.reflection.reflector_v2 import run_reflector_v2

        decision_lines = [
            f"- [2026-06-12] #decision: decided thing {i}" for i in range(25)
        ]
        action_lines = [
            f"- [2026-06-12] #task: owner: alice deliver {i}" for i in range(25)
        ]
        self._write_obs(
            "# Observations\n\n" + "\n".join(decision_lines + action_lines) + "\n"
        )

        ran = run_reflector_v2(self.ws, force=True, top_n=20)
        self.assertTrue(ran)

        condensed = (self._store() / "observations.md").read_text(encoding="utf-8")
        decision_count = sum(
            1 for ln in condensed.splitlines() if "decided thing" in ln
        )
        action_count = sum(
            1 for ln in condensed.splitlines() if "owner: alice deliver" in ln
        )
        self.assertEqual(decision_count, 20)
        self.assertEqual(action_count, 20)

    def test_roundtrip_no_data_loss_via_archive(self) -> None:
        """Condensed file may drop lines, but read_archive must recover them all."""
        from memory_system.storage.observations_archive import read_archive
        from memory_system.reflection.reflector_v2 import run_reflector_v2

        lines = [f"- [2026-06-12] #decision: decided X{i}" for i in range(30)]
        self._write_obs("# Observations\n\n" + "\n".join(lines) + "\n")

        ran = run_reflector_v2(self.ws, force=True, top_n=10)
        self.assertTrue(ran)

        archived = read_archive(self.ws)
        for i in range(30):
            self.assertIn(f"decided X{i}", archived)

    def test_preserves_topic_diversity(self) -> None:
        """Each topic appears in the condensed output (no single-topic collapse)."""
        from memory_system.reflection.reflector_v2 import run_reflector_v2

        lines = [
            "- [2026-06-12] #decision: decided alpha",
            "- [2026-06-12] #task: owner: bob deliver report",
            "- [2026-06-12] #pref: prefer terse responses",
            "- [2026-06-12] #obs: random thing observed",
        ]
        self._write_obs("# Observations\n\n" + "\n".join(lines) + "\n")

        ran = run_reflector_v2(self.ws, force=True, top_n=20)
        self.assertTrue(ran)

        condensed = (self._store() / "observations.md").read_text(encoding="utf-8")
        self.assertIn("decided alpha", condensed)
        self.assertIn("owner: bob", condensed)
        self.assertIn("prefer terse", condensed)
        self.assertIn("random thing observed", condensed)

    def test_writes_reflect_state_v2(self) -> None:
        """Sidecar state file records v2 so a re-run won't downgrade."""
        import json

        from memory_system.reflection.reflector_v2 import run_reflector_v2

        lines = [f"- [2026-06-12] #decision: decided thing {i}" for i in range(25)]
        self._write_obs("# Observations\n\n" + "\n".join(lines) + "\n")

        run_reflector_v2(self.ws, force=True, top_n=20)
        state_path = self._store() / ".reflect_state.json"
        self.assertTrue(state_path.exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state.get("version"), 2)

    def test_reruns_archive_each_time(self) -> None:
        """A second forced run also archives — no silent skip when content exists."""
        from memory_system.reflection.reflector_v2 import run_reflector_v2

        lines = [f"- [2026-06-12] #decision: decided thing {i}" for i in range(25)]
        self._write_obs("# Observations\n\n" + "\n".join(lines) + "\n")
        run_reflector_v2(self.ws, force=True, top_n=20)

        # Append more then re-run; archive must accumulate.
        store = self._store()
        more = "\n".join(
            f"- [2026-06-12] #decision: decided extra {i}" for i in range(25)
        )
        with (store / "observations.md").open("a", encoding="utf-8") as f:
            _ = f.write(more + "\n")

        run_reflector_v2(self.ws, force=True, top_n=20)
        archive_dir = store / "observations-archive"
        archives = [p for p in archive_dir.glob("*.md") if p.name != ".gitignore"]
        self.assertEqual(len(archives), 1)
        text = archives[0].read_text(encoding="utf-8")
        self.assertEqual(text.count("## Archived "), 2)
        self.assertIn("decided extra 0", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
