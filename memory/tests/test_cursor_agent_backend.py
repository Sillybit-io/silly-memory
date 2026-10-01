# pyright: reportAny=false, reportUnusedCallResult=false

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(MEM_LIB))

_MOD = "memory_system.backends.llm.cursor_agent_backend"
_ABOUT_ARGV = ["cursor-agent", "about", "--format", "json"]


def _about(email: str | None) -> subprocess.CompletedProcess[str]:
    payload = {"cliVersion": "2026.06.15", "userEmail": email, "subscriptionTier": None}
    return subprocess.CompletedProcess(_ABOUT_ARGV, 0, stdout=json.dumps(payload), stderr="")


class TestCursorAgentBackend(unittest.TestCase):
    def test_is_available_when_about_reports_signed_in_user(self) -> None:
        from memory_system.backends.llm.cursor_agent_backend import CursorAgentBackend

        with patch.dict("os.environ", {"MEMORY_ALLOW_NETWORK": "1"}), patch(
            f"{_MOD}.shutil.which", return_value="/usr/bin/cursor-agent"
        ), patch(f"{_MOD}.subprocess.run", return_value=_about("user@example.com")) as run:
            self.assertTrue(CursorAgentBackend().is_available())

        run.assert_called_once_with(_ABOUT_ARGV, capture_output=True, text=True, timeout=5)

    def test_is_available_false_when_binary_missing(self) -> None:
        from memory_system.backends.llm.cursor_agent_backend import CursorAgentBackend

        with patch(f"{_MOD}.shutil.which", return_value=None), patch(f"{_MOD}.subprocess.run") as run:
            self.assertFalse(CursorAgentBackend().is_available())

        run.assert_not_called()

    def test_is_available_false_when_logged_out(self) -> None:
        """A logged-out CLI reports userEmail null; `status` used to hang instead."""
        from memory_system.backends.llm.cursor_agent_backend import CursorAgentBackend

        with patch.dict("os.environ", {"MEMORY_ALLOW_NETWORK": "1"}), patch(
            f"{_MOD}.shutil.which", return_value="/usr/bin/cursor-agent"
        ), patch(f"{_MOD}.subprocess.run", return_value=_about(None)):
            _ = os.environ.pop("CURSOR_API_KEY", None)
            self.assertFalse(CursorAgentBackend().is_available())

    def test_is_available_with_api_key_and_no_login(self) -> None:
        from memory_system.backends.llm.cursor_agent_backend import CursorAgentBackend

        with patch.dict("os.environ", {"MEMORY_ALLOW_NETWORK": "1", "CURSOR_API_KEY": "key"}), patch(
            f"{_MOD}.shutil.which", return_value="/usr/bin/cursor-agent"
        ), patch(f"{_MOD}.subprocess.run", return_value=_about(None)):
            self.assertTrue(CursorAgentBackend().is_available())

    def test_is_available_false_when_probe_times_out(self) -> None:
        from memory_system.backends.llm.cursor_agent_backend import CursorAgentBackend

        with patch.dict("os.environ", {"MEMORY_ALLOW_NETWORK": "1"}), patch(
            f"{_MOD}.shutil.which", return_value="/usr/bin/cursor-agent"
        ), patch(f"{_MOD}.subprocess.run", side_effect=subprocess.TimeoutExpired(_ABOUT_ARGV, 5)):
            self.assertFalse(CursorAgentBackend().is_available())

    def test_is_available_false_offline_without_spawning(self) -> None:
        from memory_system.backends.llm.cursor_agent_backend import CursorAgentBackend

        with patch.dict("os.environ", {"MEMORY_ALLOW_NETWORK": "0"}), patch(f"{_MOD}.subprocess.run") as run:
            self.assertFalse(CursorAgentBackend().is_available())

        run.assert_not_called()

    def test_condense_sends_prompt_on_stdin_in_read_only_print_mode(self) -> None:
        """`agent send --prompt` was rejected by current CLI builds; the prompt goes on stdin."""
        from memory_system.backends.llm.cursor_agent_backend import CursorAgentBackend

        with patch.dict("os.environ", {"MEMORY_ALLOW_NETWORK": "1"}), patch(
            f"{_MOD}.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout="What should I remember about deployment?\n", stderr=""),
        ) as run:
            answer = CursorAgentBackend().condense(["low confidence deployment note"])

        self.assertEqual(answer, "What should I remember about deployment?")
        args, kwargs = run.call_args
        self.assertEqual(
            args[0], ["cursor-agent", "--print", "--trust", "--mode", "ask", "--output-format", "text"]
        )
        self.assertEqual(
            kwargs,
            {"input": "low confidence deployment note", "capture_output": True, "text": True, "timeout": 15},
        )

    def test_send_prompt_raises_on_nonzero_exit(self) -> None:
        from memory_system.backends.llm.cursor_agent_backend import CursorAgentBackend

        with patch.dict("os.environ", {"MEMORY_ALLOW_NETWORK": "1"}), patch(
            f"{_MOD}.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="Error: keychain is locked"),
        ):
            with self.assertRaisesRegex(RuntimeError, "keychain is locked"):
                CursorAgentBackend().condense(["x"])

    def test_every_call_refuses_offline_before_spawning(self) -> None:
        from memory_system.backends.llm.cursor_agent_backend import CursorAgentBackend

        backend = CursorAgentBackend()
        with patch.dict("os.environ", {"MEMORY_ALLOW_NETWORK": "0"}), patch(f"{_MOD}.subprocess.run") as run:
            for call in (
                lambda: backend.classify("text", ["decision"]),
                lambda: backend.condense(["obs"]),
                lambda: backend.extract_facts("prompt", "response"),
            ):
                with self.assertRaises(RuntimeError):
                    call()

        run.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main(verbosity=2)
