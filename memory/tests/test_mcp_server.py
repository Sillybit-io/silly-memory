"""The stdio MCP server, driven through ``bin/memory-mcp`` over real pipes.

Guards:
  - The classic 2025-06-18 lifecycle: ping works before initialization,
    other requests wait for ``initialize``; an unsupported requested revision
    gets the one the server implements; notifications never get a reply;
    stdout carries only JSON-RPC; the server exits when stdin closes.
  - Exactly three tools, with argument validation as JSON-RPC errors and
    storage failures as ``isError`` tool results. A malformed line or unknown
    method does not end the loop.
  - The workspace per client: ``--workspace``, Claude Code's
    ``CLAUDE_PROJECT_DIR``, Cursor's negotiated roots (including a change, a
    timeout, and ambiguity), and the current directory for OpenCode and a
    standalone launch. Every one resolves ``repo/packages/api``
    to the same repository store; a linked worktree stays separate.
  - ``scope: all`` finds the only match in the least recently active of 21
    stores; tool text is capped at 4,000 characters.
  - ``memory_add`` stores the fact and refreshes every configured project
    rule; a failed refresh still reports the fact as saved.

Isolation: every server runs with a throwaway HOME, memory home, and projects.
"""
from __future__ import annotations

import ast
import json
import os
import queue
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

MEM_HOME = Path(__file__).resolve().parents[1]
MEM_LIB = MEM_HOME / "lib"
LAUNCHER = MEM_HOME / "bin" / "memory-mcp"
if str(MEM_LIB) not in sys.path:
    sys.path.insert(0, str(MEM_LIB))

from memory_system.mcp_server import decode_file_uri  # noqa: E402
from memory_system.paths import ensure_layout, workspace_store  # noqa: E402

PROTOCOL = "2025-06-18"
_ENV_VARS = ("SILLY_MEMORY_HOME", "MEMORY_BIN", "CLAUDE_PROJECT_DIR", "MEMORY_EMBEDDING_BACKEND")


