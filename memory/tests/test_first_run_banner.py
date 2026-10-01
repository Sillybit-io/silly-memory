# pyright: reportMissingImports=false, reportUninitializedInstanceVariable=false, reportUnannotatedClassAttribute=false, reportImplicitOverride=false, reportUnusedCallResult=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportAny=false

"""T11: one-shot first-run banner emitted by ``memstatus``.

Pins:
  * First invocation prepends the banner block containing the exact phrase
    ``initialized and ready`` on a single line and writes
    ``$SILLY_MEMORY_HOME/.first-run-seen`` with mode 0o600.
  * Second invocation suppresses the banner.
  * ``render_status_json`` never emits the banner text, but adds the
    ``first_run`` boolean to its JSON payload and flips it to False once the
    marker has been written.
  * Banner emission is fail-open: when the marker cannot be written the
    banner still renders so the user is not left in the dark.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
MEM_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MEM_LIB))


class FirstRunBannerTestBase(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self._tmp: str = ""
        self._ws: Path = Path()

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_first_run_")
        self._saved_env = {k: os.environ.pop(k, None) for k in ("SILLY_MEMORY_HOME")}
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        real_cfg = MEM_HOME / "config.json"
        if real_cfg.exists():
            shutil.copy2(real_cfg, Path(self._tmp) / "config.json")
        self._ws = Path(tempfile.mkdtemp(prefix="memtest_first_run_ws_"))
        (self._ws / ".git").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._ws, ignore_errors=True)
        os.environ.pop("SILLY_MEMORY_HOME", None)
        os.environ.update({k: v for k, v in self._saved_env.items() if v is not None})

    def _marker(self) -> Path:
        return Path(self._tmp) / ".first-run-seen"


class TestFirstRunBanner(FirstRunBannerTestBase):
    def test_first_run_emits_banner_and_writes_marker(self) -> None:
        from memory_system.status import render_status

        out = render_status(self._ws)

        self.assertIn("initialized and ready", out)
        banner_line = next(
            (ln for ln in out.splitlines() if "initialized and ready" in ln), ""
        )
        self.assertNotEqual(banner_line, "")
        self.assertEqual(banner_line.count("initialized and ready"), 1)
        self.assertIn("silly-memory initialized and ready", banner_line)
        self.assertNotIn("Cursor Memory", out)
        self.assertIn("Version:", out)
        self.assertIn("Backend:", out)
        self.assertIn("Privacy:", out)
        privacy_line = next(
            (ln for ln in out.splitlines() if ln.strip().startswith("Privacy:")), ""
        )
        self.assertTrue(
            "offline" in privacy_line or "network-allowed" in privacy_line,
            msg=f"privacy line malformed: {privacy_line!r}",
        )
        self.assertTrue(self._marker().exists())

    def test_second_run_does_not_emit_banner(self) -> None:
        from memory_system.status import render_status

        first = render_status(self._ws)
        self.assertIn("initialized and ready", first)
        second = render_status(self._ws)
        self.assertNotIn("initialized and ready", second)

    def test_marker_file_is_0600(self) -> None:
        from memory_system.status import render_status

        _ = render_status(self._ws)
        marker = self._marker()
        self.assertTrue(marker.exists())
        mode = stat.S_IMODE(marker.stat().st_mode)
        self.assertEqual(mode, 0o600, msg=f"marker mode is {oct(mode)} not 0o600")

    def test_json_mode_does_not_print_banner_text(self) -> None:
        from memory_system.status import render_status_json

        payload_text = render_status_json(self._ws)
        self.assertNotIn("initialized and ready", payload_text)
        self.assertNotIn("━", payload_text)
        data = json.loads(payload_text)
        self.assertIs(data.get("first_run"), True)
        self.assertIn("version", data)
        self.assertIn("backend", data)
        self.assertIn("privacy", data)

    def test_json_mode_first_run_flips_to_false_after_marker(self) -> None:
        from memory_system.status import render_status_json

        _ = render_status_json(self._ws)
        data = json.loads(render_status_json(self._ws))
        self.assertIs(data.get("first_run"), False)

    def test_marker_write_failure_keeps_banner_and_does_not_block(self) -> None:
        from memory_system import status

        with mock.patch.object(status.banner, "_write_first_run_marker", return_value=False):
            out = status.render_status(self._ws)
        self.assertIn("initialized and ready", out)
        self.assertFalse(self._marker().exists())
        out2 = status.render_status(self._ws)
        self.assertIn("initialized and ready", out2)

    def test_upgrade_marker_is_read_and_consumed_in_json_mode(self) -> None:
        from memory_system.status import render_status_json

        upgrade_marker = Path(self._tmp) / ".upgraded-from"
        upgrade_marker.write_text("0.9.0\n", encoding="utf-8")

        data = json.loads(render_status_json(self._ws))
        self.assertEqual(data.get("upgraded_from"), "0.9.0")
        self.assertFalse(upgrade_marker.exists())

        data2 = json.loads(render_status_json(self._ws))
        self.assertNotIn("upgraded_from", data2)


if __name__ == "__main__":
    unittest.main()
