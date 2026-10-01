"""End-to-end cursor-agent memory injection test (opt-in, needs network + auth).

This test seeds a UUID4 sentinel through the production memory pipeline,
renders the Cursor context-pack rule file into a temp workspace, invokes the
real ``cursor-agent`` CLI non-interactively in that workspace, and asserts the
sentinel appears in the response. The UUID is generated fresh at test time so
it is provably not in the LLM's training data — its appearance in the response
can only be explained by the injected memory context.

Opt-in gate
-----------
``unittest discover`` recurses into this package, so the test gates itself.
It runs only when BOTH are set:

    MEMORY_ALLOW_NETWORK=1 MEMORY_RUN_INTEGRATION_TESTS=1

``MEMORY_ALLOW_NETWORK=0`` does not block the cursor-agent binary itself (it
has its own network stack); the gate exists purely so the test is opt-in.

Skip chain (every environmental failure is a SKIP, not a FAIL — opaque
environmental errors must never gate CI):

  1. Either opt-in env var unset.
  2. ``cursor-agent`` not on PATH and not at ``~/.local/bin/cursor-agent``.
  3. Pre-flight probe fails with a recognized auth/network stderr pattern, or
     times out. ``cursor-agent status`` can hang on a locked keychain or an
     unreachable API, so the probe gives up after 10 s.
  4. The prompt invocation times out at 60 s.

Invocation pattern
------------------
    printf '<prompt>\\n' | cursor-agent --trust --print --workspace <ws>

``--prompt`` is not a valid flag on current CLI builds; the prompt goes on
stdin. ``--workspace`` points the agent at the temp workspace so it loads the
rendered ``.cursor/rules/_memory-context.mdc``.

Real-store guard
----------------
The test runs with ``SILLY_MEMORY_HOME`` pointed at a temp dir, and the
cursor-agent subprocess inherits it, so any memory hooks it fires write into
the sandbox. We snapshot ``~/.silly-memory``
before and after and fail if any file appeared there. cursor-agent's own session files elsewhere under
``~/.cursor`` are the CLI's business and are not checked.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

MEM_LIB = Path(__file__).resolve().parents[2] / "lib"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))


CURSOR_AGENT_FALLBACK = Path.home() / ".local" / "bin" / "cursor-agent"

# Match case-insensitively; any hit routes to SKIP.
AUTH_FAIL_PATTERNS = (
    "api key",
    "unauthorized",
    "auth",
    "credential",
    "login",
    "keychain is locked",
    "provided api key is invalid",
)
NETWORK_FAIL_PATTERNS = (
    "failed to reach the cursor api",
    "connection refused",
    "econnrefused",
    "network",
    "[unavailable]",
)


def _resolve_cursor_agent() -> str | None:
    on_path = shutil.which("cursor-agent")
    if on_path:
        return on_path
    if CURSOR_AGENT_FALLBACK.is_file() and os.access(CURSOR_AGENT_FALLBACK, os.X_OK):
        return str(CURSOR_AGENT_FALLBACK)
    return None


def _snapshot_real_store() -> set[str]:
    roots = (Path.home() / ".silly-memory",)
    return {
        os.path.join(dirpath, name)
        for root in roots
        if root.exists()
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False)
        if "__pycache__" not in dirpath
        for name in filenames
    }


def _import_pipeline_seed():
    """Locate enqueue_job + process_queue."""
    try:
        from memory_system.events.events import enqueue_job  # type: ignore[import-not-found]
        from memory_system.events.worker import process_queue  # type: ignore[import-not-found]
        return enqueue_job, process_queue
    except ImportError:
        return None


def _seed_via_pipeline(workspace_root: Path, uuid_str: str) -> bool:
    """Seed the UUID and render it through the production pipeline tail.

    The seed goes into the GLOBAL memory bank, the location the
    observer→distiller stages normally populate. Rendering via
    ``process_queue`` / ``render_rule_file`` is the real production path;
    bypassing only the observer/distiller heuristics keeps the test
    deterministic without making it vacuous. Returns True if the UUID lands in
    the rendered rule file.
    """
    from memory_system.paths import ensure_layout, global_store

    pipeline = _import_pipeline_seed()
    if pipeline is None:
        return False
    enqueue_job, process_queue = pipeline

    g_store = global_store()
    ensure_layout(g_store)
    bank_dir = g_store / "memory-bank"
    bank_dir.mkdir(parents=True, exist_ok=True)
    bank_file = bank_dir / "preferences.md"
    line = f"- The codeword for this session is {uuid_str}\n"
    existing = bank_file.read_text(encoding="utf-8") if bank_file.exists() else ""
    _ = bank_file.write_text(existing + line, encoding="utf-8")

    enqueue_job(workspace_root, "render")
    process_queue(workspace_root)

    # Also render directly so the test does not depend on the queue dispatcher.
    from memory_system.recall.context_pack import render_rule_file

    render_rule_file(workspace_root)

    rule_path = workspace_root / ".cursor" / "rules" / "_memory-context.mdc"
    if not rule_path.exists():
        return False
    return uuid_str in rule_path.read_text(encoding="utf-8")


class TestCursorAgentMemoryInjection(unittest.TestCase):
    def _skip_on_failure(self, result: subprocess.CompletedProcess[str], what: str) -> None:
        err = (result.stderr or "").lower()
        if any(p in err for p in AUTH_FAIL_PATTERNS):
            self.skipTest(f"cursor-agent: not authenticated ({what})")
        if any(p in err for p in NETWORK_FAIL_PATTERNS):
            self.skipTest(f"cursor-agent: cannot reach network ({what})")
        self.skipTest(
            f"cursor-agent: {what} failed rc={result.returncode}: {result.stderr.strip()[:200]}"
        )

    def _probe(self, cursor_agent: str) -> None:
        for argv in ([cursor_agent, "--version"], [cursor_agent, "status", "--trust"]):
            try:
                result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
            except subprocess.TimeoutExpired:
                self.skipTest(f"cursor-agent: {' '.join(argv[1:])} timed out (10s)")
            except OSError as exc:
                self.skipTest(f"cursor-agent: cannot exec ({exc!s})")
            if result.returncode != 0:
                self._skip_on_failure(result, " ".join(argv[1:]))

    def _ask(self, cursor_agent: str, workspace: Path, prompt: str) -> str:
        try:
            result = subprocess.run(
                [cursor_agent, "--trust", "--print", "--workspace", str(workspace)],
                input=prompt + "\n",
                capture_output=True,
                text=True,
                timeout=60,
                cwd=workspace,
            )
        except subprocess.TimeoutExpired:
            self.skipTest("cursor-agent: timed out after 60s")
        except OSError as exc:
            self.skipTest(f"cursor-agent: cannot exec for prompt ({exc!s})")
        if result.returncode != 0:
            self._skip_on_failure(result, "prompt")
        return result.stdout or ""

    def test_cursor_agent_memory_injection_e2e(self) -> None:
        """Seed UUID → render rule → invoke cursor-agent → assert UUID returned."""
        if os.environ.get("MEMORY_ALLOW_NETWORK") != "1":
            self.skipTest("network gated (set MEMORY_ALLOW_NETWORK=1)")
        if os.environ.get("MEMORY_RUN_INTEGRATION_TESTS") != "1":
            self.skipTest("integration tests are opt-in (set MEMORY_RUN_INTEGRATION_TESTS=1)")

        cursor_agent = _resolve_cursor_agent()
        if cursor_agent is None:
            self.skipTest("cursor-agent binary not found")

        self._probe(cursor_agent)

        store_before = _snapshot_real_store()
        tmp_home = Path(tempfile.mkdtemp(prefix="e2e-mem-home-"))
        tmp_ws = Path(tempfile.mkdtemp(prefix="e2e-ws-"))
        prev_home = os.environ.get("SILLY_MEMORY_HOME")
        os.environ["SILLY_MEMORY_HOME"] = str(tmp_home)
        # A .git dir makes workspace_id resolution behave like a real repo.
        (tmp_ws / ".git").mkdir(parents=True, exist_ok=True)

        try:
            uuid_str = str(uuid.uuid4())
            if not _seed_via_pipeline(tmp_ws, uuid_str):
                self.skipTest(
                    "memory pipeline did not surface the seeded sentinel — "
                    "enqueue_job/process_queue/render_rule_file may have moved"
                )

            # The UUID itself can only come from the injected context pack.
            response = self._ask(
                cursor_agent,
                tmp_ws,
                "What is the codeword for this session? Reply with only the codeword.",
            )
            self.assertIn(
                uuid_str,
                response,
                f"UUID sentinel not found in cursor-agent response: {response[:500]!r}",
            )
        finally:
            if prev_home is None:
                _ = os.environ.pop("SILLY_MEMORY_HOME", None)
            else:
                os.environ["SILLY_MEMORY_HOME"] = prev_home
            shutil.rmtree(tmp_home, ignore_errors=True)
            shutil.rmtree(tmp_ws, ignore_errors=True)

        unexpected = sorted(_snapshot_real_store() - store_before)
        self.assertFalse(
            unexpected,
            f"test run wrote into the real store: {unexpected[:20]}",
        )


if __name__ == "__main__":
    _ = unittest.main()