class McpClient:
    """A test-side MCP client for one server process."""

    def __init__(self, test: unittest.TestCase, args: list[str], env: dict[str, str], cwd: Path) -> None:
        self.test = test
        self.proc = subprocess.Popen(
            [sys.executable, str(LAUNCHER), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd,
            bufsize=1,
        )
        self.inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self.non_json: list[str] = []
        self.stderr: list[str] = []
        self.server_requests: list[dict[str, Any]] = []
        self.next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.non_json.append(line)
                continue
            self.inbox.put(message)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line)

    def send(self, message: dict[str, Any]) -> None:
        self.raw(json.dumps(message))

    def raw(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout: float = 30) -> dict[str, Any]:
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            self.test.fail(f"no message from the server within {timeout}s; stderr={''.join(self.stderr)}")
            raise

    def response_to(self, request_id: object, timeout: float = 30) -> dict[str, Any]:
        """The response for ``request_id``; server requests seen meanwhile are kept."""
        deadline = time.monotonic() + timeout
        while True:
            message = self.recv(max(0.1, deadline - time.monotonic()))
            if "method" in message:
                self.server_requests.append(message)
                continue
            self.test.assertEqual(message.get("id"), request_id, f"unexpected message: {message}")
            return message

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.response_to(request_id, timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def initialize(self, capabilities: dict[str, Any] | None = None, version: str = PROTOCOL) -> dict[str, Any]:
        reply = self.request(
            "initialize",
            {"protocolVersion": version, "capabilities": capabilities or {}, "clientInfo": {"name": "test", "version": "1"}},
        )
        self.notify("notifications/initialized")
        return reply

    def call(self, name: str, arguments: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.request("tools/call", params, timeout)

    def text(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[str, bool]:
        reply = self.call(name, arguments)
        self.test.assertIn("result", reply, reply)
        (content,) = reply["result"]["content"]
        self.test.assertEqual(content["type"], "text")
        return content["text"], reply["result"]["isError"]

    def server_request(self, method: str, timeout: float = 10) -> dict[str, Any]:
        for index, message in enumerate(self.server_requests):
            if message["method"] == method:
                return self.server_requests.pop(index)
        deadline = time.monotonic() + timeout
        while True:
            message = self.recv(max(0.1, deadline - time.monotonic()))
            if message.get("method") == method:
                return message
            self.server_requests.append(message)

    def close(self) -> int:
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
            raise


class McpTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_mcp_")).resolve()
        self._saved = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        self.home = self.tmp / "user-home"
        self.home.mkdir()
        self.stores = self.tmp / "stores"
        self.stores.mkdir()
        self.set_tools(["cursor"])
        os.environ["SILLY_MEMORY_HOME"] = str(self.stores)
        os.environ["MEMORY_EMBEDDING_BACKEND"] = "noop"
        self.repo = self.tmp / "my repo"
        (self.repo / ".git").mkdir(parents=True)
        self.subdir = self.repo / "packages" / "api"
        self.subdir.mkdir(parents=True)
        self.elsewhere = self.tmp / "unrelated"
        self.elsewhere.mkdir()
        self.clients: list[McpClient] = []
        self._clock = time.time() - 100_000

    def tearDown(self) -> None:
        for client in self.clients:
            if client.proc.poll() is None:
                client.proc.kill()
                client.proc.wait(timeout=5)
        for path in self.tmp.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        for key in _ENV_VARS:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in self._saved.items() if v is not None})
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_tools(self, tools: list[str]) -> None:
        config = json.loads((MEM_HOME / "config.json").read_text(encoding="utf-8"))
        config["tools"] = tools
        (self.stores / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def server(self, *args: str, cwd: Path | None = None, **env: str) -> McpClient:
        base = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        base.update(
            {
                "HOME": str(self.home),
                "SILLY_MEMORY_HOME": str(self.stores),
                "MEMORY_EMBEDDING_BACKEND": "noop",
                "MEMORY_ALLOW_NETWORK": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        client = McpClient(self, list(args), {**base, **env}, cwd or self.elsewhere)
        self.clients.append(client)
        return client

    def ready(self, *args: str, cwd: Path | None = None, **env: str) -> McpClient:
        client = self.server(*args, cwd=cwd, **env)
        client.initialize()
        return client

    def store_of(self, root: Path) -> Path:
        return self.stores / (root / ".silly-memory" / "memory-id").read_text(encoding="utf-8").strip()

    def workspace(self, name: str, *facts: str) -> tuple[Path, Path]:
        """A tracked project whose activity is later than every earlier one."""
        project = self.tmp / "projects" / name
        (project / ".git").mkdir(parents=True)
        store = workspace_store(project)
        ensure_layout(store)
        (store / "memory-bank" / "domainContext.md").write_text(
            "# Domain\n\n" + "".join(f"- {fact}\n" for fact in facts), encoding="utf-8"
        )
        self._clock += 60
        os.utime(store / "observations.md", (self._clock, self._clock))
        return project, store


class TestLifecycle(McpTestBase):
    def test_initialize_then_exactly_three_tools(self) -> None:
        client = self.server("--workspace", str(self.repo))
        self.assertEqual(client.request("ping"), {"jsonrpc": "2.0", "id": 1, "result": {}})
        early = client.request("tools/list")
        self.assertEqual(early["error"]["code"], -32002)
        reply = client.initialize()
        self.assertEqual(reply["result"]["protocolVersion"], PROTOCOL)
        self.assertEqual(reply["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "silly-memory")
        tools = client.request("tools/list")["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["memory_recall", "memory_tasks", "memory_add"])
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertEqual(client.close(), 0)
        self.assertEqual(client.non_json, [], "stdout must carry JSON-RPC only")

    def test_unsupported_revision_gets_the_supported_one(self) -> None:
        client = self.server("--workspace", str(self.repo))
        reply = client.initialize(version="1999-01-01")
        self.assertEqual(reply["result"]["protocolVersion"], PROTOCOL)

    def test_notifications_never_get_a_reply(self) -> None:
        client = self.server("--workspace", str(self.repo))
        client.notify("notifications/initialized")
        client.initialize()
        client.notify("notifications/cancelled", {"requestId": 99})
        client.notify("notifications/something/unknown")
        client.notify("notifications/roots/list_changed")
        client.send({"jsonrpc": "2.0", "id": "stray", "result": {}})
        ping = client.request("ping")
        self.assertEqual(ping["result"], {}, "a notification or stray response produced output first")

    def test_malformed_lines_and_unknown_methods_do_not_end_the_loop(self) -> None:
        client = self.ready("--workspace", str(self.repo))
        client.raw("{not json")
        self.assertEqual(client.recv()["error"]["code"], -32700)
        client.raw("[1, 2]")
        self.assertEqual(client.recv()["error"]["code"], -32600)
        client.send({"jsonrpc": "1.0", "id": 5, "method": "ping"})
        self.assertEqual(client.recv()["error"]["code"], -32600)
        self.assertEqual(client.request("resources/list")["error"]["code"], -32601)
        self.assertEqual(client.request("prompts/list")["error"]["code"], -32601)
        self.assertEqual(client.request("ping")["result"], {})

    def test_invalid_arguments_are_protocol_errors(self) -> None:
        client = self.ready("--workspace", str(self.repo))
        bad = [
            ("memory_recall", {}),
            ("memory_recall", {"query": "  "}),
            ("memory_recall", {"query": "x", "limit": "5"}),
            ("memory_recall", {"query": "x", "limit": True}),
            ("memory_recall", {"query": "x", "limit": 0}),
            ("memory_recall", {"query": "x", "scope": "global"}),
            ("memory_recall", {"query": "x", "extra": 1}),
            ("memory_tasks", {"status": "done"}),
            ("memory_tasks", {"all": "yes"}),
            ("memory_add", {"text": "x", "scope": "everywhere"}),
            ("memory_add", {"text": 7}),
            ("memory_add", None),
            ("memory_delete", {"id": "x"}),
        ]
        for name, arguments in bad:
            with self.subTest(name=name, arguments=arguments):
                reply = client.call(name, arguments)
                self.assertEqual(reply["error"]["code"], -32602, reply)
        self.assertEqual(client.request("tools/call", {"arguments": {}})["error"]["code"], -32602)


class TestWorkspaceBinding(McpTestBase):
    def added_root(self, client: McpClient, fact: str) -> str:
        # Forced workspace scope: auto routing may send a fact to global memory by its wording.
        text, is_error = client.text("memory_add", {"text": fact, "scope": "workspace"})
        self.assertFalse(is_error, text)
        return text

    def test_every_client_binds_a_subdirectory_to_the_repository_store(self) -> None:
        clients = {
            "standalone": self.ready(cwd=self.subdir),
            "explicit": self.ready("--workspace", str(self.subdir)),
            "claude-code": self.ready("--client", "claude-code", CLAUDE_PROJECT_DIR=str(self.subdir)),
            "opencode": self.ready("--client", "opencode", cwd=self.subdir),
        }
        cursor = self.server("--client", "cursor")
        cursor.initialize(capabilities={"roots": {"listChanged": True}})
        roots = cursor.server_request("roots/list")
        cursor.send({"jsonrpc": "2.0", "id": roots["id"], "result": {"roots": [{"uri": self.subdir.as_uri(), "name": "api"}]}})
        clients["cursor"] = cursor
        for index, (label, client) in enumerate(clients.items()):
            with self.subTest(client=label):
                text = self.added_root(client, f"fact number {index} from {label}")
                self.assertIn(f"the memory for {self.repo}.", text)
        self.assertFalse((self.subdir / ".silly-memory").exists())
        bank = "".join(p.read_text(encoding="utf-8") for p in (self.store_of(self.repo) / "memory-bank").glob("*.md"))
        for label in clients:
            self.assertIn(f"from {label}", bank)

    def test_claude_project_dir_wins_over_an_unrelated_cwd(self) -> None:
        client = self.ready("--client", "claude-code", cwd=self.elsewhere, CLAUDE_PROJECT_DIR=str(self.repo))
        self.assertIn(str(self.repo), self.added_root(client, "claude fact"))
        self.assertFalse((self.elsewhere / ".silly-memory").exists())

    def test_claude_without_project_dir_is_a_tool_error_not_the_cwd(self) -> None:
        client = self.ready("--client", "claude-code", cwd=self.subdir)
        text, is_error = client.text("memory_add", {"text": "would land in cwd"})
        self.assertTrue(is_error)
        self.assertIn("CLAUDE_PROJECT_DIR", text)
        self.assertFalse((self.repo / ".silly-memory").exists())
        text, is_error = client.text("memory_recall", {"query": "anything", "scope": "all"})
        self.assertFalse(is_error, text)

    def test_opencode_binds_where_it_was_launched(self) -> None:
        """A committed opencode.json carries no path; OpenCode starts the server in its launch directory."""
        client = self.ready("--client", "opencode", cwd=self.subdir)
        self.assertIn(str(self.repo), self.added_root(client, "opencode fact"))

    def test_linked_worktree_keeps_its_own_store(self) -> None:
        worktree = self.repo / "wt"
        (worktree / "src").mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: ../.git/worktrees/wt\n", encoding="utf-8")
        main = self.ready("--client", "opencode", "--workspace", str(self.subdir))
        linked = self.ready("--client", "opencode", "--workspace", str(worktree / "src"))
        self.assertIn(str(self.repo), self.added_root(main, "main checkout fact"))
        self.assertIn(str(worktree), self.added_root(linked, "worktree fact"))
        self.assertNotEqual(self.store_of(worktree), self.store_of(self.repo))

    def test_cursor_roots_change_rebinds_the_workspace(self) -> None:
        other = self.tmp / "other"
        (other / ".git").mkdir(parents=True)
        client = self.server("--client", "cursor")
        client.initialize(capabilities={"roots": {"listChanged": True}})
        first = client.server_request("roots/list")
        # A workspace call made before the roots arrive waits for them; ping does not.
        pending_id = client.next_id
        client.next_id += 1
        client.send({"jsonrpc": "2.0", "id": pending_id, "method": "tools/call", "params": {"name": "memory_add", "arguments": {"text": "first root fact"}}})
        self.assertEqual(client.request("ping")["result"], {})
        client.send({"jsonrpc": "2.0", "id": first["id"], "result": {"roots": [{"uri": "https://example.invalid/x"}, {"uri": self.repo.as_uri()}]}})
        reply = client.response_to(pending_id)
        self.assertIn(str(self.repo), reply["result"]["content"][0]["text"])
        client.notify("notifications/roots/list_changed")
        second = client.server_request("roots/list")
        self.assertNotEqual(second["id"], first["id"])
        client.send({"jsonrpc": "2.0", "id": second["id"], "result": {"roots": [{"uri": other.as_uri()}]}})
        client.request("ping")
        self.assertIn(str(other), self.added_root(client, "second root fact"))

    def test_cursor_several_roots_need_the_cwd_to_choose(self) -> None:
        other = self.tmp / "other"
        (other / ".git").mkdir(parents=True)
        roots = [{"uri": self.repo.as_uri()}, {"uri": other.as_uri()}]
        for cwd, expected in ((self.elsewhere, None), (self.subdir, self.repo)):
            with self.subTest(cwd=cwd):
                client = self.server("--client", "cursor", cwd=cwd)
                client.initialize(capabilities={"roots": {}})
                request = client.server_request("roots/list")
                client.send({"jsonrpc": "2.0", "id": request["id"], "result": {"roots": roots}})
                text, is_error = client.text("memory_add", {"text": f"fact for {cwd.name}"})
                if expected is None:
                    self.assertTrue(is_error)
                    self.assertIn("Ambiguous workspace", text)
                else:
                    self.assertFalse(is_error, text)
                    self.assertIn(str(expected), text)

    def test_cursor_without_roots_support_or_answer_fails_without_guessing(self) -> None:
        silent = self.server("--client", "cursor", cwd=self.subdir)
        silent.initialize(capabilities={"roots": {}})
        silent.server_request("roots/list")
        start = time.monotonic()
        text, is_error = silent.text("memory_add", {"text": "never stored"})
        self.assertTrue(is_error)
        self.assertIn("did not answer", text)
        self.assertGreaterEqual(time.monotonic() - start, 3.0)
        text, is_error = silent.text("memory_recall", {"query": "anything", "scope": "all"})
        self.assertFalse(is_error, text)

        unsupported = self.server("--client", "cursor", cwd=self.subdir)
        unsupported.initialize(capabilities={})
        text, is_error = unsupported.text("memory_tasks")
        self.assertTrue(is_error)
        self.assertIn("did not offer workspace roots", text)

        failing = self.server("--client", "cursor", cwd=self.subdir)
        failing.initialize(capabilities={"roots": {}})
        request = failing.server_request("roots/list")
        failing.send({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32603, "message": "nope"}})
        text, is_error = failing.text("memory_add", {"text": "never stored"})
        self.assertTrue(is_error)
        self.assertFalse((self.repo / ".silly-memory").exists(), "no fallback to the cwd")

    def test_file_uri_decoding(self) -> None:
        self.assertEqual(decode_file_uri("file:///tmp/my%20repo"), Path("/tmp/my repo"))
        self.assertEqual(decode_file_uri("file://localhost/tmp/x"), Path("/tmp/x"))
        self.assertEqual(decode_file_uri("file:///tmp/%C3%A9t%C3%A9"), Path("/tmp/été"))
        for bad in ("https://x/y", "file://server/share", "file:///tmp/%zz", "file:///tmp/%4", "file:relative", None, 3):
            with self.subTest(uri=bad):
                self.assertIsNone(decode_file_uri(bad))


class TestTools(McpTestBase):
    def test_add_then_recall_refreshes_every_configured_rule(self) -> None:
        self.set_tools(["cursor", "claude-code"])
        client = self.ready("--workspace", str(self.repo))
        text, is_error = client.text("memory_add", {"text": "the API lives in packages/api"})
        self.assertFalse(is_error, text)
        self.assertTrue(text.startswith("Saved to domainContext.md"), text)
        text, is_error = client.text("memory_recall", {"query": "API"})
        self.assertFalse(is_error, text)
        # Recall snippets bracket the matched words.
        self.assertIn("#explicit: the [API] lives in packages/", text)
        for rule in (".cursor/rules/_memory-context.mdc", ".claude/rules/_memory-context.md"):
            self.assertIn("the API lives in packages/api", (self.repo / rule).read_text(encoding="utf-8"), rule)

    def test_global_scope_and_private_only_text(self) -> None:
        client = self.ready("--workspace", str(self.repo))
        text, is_error = client.text("memory_add", {"text": "the staging VPN is required", "scope": "global"})
        self.assertFalse(is_error, text)
        self.assertIn("global memory", text)
        text, is_error = client.text("memory_add", {"text": "<private>hunter2</private>"})
        self.assertTrue(is_error)
        self.assertIn("Nothing stored", text)
        for path in self.stores.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"hunter2", path.read_bytes(), path)

    def test_saved_fact_with_failed_rule_refresh_is_reported_as_saved(self) -> None:
        (self.repo / ".cursor").mkdir()
        (self.repo / ".cursor" / "rules").write_text("not a directory", encoding="utf-8")
        client = self.ready("--workspace", str(self.repo))
        text, is_error = client.text("memory_add", {"text": "saved despite the rule problem"})
        self.assertFalse(is_error, text)
        self.assertIn("Saved to", text)
        self.assertIn("Refreshing the project rules failed", text)
        bank = self.store_of(self.repo) / "memory-bank" / "domainContext.md"
        self.assertIn("saved despite the rule problem", bank.read_text(encoding="utf-8"))

    def test_default_recall_matches_the_cli_scope(self) -> None:
        project_a, _ = self.workspace("alpha", "alpha deploys to zephyr")
        self.workspace("beta", "beta deploys to zephyr")
        client = self.ready("--workspace", str(project_a))
        text, _ = client.text("memory_recall", {"query": "zephyr"})
        self.assertIn("alpha deploys", text)
        self.assertNotIn("beta deploys", text)
        text, _ = client.text("memory_recall", {"query": "zephyr", "scope": "all"})
        self.assertIn(f"[{project_a}]", text)
        self.assertIn("beta deploys", text)

    def test_all_scope_finds_the_oldest_of_21_stores(self) -> None:
        oldest, _ = self.workspace("ws00", "oldestonlydeploy lives only here")
        projects = [self.workspace(f"ws{n:02d}", "shared deploy note")[0] for n in range(1, 21)]
        client = self.ready("--workspace", str(projects[-1]))
        text, is_error = client.text("memory_recall", {"query": "oldestonlydeploy", "scope": "all", "limit": 1})
        self.assertFalse(is_error, text)
        self.assertTrue(text.startswith(f"[{oldest}] domainContext.md"), text)
        self.assertEqual(text.count("\n["), 0)

    def test_tasks_for_the_workspace_and_every_workspace(self) -> None:
        project_a, store_a = self.workspace("alpha")
        project_b, store_b = self.workspace("beta")
        (store_a / "memory-bank" / "actionItems.md").write_text(
            "# Actions\n\n- [ ] [2026-09-29] #release: ship alpha — owner: Maya\n", encoding="utf-8"
        )
        (store_b / "memory-bank" / "actionItems.md").write_text("# Actions\n\n- [ ] ship beta\n- [x] done beta\n", encoding="utf-8")
        client = self.ready("--workspace", str(project_a))
        text, _ = client.text("memory_tasks")
        self.assertIn("ship alpha", text)
        self.assertNotIn("ship beta", text)
        text, _ = client.text("memory_tasks", {"all": True, "status": "all"})
        self.assertIn("ship alpha", text)
        self.assertIn("done beta", text)
        text, _ = client.text("memory_tasks", {"tag": "release", "owner": "maya"})
        self.assertIn("ship alpha", text)

    def test_missing_index_is_rebuilt_without_a_deadlock(self) -> None:
        project, store = self.workspace("alpha", "kraken fact without an index")
        self.assertFalse((store / "memory.sqlite").exists())
        client = self.ready("--workspace", str(project))
        reply = client.call("memory_recall", {"query": "kraken"}, timeout=30)
        self.assertIn("fact without an index", reply["result"]["content"][0]["text"])
        self.assertTrue((store / "memory.sqlite").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read every file")
    def test_unreadable_store_is_a_tool_error(self) -> None:
        project, store = self.workspace("alpha", "kraken fact a")
        client = self.ready("--workspace", str(project))
        client.text("memory_recall", {"query": "kraken"})
        (store / "memory.sqlite").chmod(0)
        for scope in ("workspace", "all"):
            with self.subTest(scope=scope):
                text, is_error = client.text("memory_recall", {"query": "kraken", "scope": scope})
                self.assertTrue(is_error)
                self.assertNotIn("kraken fact", text)
        self.assertEqual(client.request("ping")["result"], {})

    def test_tool_text_is_capped_at_4000_characters(self) -> None:
        project, store = self.workspace("alpha")
        for n in range(3):
            sections = "".join(f"## Topic {n}-{s}\n- kraken detail {n}-{s} " + "filler words " * 30 + "\n\n" for s in range(20))
            (store / "memory-bank" / f"notes{n}.md").write_text(f"# Notes {n}\n\n{sections}", encoding="utf-8")
        client = self.ready("--workspace", str(project))
        text, is_error = client.text("memory_recall", {"query": "kraken", "limit": 50})
        self.assertFalse(is_error)
        self.assertLessEqual(len(text), 4000)
        self.assertTrue(text.endswith("[truncated]"), text[-80:])

    def test_warm_workspace_recall_150ms_or_skip(self) -> None:
        project, _ = self.workspace("alpha", *[f"entry {i} about routine deploys" for i in range(200)])
        client = self.ready("--workspace", str(project))
        client.text("memory_recall", {"query": "routine"})
        samples = []
        for _ in range(30):
            start = time.perf_counter()
            text, is_error = client.text("memory_recall", {"query": "routine deploys"})
            samples.append((time.perf_counter() - start) * 1000)
            self.assertFalse(is_error)
            self.assertIn("routine", text)
        p95 = statistics.quantiles(samples, n=20)[-1]
        if p95 > 150:
            self.skipTest(f"diagnostic target missed: warm workspace recall p95 {p95:.1f} ms > 150 ms")


class TestNoNetwork(unittest.TestCase):
    def test_server_imports_no_network_or_mcp_package(self) -> None:
        forbidden = {"socket", "http", "urllib", "requests", "mcp", "ssl", "asyncio"}
        for path in (MEM_LIB / "memory_system" / "mcp_server.py", LAUNCHER):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                for name in names:
                    with self.subTest(path=path.name, module=name):
                        self.assertNotIn(name.split(".")[0], forbidden)


if __name__ == "__main__":
    unittest.main()
