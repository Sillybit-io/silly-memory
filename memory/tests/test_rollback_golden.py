"""Rollback golden test (plan task T34).

Installs the current repo version into a tmp HOME, seeds realistic user data,
snapshots the `~/.silly-memory` tree, runs `./upgrade.sh`, runs
`./upgrade.sh --rollback --confirm`, and asserts the post-rollback tree is
byte-identical (content-only SHA-256) to the pre-upgrade tree.

Also verifies:
- Rollback lifecycle artifacts remain beside the home: the numbered backup
  that was the source of the restore (restored by copying, so it stays usable)
  and the discarded upgraded install that rollback moves aside.
- After rollback, `memory status` does NOT emit the post-upgrade banner — the
  restored state predates the `.upgraded-from` marker, so the banner has
  nothing to consume.

Skipped automatically when install.sh/upgrade.sh are not sitting next to the
source tree (e.g. when the suite is run from inside ~/.silly-memory/tests/).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = PROJECT_ROOT / "install.sh"
UPGRADE_SH = PROJECT_ROOT / "upgrade.sh"

# Files whose presence is incidental to the upgrade/rollback lifecycle and
# must not influence golden-equality. See task T34 brief + T26 banner notes
# for the rationale of each.
TRANSIENT_FILES = {
    ".first-run-seen",      # T11 banner one-shot marker
    ".upgraded-from",       # T23/T26 upgrade banner marker
    ".install.lock",        # T15 install lock (trap-cleaned)
    ".upgrade.lock",        # T23 upgrade lock (trap-cleaned)
    ".doctor-cache.json",   # T26 doctor cache (written by step 8 / rollback memdoctor)
}
TRANSIENT_DIRS = {"__pycache__"}


@unittest.skipUnless(
    INSTALL_SH.exists() and UPGRADE_SH.exists(),
    "install.sh / upgrade.sh not adjacent to source tree",
)
class TestRollbackGolden(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.tmp: Path = Path()
        self.home: Path = Path()
        self.memory_home: Path = Path()

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rollback_golden_"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        # install-zsh.sh touches/appends to ~/.zshrc — empty file is fine.
        (self.home / ".zshrc").write_text("", encoding="utf-8")
        # A Cursor user: the upgrade's doctor needs at least one wired tool.
        (self.home / ".cursor").mkdir()
        self.memory_home = self.home / ".silly-memory"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, **overrides: str) -> dict[str, str]:
        env = {
            **{k: v for k, v in os.environ.items() if k not in ("SILLY_MEMORY_HOME")},
            "HOME": str(self.home),
            "SILLY_MEMORY_HOME": str(self.memory_home),
            "MEMORY_ALLOW_NETWORK": "0",
            "MEMORY_EMBEDDING_BACKEND": "noop",
            "MEMORY_SKIP_MODEL_DOWNLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        env.update(overrides)
        return env

    def _run(self, *argv: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            env=self._env(),
            timeout=timeout,
        )

    @staticmethod
    def _digest_tree(root: Path) -> tuple[str, list[tuple[str, str]]]:
        """Return (sha256-of-(relpath, file_sha)-tuples, entries-for-diff).

        Excludes TRANSIENT_FILES by basename and TRANSIENT_DIRS during walk.
        Content-only — no mode, no mtime — so atomic rewrites of identical
        content do not perturb the digest.
        """
        entries: list[tuple[str, str]] = []
        for current, dirnames, filenames in os.walk(root):
            # Prune transient dirs in-place (modifying dirnames affects os.walk).
            dirnames[:] = sorted(d for d in dirnames if d not in TRANSIENT_DIRS)
            for name in sorted(filenames):
                if name in TRANSIENT_FILES:
                    continue
                p = Path(current) / name
                rel = p.relative_to(root).as_posix()
                if p.is_symlink():
                    sha = "L:" + os.readlink(p)
                else:
                    h = hashlib.sha256()
                    with p.open("rb") as fh:
                        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                            h.update(chunk)
                    sha = h.hexdigest()
                entries.append((rel, sha))
        entries.sort()
        outer = hashlib.sha256()
        for rel, sha in entries:
            outer.update(rel.encode() + b"\0" + sha.encode() + b"\0")
        return outer.hexdigest(), entries

    @staticmethod
    def _diff_summary(
        pre: list[tuple[str, str]],
        post: list[tuple[str, str]],
        limit: int = 25,
    ) -> str:
        pre_map = dict(pre)
        post_map = dict(post)
        only_pre = sorted(set(pre_map) - set(post_map))
        only_post = sorted(set(post_map) - set(pre_map))
        changed = sorted(
            k for k in (set(pre_map) & set(post_map)) if pre_map[k] != post_map[k]
        )

        def fmt(label: str, items: list[str]) -> str:
            head = items[:limit]
            tail = "…" if len(items) > limit else ""
            joined = ", ".join(head)
            return f"  {label} ({len(items)}): {joined}{tail}"

        return "\n".join([
            "tree diff:",
            fmt("only_pre", only_pre),
            fmt("only_post", only_post),
            fmt("changed", changed),
        ])

    def _seed_user_data(self) -> None:
        """Seed realistic durable user data per `paths.py:54-65`."""
        ws = self.memory_home / "ws-test"
        (ws / "memory-bank").mkdir(parents=True, exist_ok=True)
        (ws / ".meta.json").write_text(
            '{"workspace_id": "ws-test", "workspace_root": "/tmp/ws-test"}\n',
            encoding="utf-8",
        )
        (ws / "memory-bank" / "test-note.md").write_text(
            "# Test Note\n\n- captured: durable user fact A\n- captured: durable user fact B\n",
            encoding="utf-8",
        )
        (ws / "events.jsonl").write_text(
            '{"id":"ev-1","ts":"2026-06-15T00:00:00Z","kind":"manual","payload":{"text":"seeded"}}\n',
            encoding="utf-8",
        )
        (ws / "observations.md").write_text(
            "# Observations\n\n- [2026-06-15] seeded by rollback golden test\n",
            encoding="utf-8",
        )
        (ws / "work-state.md").write_text(
            "# Work State\n\nseeded; should survive upgrade + rollback\n",
            encoding="utf-8",
        )
        (ws / "context-pack.md").write_text(
            "# Context Pack\n\n(seeded by test)\n", encoding="utf-8",
        )
        global_dir = self.memory_home / "_global"
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "notes.md").write_text(
            "# Global Notes\n\n- seeded global fact\n", encoding="utf-8",
        )

    # --- the golden test ------------------------------------------------------
    def test_rollback_round_trips_byte_identical(self) -> None:
        # 1. Install version X.
        install_result = self._run("bash", str(INSTALL_SH))
        # Per T34 spec: install rc is not load-bearing — verify the install
        # footprint instead so this stays portable across MEMORY_SKIP_MODEL_DOWNLOAD
        # paths that exit non-zero on a missing-preseed deferred error.
        self.assertTrue(
            (self.memory_home / "VERSION").exists(),
            msg=(
                f"install.sh did not write VERSION (rc={install_result.returncode})\n"
                f"stdout tail:\n{install_result.stdout[-1500:]}\n"
                f"stderr tail:\n{install_result.stderr[-1500:]}"
            ),
        )
        self.assertTrue(
            (self.memory_home / "bin" / "memory").exists(),
            msg="install.sh did not write bin/memory",
        )

        # 2. Seed durable user data on top of the fresh install.
        self._seed_user_data()

        # 3. Snapshot pre-upgrade tree.
        digest_pre, entries_pre = self._digest_tree(self.memory_home)

        # 4. Forward upgrade (default flow; --confirm not required for upgrade).
        up = self._run("bash", str(UPGRADE_SH))
        self.assertEqual(
            up.returncode,
            0,
            msg=(
                f"upgrade.sh failed (rc={up.returncode})\n"
                f"stdout tail:\n{up.stdout[-1500:]}\n"
                f"stderr tail:\n{up.stderr[-1500:]}"
            ),
        )

        # 5. Rollback with --confirm.
        rb = self._run("bash", str(UPGRADE_SH), "--rollback", "--confirm")
        self.assertEqual(
            rb.returncode,
            0,
            msg=(
                f"rollback failed (rc={rb.returncode})\n"
                f"stdout tail:\n{rb.stdout[-1500:]}\n"
                f"stderr tail:\n{rb.stderr[-1500:]}"
            ),
        )

        # 6. Snapshot post-rollback tree.
        digest_post, entries_post = self._digest_tree(self.memory_home)

        # 7. Golden: byte-identical content under ~/.silly-memory (modulo
        #    transients listed in TRANSIENT_FILES / TRANSIENT_DIRS).
        self.assertEqual(
            digest_pre,
            digest_post,
            msg=(
                f"rollback round-trip not byte-identical\n"
                f"digest_pre  = {digest_pre}\n"
                f"digest_post = {digest_post}\n"
                f"{self._diff_summary(entries_pre, entries_post)}"
            ),
        )

        # 8. Rollback lifecycle artifacts persist beside the home: rollback
        #    restores a copy, so the numbered backup stays usable, and the
        #    upgraded install it replaced is moved aside, not deleted.
        backups = sorted(self.home.glob(".silly-memory.upgrade-backup-*"))
        discards = sorted(self.home.glob(".silly-memory.discard-*"))
        self.assertTrue(
            backups and discards,
            msg=(
                "rollback left no lifecycle artifacts beside ~/.silly-memory; "
                "expected .silly-memory.upgrade-backup-* and .silly-memory.discard-*\n"
                f"~/ contents: {sorted(p.name for p in self.home.iterdir())}"
            ),
        )

        # 9. Post-upgrade banner is absent after rollback. The restored DEST
        #    was the pre-upgrade backup which carries no .upgraded-from
        #    marker, so render_status / render_status_json have nothing to
        #    consume.
        status = self._run(
            "python3",
            str(self.memory_home / "bin" / "memory"),
            "status",
            timeout=30,
        )
        combined = status.stdout + status.stderr
        self.assertNotIn(
            "Upgraded:",
            combined,
            msg=(
                "post-upgrade banner leaked into `memory status` after rollback "
                "(.upgraded-from should be absent in the restored state)\n"
                f"status output tail:\n{combined[-1500:]}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
