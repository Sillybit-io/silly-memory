"""Zero-network socket trap (T12).

Privacy invariant: when MEMORY_ALLOW_NETWORK=0 (the default), no hot-path memory
command may construct a real socket. We enforce this by patching socket.socket
and counting constructions across:

  - all 8 Cursor hook events (sessionStart, beforeSubmitPrompt, afterAgentResponse,
    afterFileEdit, afterShellExecution, preCompact, sessionEnd, stop)
  - `memory recall`
  - `memory status`

Each test runs against a throwaway SILLY_MEMORY_HOME so the real store is never
touched. The sanity guard test confirms the trap is *able* to record sockets
when MEMORY_ALLOW_NETWORK=1, so a future regression that simply never reaches
socket construction can't masquerade as "passing".
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

MEM_LIB = Path(__file__).resolve().parents[1] / "lib"
MEM_HOME = Path(__file__).resolve().parents[1]
MEM_CLI = MEM_HOME / "bin" / "memory"
sys.path.insert(0, str(MEM_LIB))


def _load_cli_module():
    """Load memory/bin/memory as a module so we can call cmd_hook/cmd_recall/cmd_status.

    The CLI script has no .py extension, so importlib can't infer a loader from
    the path alone. We pass SourceFileLoader explicitly.
    """
    loader = SourceFileLoader("memory_cli", str(MEM_CLI))
    spec = importlib.util.spec_from_loader("memory_cli", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


HOOK_EVENTS = (
    "sessionStart",
    "beforeSubmitPrompt",
    "afterAgentResponse",
    "afterFileEdit",
    "afterShellExecution",
    "preCompact",
    "sessionEnd",
    "stop",
)


class _SocketTrap:
    """Replacement __init__ for socket.socket that counts construction attempts."""

    def __init__(self, real_init):
        self.real_init = real_init
        self.count = 0
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, sock_self, *args, **kwargs):
        self.count += 1
        self.calls.append((args, dict(kwargs)))
        # Do NOT actually initialize a real socket — keep the test offline even
        # if some import path tries DNS or a bind.
        return None


class TestZeroNetwork(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="memtest_zeronet_home_")
        os.environ["SILLY_MEMORY_HOME"] = self._tmp
        # Lock out network at the policy layer too.
        os.environ["MEMORY_ALLOW_NETWORK"] = "0"
        real_cfg = MEM_HOME / "config.json"
        if real_cfg.exists():
            shutil.copy2(real_cfg, Path(self._tmp) / "config.json")
        self._ws = Path(tempfile.mkdtemp(prefix="memtest_zeronet_ws_"))
        (self._ws / ".git").mkdir(parents=True, exist_ok=True)
        # Clear name-normalization cache between tests (mirrors test_memory.py).
        try:
            from memory_system.system.normalize import clear_cache

            clear_cache()
        except Exception:
            pass
        self._cli = _load_cli_module()

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._ws, ignore_errors=True)
        os.environ.pop("SILLY_MEMORY_HOME", None)
        os.environ.pop("MEMORY_ALLOW_NETWORK", None)

    def _run_under_trap(self, fn):
        """Run fn() with socket.socket.__init__ replaced by a counting trap.
        Returns the trap so the caller can assert on .count."""
        real_init = socket.socket.__init__
        trap = _SocketTrap(real_init)
        with patch.object(socket.socket, "__init__", trap):
            fn()
        return trap

    def test_all_8_hook_events_no_socket(self) -> None:
        """All 8 Cursor hook events must complete without constructing a socket."""
        cmd_hook = self._cli.cmd_hook
        ws = str(self._ws.resolve())

        def drive() -> None:
            for event in HOOK_EVENTS:
                payload = {
                    "hook_event_name": event,
                    "workspace_roots": [ws],
                }
                # cmd_hook reads JSON from stdin.
                saved_stdin = sys.stdin
                sys.stdin = io.StringIO(json.dumps(payload))
                try:
                    args = argparse.Namespace(event=event)
                    rc = cmd_hook(args)
                    self.assertEqual(rc, 0, f"{event} returned non-zero: {rc}")
                finally:
                    sys.stdin = saved_stdin

        trap = self._run_under_trap(drive)
        self.assertEqual(
            trap.count,
            0,
            f"hook dispatch created {trap.count} socket(s): {trap.calls!r}",
        )

    def test_recall_no_socket(self) -> None:
        """`memory recall` must not construct a socket on the hot path."""
        cmd_recall = self._cli.cmd_recall
        # Seed a tiny bank so recall has something to scan (still no network).
        from memory_system.paths import bank_path, ensure_layout, workspace_store
        from memory_system.index import rebuild_index

        store = workspace_store(self._ws)
        ensure_layout(store)
        bank_path(store, "domainContext.md").write_text(
            "# Domain\n\n- [2026-06-05] #decision: ship catalog\n",
            encoding="utf-8",
        )
        rebuild_index(store, store.name)

        def drive() -> None:
            args = argparse.Namespace(
                workspace=str(self._ws),
                query="catalog",
                limit=5,
            )
            rc = cmd_recall(args)
            self.assertEqual(rc, 0)

        trap = self._run_under_trap(drive)
        self.assertEqual(
            trap.count,
            0,
            f"recall created {trap.count} socket(s): {trap.calls!r}",
        )

    def test_status_no_socket(self) -> None:
        """`memory status` must not construct a socket on the hot path."""
        cmd_status = self._cli.cmd_status

        def drive() -> None:
            args = argparse.Namespace(
                workspace=str(self._ws),
                json=False,
            )
            rc = cmd_status(args)
            self.assertEqual(rc, 0)

        trap = self._run_under_trap(drive)
        self.assertEqual(
            trap.count,
            0,
            f"status created {trap.count} socket(s): {trap.calls!r}",
        )

    def test_socket_allowed_when_network_allowed(self) -> None:
        """Sanity guard: the trap itself works, and real sockets can still be
        constructed when MEMORY_ALLOW_NETWORK=1. Without this guard, a
        regression that simply never reaches socket construction would pass
        all the other tests silently."""
        # Save an un-patched reference BEFORE entering any patch context.
        real_socket_cls = socket.socket

        # First, prove the trap records construction attempts.
        def construct() -> None:
            # Under the trap, real_init is replaced — no real fd is opened.
            _ = real_socket_cls(socket.AF_INET, socket.SOCK_STREAM)

        trap = self._run_under_trap(construct)
        self.assertEqual(
            trap.count,
            1,
            "trap failed to record a socket construction — the trap itself is broken",
        )

        # Now switch to network-allowed and confirm an unpatched real socket
        # can be constructed. Close immediately — no network IO needed.
        os.environ["MEMORY_ALLOW_NETWORK"] = "1"
        s = real_socket_cls(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.assertIsNotNone(s.fileno())
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
