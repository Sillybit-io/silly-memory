# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false

"""T26: one-shot post-upgrade banner emitted by ``memstatus``.

Pins:
  * After upgrade.sh writes ``.upgraded-from``, the next ``render_status``
    prepends a banner whose first informational line contains ``Upgraded:``
    and surfaces version transition, integrity verdict, backup path, and
    the doctor verify hint.
  * Second invocation has no banner (marker is consumed once).
  * ``render_status_json`` does not emit any banner text, but adds an
    ``upgrade`` key with ``{from, to, integrity, backup}`` and consumes
    the marker.
  * If both ``.first-run-seen`` is absent (first run) and ``.upgraded-from``
    exists, the first-run banner from T11 takes precedence and the
    upgrade marker is preserved so the post-upgrade banner can render on
    the very next invocation.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
MEM_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MEM_LIB))


class PostUpgradeBannerTestBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp_root: str = ""
        self._home: str = ""
        self._ws: Path = Path()

    def setUp(self) -> None:
        # Nest SILLY_MEMORY_HOME inside a parent so `memory_home().parent`
        # — used to glob `<home name>.upgrade-backup-*` — points at a clean
        # tempdir we control rather than the system /tmp root.
        self._tmp_root = tempfile.mkdtemp(prefix="memtest_post_upgrade_root_")
        self._home = str(Path(self._tmp_root) / "memory")
        Path(self._home).mkdir(parents=True, exist_ok=True)
        self._saved_env = {k: os.environ.pop(k, None) for k in ("SILLY_MEMORY_HOME")}
        os.environ["SILLY_MEMORY_HOME"] = self._home
        real_cfg = MEM_HOME / "config.json"
        if real_cfg.exists():
            shutil.copy2(real_cfg, Path(self._home) / "config.json")
        self._ws = Path(tempfile.mkdtemp(prefix="memtest_post_upgrade_ws_"))
        (self._ws / ".git").mkdir(parents=True, exist_ok=True)
        # Pre-seed the first-run marker by default so post-upgrade tests do
        # not collide with T11's first-run banner; the first-run-precedence
        # test explicitly clears it.
        Path(self._home, ".first-run-seen").write_text("1\n", encoding="utf-8")
        # Pre-warm doctor cache to a deterministic "healthy" so tests stay
        # fast and do not depend on the real doctor checks.
        Path(self._home, ".doctor-cache.json").write_text(
            json.dumps({"ts": time.time(), "integrity": "healthy"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp_root, ignore_errors=True)
        shutil.rmtree(self._ws, ignore_errors=True)
        os.environ.pop("SILLY_MEMORY_HOME", None)
        os.environ.update({k: v for k, v in self._saved_env.items() if v is not None})

    def _verify_line(self) -> str:
        from memory_system.paths import cli_command

        return f"Verify: {cli_command()} doctor"

    def _upgrade_marker(self) -> Path:
        return Path(self._home) / ".upgraded-from"

    def _first_run_marker(self) -> Path:
        return Path(self._home) / ".first-run-seen"

    def _seed_upgrade(self, old: str = "0.9.0") -> None:
        self._upgrade_marker().write_text(old + "\n", encoding="utf-8")

    def _seed_backup(self, suffix: str = "001") -> Path:
        backup = Path(self._tmp_root) / f"memory.upgrade-backup-{suffix}"
        backup.mkdir(parents=True, exist_ok=True)
        # macOS /var ↔ /private/var symlink: glob resolution surfaces the
        # canonical path, so return the same shape callers will see.
        return backup.resolve()


class TestPostUpgradeBanner(PostUpgradeBannerTestBase):
    def test_banner_shown_once_then_consumed(self) -> None:
        from memory_system.status import render_status

        self._seed_upgrade("0.9.0")
        backup = self._seed_backup("042")

        out = render_status(self._ws)

        upgraded_line = next(
            (ln for ln in out.splitlines() if "Upgraded:" in ln), ""
        )
        self.assertNotEqual(upgraded_line, "", msg=f"no Upgraded line in: {out!r}")
        self.assertIn("0.9.0", upgraded_line)
        self.assertIn("→", upgraded_line)
        self.assertIn("Integrity: healthy", out)
        self.assertIn(f"Backup: {backup}", out)
        self.assertIn(self._verify_line(), out)
        self.assertIn(str(Path(self._home).resolve() / "bin" / "memory"), self._verify_line())
        # Marker consumed after the single display.
        self.assertFalse(self._upgrade_marker().exists())

        out2 = render_status(self._ws)
        self.assertNotIn("Upgraded:", out2)

    def test_json_mode_emits_upgrade_key_and_no_banner_text(self) -> None:
        from memory_system.status import render_status_json

        self._seed_upgrade("0.9.0")
        backup = self._seed_backup("007")

        payload_text = render_status_json(self._ws)
        # No banner glyphs or words bleed into JSON output.
        self.assertNotIn("Upgraded:", payload_text)
        self.assertNotIn("━", payload_text)

        data = json.loads(payload_text)
        upgrade = data.get("upgrade")
        self.assertIsInstance(upgrade, dict)
        self.assertEqual(upgrade["from"], "0.9.0")
        self.assertEqual(upgrade["integrity"], "healthy")
        self.assertEqual(upgrade["backup"], str(backup))
        self.assertIn("to", upgrade)
        # Existing T11 field stays so callers depending on it keep working.
        self.assertEqual(data.get("upgraded_from"), "0.9.0")
        # Marker consumed after the JSON read so the next call is clean.
        self.assertFalse(self._upgrade_marker().exists())
        data2 = json.loads(render_status_json(self._ws))
        self.assertNotIn("upgrade", data2)
        self.assertNotIn("upgraded_from", data2)

    def test_marker_absent_no_banner_emitted(self) -> None:
        from memory_system.status import render_status

        # No upgrade marker seeded.
        self.assertFalse(self._upgrade_marker().exists())
        out = render_status(self._ws)
        self.assertNotIn("Upgraded:", out)
        self.assertNotIn("Verify:", out)

    def test_first_run_precedence_when_both_markers_present(self) -> None:
        from memory_system.status import render_status

        # Remove first-run marker → next render is "first run".
        self._first_run_marker().unlink(missing_ok=True)
        self._seed_upgrade("0.9.0")
        _ = self._seed_backup("100")

        out_first = render_status(self._ws)
        self.assertIn("initialized and ready", out_first)
        self.assertNotIn("Upgraded:", out_first)
        # Upgrade marker is preserved so the post-upgrade banner can fire
        # on the very next invocation.
        self.assertTrue(self._upgrade_marker().exists())

        out_second = render_status(self._ws)
        self.assertNotIn("initialized and ready", out_second)
        self.assertIn("Upgraded:", out_second)
        # Now the marker is consumed.
        self.assertFalse(self._upgrade_marker().exists())

        out_third = render_status(self._ws)
        self.assertNotIn("initialized and ready", out_third)
        self.assertNotIn("Upgraded:", out_third)

    def test_malformed_marker_does_not_crash(self) -> None:
        from memory_system.status import render_status

        # Empty file → _read_upgrade_marker returns None → graceful skip.
        self._upgrade_marker().write_text("", encoding="utf-8")
        out = render_status(self._ws)
        self.assertNotIn("Upgraded:", out)

    def test_doctor_cache_used_when_fresh(self) -> None:
        from memory_system import status

        self._seed_upgrade("0.9.0")
        _ = self._seed_backup("003")
        # Cache is pre-seeded "healthy" in setUp; doctor.run_doctor must
        # NOT be invoked while the cache is fresh.
        with mock.patch.object(status.doctor_cache, "io"):
            with mock.patch("memory_system.cli.doctor.run_doctor") as run_doctor:
                out = status.render_status(self._ws)
                self.assertEqual(run_doctor.call_count, 0)
        self.assertIn("Integrity: healthy", out)

    def test_doctor_cache_refreshed_when_stale(self) -> None:
        from memory_system import status

        self._seed_upgrade("0.9.0")
        _ = self._seed_backup("004")
        cache = Path(self._home) / ".doctor-cache.json"
        # Force the cache far past the 5-minute TTL.
        cache.write_text(
            json.dumps({"ts": time.time() - 10_000, "integrity": "healthy"}),
            encoding="utf-8",
        )
        with mock.patch.object(
            status.banner, "_doctor_integrity", return_value="warn"
        ) as patched:
            out = status.render_status(self._ws)
            self.assertEqual(patched.call_count, 1)
        self.assertIn("Integrity: warn", out)


if __name__ == "__main__":
    unittest.main()
