"""RED→GREEN coverage for ``memory profile-sync`` (the private-profile mechanism).

Guards (bugs this pins):
  - The seeded ``_global/memory-bank/profile.md`` is an ORPHAN source file: it is
    NOT one of ``scope.GLOBAL_FILES`` and therefore never appears in the injected
    context pack on its own (Oracle O-4). ``profile-sync`` is the bridge that
    routes its sections into the RECOGNIZED global bank files so they DO surface.
  - Identity/stakeholder sections must land in ``audienceContext.md`` and hard
    preferences in ``learned-memories.md`` — both in ``GLOBAL_FILES`` — so a fact
    the user typed into ``profile.md`` reaches the "## Global memory" section of
    every chat's context pack.
  - The sync must be idempotent and non-destructive: re-running replaces ONLY the
    ``<!-- profile:start -->…<!-- profile:end -->`` managed block, never
    duplicating it and never disturbing content a user (or ``/add-memory``) wrote
    outside it.

Isolation: every test roots ``SILLY_MEMORY_HOME`` in a throwaway tempdir and
seeds the real ``memory/config.json`` (so ``scope`` sees the real ``bank_files``
routing). ``TestRealHomeGuard`` asserts the isolation itself — a fixture
regression is caught here, not by a confused user days later — and that a full
sync+render cycle leaves the real ``~/.silly-memory``
byte-for-byte unchanged.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.json"
MEMORY_BIN = Path(__file__).resolve().parents[1] / "bin" / "memory"
REAL_HOMES = ((Path.home() / ".silly-memory").resolve(),)

START = "<!-- profile:start -->"
END = "<!-- profile:end -->"


def _snapshot_real_home() -> set[tuple[str, str]]:
    # Why: fresh-machine safety — an absent home is a valid baseline, not an error.
    return {
        (str(p), hashlib.sha256(p.read_bytes()).hexdigest())
        for home in REAL_HOMES
        if home.is_dir()
        for p in home.rglob("*")
        if p.is_file()
    }


class ProfileSyncTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="profile_sync_")
        root = Path(self._tmp.name)
        self.home = root / "memory-home"
        (self.home / "_global" / "memory-bank").mkdir(parents=True)
        # A throwaway workspace whose .silly-memory/memory-id marker + store stay in the
        # tempdir; merge_context_sources() plants both under SILLY_MEMORY_HOME.
        self.ws = root / "workspace"
        self.ws.mkdir(parents=True)
        # scope.merge_context_sources reads config.json:bank_files; seed the real
        # one so GLOBAL_FILES routing (audienceContext.md/learned-memories.md) is
        # realistic instead of the bank_files-less DEFAULT_CONFIG fallback.
        shutil.copyfile(REPO_CONFIG, self.home / "config.json")
        self._prev_home = os.environ.get("SILLY_MEMORY_HOME")
        os.environ["SILLY_MEMORY_HOME"] = str(self.home)

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("SILLY_MEMORY_HOME", None)
        else:
            os.environ["SILLY_MEMORY_HOME"] = self._prev_home
        self._tmp.cleanup()

    # --- helpers ---
    @property
    def _bank(self) -> Path:
        return self.home / "_global" / "memory-bank"

    def _write_profile(self, body: str) -> None:
        (self._bank / "profile.md").write_text(body, encoding="utf-8")

    def _global_section(self, pack: str) -> str:
        """Return only the text under '## Global memory' (up to the next section)."""
        self.assertIn("## Global memory", pack)
        rest = pack.split("## Global memory", 1)[1]
        for nxt in ("## Workspace memory", "## Current work state", "## Recent observations"):
            rest = rest.split(nxt, 1)[0]
        return rest


class TestProfileSync(ProfileSyncTestBase):
    def test_identity_fact_surfaces_under_global_memory(self) -> None:
        """A UUID identity fact typed into profile.md must reach the injected
        pack's '## Global memory' section via audienceContext.md — NOT via the
        orphaned profile.md itself (Oracle O-4)."""
        from memory_system.scope import merge_context_sources
        from memory_system.system.profile import sync_profile

        tag = uuid.uuid4().hex
        pref_tag = uuid.uuid4().hex
        self._write_profile(
            "<!-- LOCAL-ONLY. Never commit this file. -->\n\n"
            "# Profile (local-only)\n\n"
            "## Identity\n\n"
            f"- I am the release owner, identity marker {tag}.\n\n"
            "## Hard preferences\n\n"
            f"- Never force-push shared branches ({pref_tag}).\n"
        )

        result = sync_profile()
        self.assertEqual(result.get("status"), "synced")

        pack = merge_context_sources(self.ws)
        gsec = self._global_section(pack)

        # The identity fact surfaces under Global memory, routed via a RECOGNIZED
        # global bank file, not the orphaned profile.md.
        self.assertIn(tag, gsec)
        self.assertIn("### audienceContext.md", gsec)
        # O-4: profile.md is not a recognized global bank file → it is never read
        # as its own '### profile.md' slice in the pack.
        self.assertNotIn("### profile.md", pack)

        # The hard preference routed into learned-memories.md (also Global memory).
        self.assertIn(pref_tag, gsec)
        self.assertIn("### learned-memories.md", gsec)

        # The managed markers exist in the recognized files.
        aud = (self._bank / "audienceContext.md").read_text(encoding="utf-8")
        self.assertIn(START, aud)
        self.assertIn(END, aud)
        self.assertIn(tag, aud)
        learned = (self._bank / "learned-memories.md").read_text(encoding="utf-8")
        self.assertIn(pref_tag, learned)

    def test_sync_is_idempotent_and_preserves_outside_content(self) -> None:
        """Re-running the sync replaces ONLY the managed block (byte-identical
        result) and never disturbs content written outside it."""
        from memory_system.system.profile import sync_profile

        # Pre-existing hand/curated content outside any managed block.
        sentinel = "- hand-written stakeholder note kept verbatim.\n"
        (self._bank / "audienceContext.md").write_text(
            "# Audience Context\n\n" + sentinel, encoding="utf-8"
        )

        tag = uuid.uuid4().hex
        pref_tag = uuid.uuid4().hex
        self._write_profile(
            "# Profile (local-only)\n\n"
            "## Key stakeholders\n\n"
            f"- Works with the platform team, marker {tag}.\n\n"
            "## Hard preferences\n\n"
            f"- Squash-merge only ({pref_tag}).\n"
        )

        sync_profile()
        aud_first = (self._bank / "audienceContext.md").read_text(encoding="utf-8")
        learned_first = (self._bank / "learned-memories.md").read_text(encoding="utf-8")

        # Outside-block content preserved; managed block present exactly once.
        self.assertIn(sentinel, aud_first)
        self.assertIn(tag, aud_first)
        self.assertIn(pref_tag, learned_first)
        self.assertEqual(aud_first.count(START), 1)
        self.assertEqual(aud_first.count(END), 1)

        sync_profile()
        aud_second = (self._bank / "audienceContext.md").read_text(encoding="utf-8")
        learned_second = (self._bank / "learned-memories.md").read_text(encoding="utf-8")

        # Idempotent: a second sync yields byte-identical bank files.
        self.assertEqual(aud_first, aud_second)
        self.assertEqual(learned_first, learned_second)
        self.assertEqual(aud_second.count(START), 1)

    def test_cli_profile_sync_dispatch(self) -> None:
        """`memory profile-sync` is wired into the CLI dispatch and syncs the
        seeded profile.md end-to-end (proves the bin/memory argparse wiring)."""
        import subprocess

        tag = uuid.uuid4().hex
        self._write_profile(
            "# Profile (local-only)\n\n## Identity\n\n"
            f"- CLI-dispatch identity marker {tag}.\n"
        )
        env = dict(os.environ)
        env["SILLY_MEMORY_HOME"] = str(self.home)
        proc = subprocess.run(
            [sys.executable, str(MEMORY_BIN), "profile-sync"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        aud = (self._bank / "audienceContext.md").read_text(encoding="utf-8")
        self.assertIn(tag, aud)
        self.assertIn(START, aud)


class TestRealHomeGuard(ProfileSyncTestBase):
    def test_env_points_into_tempdir_not_real_home(self) -> None:
        """Fixture guard: SILLY_MEMORY_HOME must live in a tempdir, never the
        real ~/.silly-memory store."""
        env_home = os.environ.get("SILLY_MEMORY_HOME", "")
        self.assertTrue(env_home, "SILLY_MEMORY_HOME must be set by setUp")
        tmp_root = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(
            Path(env_home).resolve().is_relative_to(tmp_root),
            f"SILLY_MEMORY_HOME ({env_home}) escaped the tempdir root ({tmp_root})",
        )
        for real_home in REAL_HOMES:
            self.assertFalse(
                Path(env_home).resolve().is_relative_to(real_home),
                f"SILLY_MEMORY_HOME ({env_home}) points inside the real store ({real_home})",
            )

    def test_sync_and_render_do_not_touch_real_home(self) -> None:
        """A full sync + context-pack render cycle must leave the real
        ~/.silly-memory byte-for-byte unchanged."""
        from memory_system.scope import merge_context_sources
        from memory_system.system.profile import sync_profile

        before = _snapshot_real_home()
        self._write_profile(
            "# Profile (local-only)\n\n## Identity\n\n- guard check.\n"
        )
        sync_profile()
        merge_context_sources(self.ws)
        after = _snapshot_real_home()
        self.assertEqual(before, after, "profile-sync/render mutated the real store")


if __name__ == "__main__":
    unittest.main(verbosity=2)
