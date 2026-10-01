"""The OpenCode v2 plugin, driven in node against a contract-shaped fake context.

The fake context mirrors the OpenCode v2.0.18 shapes this plugin relies on:
``ctx.session.get`` returns ``location.directory``; ``ctx.session.context`` returns
the message array directly, with completed assistant messages carrying
``time.completed`` and ``{type: "text"}`` content; ``execute.after`` events carry
``sessionID``/``messageID``/``id``/``tool``/``input``/``status``/``result``; the
server-wide stream delivers ``session.execution.*`` and ``session.deleted`` events
with ``data.sessionID`` and never reconnects on its own; and compaction records have
an ``id`` plus ``status`` (the context holds history from the latest completed
compaction onward).

A recording fake of the ``memory`` CLI stands in for the engine and implements the
capture acknowledgment, ``process`` exit codes (75 = busy), and the tokenized
compaction handoff. These tests prove the plugin logic only; the final scenario QA
loads the installed plugin in a real OpenCode v2.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "opencode-extras" / "plugins" / "silly-memory.js"
NODE = shutil.which("node")

_FAKE_MEMORY = r'''
import fcntl, json, os, sys, time, uuid
from pathlib import Path

cfg = json.loads(Path(os.environ["FAKE_MEMORY_CONFIG"]).read_text())
argv = sys.argv[1:]
payload = None
if argv[:1] == ["hook"]:
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else None
event = (payload or {}).get("hook_event_name")
key = f"hook:{event}" if argv[:1] == ["hook"] else argv[0]
reply, code = "{}", 0
with open(cfg["state"] + ".lock", "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state_path = Path(cfg["state"])
    state = json.loads(state_path.read_text()) if state_path.exists() else {"counts": {}, "note": None}
    n = state["counts"].get(key, 0)
    state["counts"][key] = n + 1
    with open(cfg["log"], "a") as log:
        log.write(json.dumps({"argv": argv, "payload": payload}) + "\n")
    if argv[:1] == ["process"]:
        codes = cfg.get("process_exit", [])
        code = codes[n] if n < len(codes) else 0
    elif payload is not None and n >= cfg.get("fail", {}).get(event, 0):
        root = payload["workspace_roots"][0]
        mapped = cfg.get("roots", {}).get(root, {"workspace_root": root, "store_path": cfg["store"]})
        response = {"memory_capture": {"status": "ok", "event_ids": [uuid.uuid4().hex], **mapped}}
        if event == "preCompact" and cfg.get("handoff_text") and n < cfg.get("handoff_writes", 99):
            state["note"] = {"token": payload.get("handoff_token"), "text": cfg["handoff_text"]}
        if event == "sessionStart":
            if payload.get("session_source") == "compact":
                note = state["note"]
                if note and note["token"] == payload.get("handoff_token"):
                    response["additional_context"] = "Context restored after compaction\n" + note["text"]
                    state["note"] = None
            else:
                response["additional_context"] = "top-up hint"
        reply = json.dumps(response)
    state_path.write_text(json.dumps(state))
slow = cfg.get("slow", {}).get(key)
if slow and n < slow[0]:
    time.sleep(slow[1])
print(reply)
sys.exit(code)
'''

_HARNESS = r'''
import { pathToFileURL } from "node:url"
import { readFileSync, existsSync } from "node:fs"

const plugin = (await import(pathToFileURL(process.env.PLUGIN_PATH).href)).default

function makeBus() {
  const subs = new Set()
  const bus = {
    subscriptions: 0,
    subscribe({ signal } = {}) {
      bus.subscriptions++
      const sub = { queue: [], wake: undefined, ended: false }
      subs.add(sub)
      return (async function* () {
        try {
          while (!signal?.aborted) {
            if (sub.queue.length) {
              const item = sub.queue.shift()
              yield item.event
              item.delivered()
              continue
            }
            if (sub.ended) return
            await new Promise((wake) => {
              sub.wake = wake
              signal?.addEventListener("abort", wake, { once: true })
            })
          }
        } finally {
          subs.delete(sub)
        }
      })()
    },
    emit(event) {
      return Promise.all([...subs].map((sub) => new Promise((delivered) => {
        sub.queue.push({ event, delivered })
        sub.wake?.()
      })))
    },
    disconnect() {
      for (const sub of subs) {
        sub.ended = true
        sub.wake?.()
      }
    },
  }
  return bus
}

const h = {
  sessions: {},
  mcp: new Map(),
  registered: [],
  disposed: [],
  thrown: [],
  out: {},
  bus: makeBus(),
  sleep: (ms) => new Promise((done) => setTimeout(done, ms)),
  session(id, directory, messages = []) {
    h.sessions[id] = { directory, messages, deleted: false }
    return h.sessions[id]
  },
  user: (id, text) => ({ type: "user", id, text, time: { created: Date.now() } }),
  assistant: (id, text, completedAt = Date.now()) => ({
    type: "assistant",
    id,
    agent: "build",
    model: { providerID: "test", id: "model" },
    time: completedAt ? { created: completedAt - 1, completed: completedAt } : { created: 1 },
    content: [{ type: "reasoning", text: "hidden" }, { type: "text", text }, { type: "tool", id: "call", name: "read" }],
  }),
  compactionRecord: (id, status) => ({ type: "compaction", id, status, reason: "auto", time: { created: 3 }, summary: "", recent: "" }),
  emit: (type, sessionID, extra = {}) => h.bus.emit({ id: `evt_${Math.random()}`, type, created: Date.now(), data: { sessionID, ...extra } }),
  async subscribed(count = 1) {
    for (let i = 0; i < 600 && h.bus.subscriptions < count; i++) await h.sleep(5)
  },
  async load(directory) {
    const hooks = { session: {}, tool: {} }
    const registration = (name, remove) => ({ dispose: async () => { remove(); h.disposed.push(name) } })
    const ctx = {
      app: { version: "2.0.18" },
      options: {},
      location: { directory, project: { id: "prj_test", directory, canonical: directory } },
      session: {
        async get({ sessionID }) {
          const s = h.sessions[sessionID]
          if (!s || s.deleted) throw new Error("Session not found")
          return { id: sessionID, location: { directory: s.directory } }
        },
        async context({ sessionID }) {
          const s = h.sessions[sessionID]
          if (!s || s.deleted) throw new Error("Session not found")
          return structuredClone(s.messages)
        },
        async hook(name, callback) {
          hooks.session[name] = callback
          h.registered.push(`session.${name}`)
          return registration(`session.${name}`, () => delete hooks.session[name])
        },
      },
      tool: {
        async hook(name, callback) {
          hooks.tool[name] = callback
          h.registered.push(`tool.${name}`)
          return registration(`tool.${name}`, () => delete hooks.tool[name])
        },
      },
      event: { subscribe: (options) => h.bus.subscribe(options) },
      mcp: {
        async transform(callback) {
          callback({
            list: () => [...h.mcp.entries()],
            get: (name) => h.mcp.get(name),
            set: (name, config) => h.mcp.set(name, config),
            update: (name, update) => update(h.mcp.get(name)),
            remove: (name) => h.mcp.delete(name),
          })
          h.registered.push("mcp.transform")
          return registration("mcp", () => {})
        },
      },
    }
    const call = async (kind, name, event) => {
      try {
        await hooks[kind][name]?.(event)
      } catch (error) {
        h.thrown.push(`${kind}.${name}: ${error?.message ?? error}`)
      }
      return event
    }
    const before = h.bus.subscriptions
    const cleanup = await plugin.setup(ctx)
    await h.subscribed(before + 1)
    return {
      ctx,
      prompt: (sessionID, messageID, text) =>
        call("session", "prompt", { sessionID, messageID, prompt: { text, files: [] }, metadata: {}, delivery: "steer" }),
      async context(sessionID) {
        const event = await call("session", "context", {
          sessionID, agent: "build", model: { providerID: "test", id: "model" }, system: [], messages: [], tools: {}, options: {},
        })
        return event.system.map((part) => part.text)
      },
      async compaction(sessionID) {
        const event = await call("session", "compaction", {
          sessionID, agent: "build", model: { providerID: "test", id: "model" }, system: [], messages: [], tools: {}, options: {},
        })
        return event.system.map((part) => part.text)
      },
      toolAfter: (event) => call("tool", "execute.after", { agent: "build", ...event }),
      raw: (kind, name, event) => call(kind, name, event),
      unload: () => cleanup?.(),
    }
  },
}

export async function run(scenario) {
  await scenario(h)
  const log = process.env.FAKE_LOG
  const calls = existsSync(log)
    ? readFileSync(log, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line))
    : []
  process.stdout.write(JSON.stringify({
    calls, out: h.out, thrown: h.thrown, mcp: Object.fromEntries(h.mcp), registered: h.registered, disposed: h.disposed,
  }))
  process.exit(0)
}
'''

_RULE = "---\ndescription: memory recall\nalwaysApply: true\n---\n# Memory recall\n\nPrefer the memory_recall tool.\n"
_PACK = "# Memory Context Pack\n\n- deploy target is zephyr-stage\n"


@unittest.skipUnless(NODE, "node is not installed; the OpenCode plugin tests run the plugin in node")
class OpenCodePluginTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="memtest_opencode_")).resolve()
        self.home = self.tmp / "memory-home"
        rules = self.home / "cursor-extras" / "rules"
        rules.mkdir(parents=True)
        (rules / "memory-recall.mdc").write_text(_RULE, encoding="utf-8")
        (self.home / "bin").mkdir()
        self.fake_cli = self.home / "bin" / "memory"
        self.fake_cli.write_text(_FAKE_MEMORY, encoding="utf-8")
        self.store = self.tmp / "store"
        self.store.mkdir()
        (self.store / "context-pack.md").write_text(_PACK, encoding="utf-8")
        self.project = self.tmp / "project"
        self.project.mkdir()
        (self.tmp / "harness.mjs").write_text(_HARNESS, encoding="utf-8")
        self.log = self.tmp / "fake-cli.jsonl"
        self.config: dict[str, object] = {
            "log": str(self.log),
            "state": str(self.tmp / "fake-state.json"),
            "store": str(self.store),
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scenario(self, body: str, new_session: bool = False, **env: str) -> dict:
        config_path = self.tmp / "fake-config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        script = self.tmp / f"scenario-{self._testMethodName}.mjs"
        script.write_text(
            'import { run } from "./harness.mjs"\n'
            f"const PROJECT = {json.dumps(str(self.project))}\n"
            f"const TMP = {json.dumps(str(self.tmp))}\n"
            f"await run(async (h) => {{\n{body}\n}})\n",
            encoding="utf-8",
        )
        base = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp / "user-home"),
            "SILLY_MEMORY_HOME": str(self.home),
            "MEMORY_PY": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PLUGIN_PATH": str(PLUGIN),
            "FAKE_MEMORY_CONFIG": str(config_path),
            "FAKE_LOG": str(self.log),
        }
        proc = subprocess.run(
            [NODE, str(script)], env={**base, **env}, capture_output=True, text=True, timeout=120,
            start_new_session=new_session,
        )
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        result = json.loads(proc.stdout)
        self.assertEqual(result["thrown"], [], "plugin handlers must never throw")
        return result

    @staticmethod
    def hooks(result: dict, session: str | None = None) -> list[str]:
        out = []
        for call in result["calls"]:
            payload = call["payload"]
            if payload is None:
                out.append(" ".join(call["argv"]))
            elif session is None or payload.get("conversation_id") == session:
                out.append(payload["hook_event_name"])
        return out


class TestPluginShape(OpenCodePluginTestBase):
    def test_default_export_is_a_dependency_free_v2_definition(self) -> None:
        source = PLUGIN.read_text(encoding="utf-8")
        self.assertNotRegex(source, r'from\s+"@opencode/plugin"', "a runtime import would not resolve for a direct file")
        self.assertNotIn("server(", source)
        self.assertFalse((PLUGIN.parent.parent / "package.json").exists())
        result = self.scenario(
            'const mod = (await import(process.env.PLUGIN_PATH)).default\n'
            'h.out.keys = Object.keys(mod).sort()\n'
            'h.out.id = mod.id\n'
            'const p = await h.load(PROJECT)\n'
            'await p.unload()\n'
        )
        self.assertEqual(result["out"], {"keys": ["id", "setup"], "id": "silly-memory"})
        for name in ("session.prompt", "session.context", "session.compaction", "tool.execute.after"):
            self.assertIn(name, result["registered"])
            self.assertIn(name, result["disposed"])


class TestContextInjection(OpenCodePluginTestBase):
    def test_context_injects_pack_and_recall_rule_after_session_start(self) -> None:
        result = self.scenario(
            'h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            'h.out.system = await p.context("ses_a")\n'
            'await p.unload()\n'
        )
        self.assertEqual(result["out"]["system"], [_PACK.strip(), "# Memory recall\n\nPrefer the memory_recall tool."])
        start = result["calls"][0]
        self.assertEqual(start["argv"], ["hook", "--tool", "opencode"])
        self.assertEqual(start["payload"]["hook_event_name"], "sessionStart")
        self.assertEqual(start["payload"]["workspace_roots"], [str(self.project)])
        self.assertEqual(start["payload"]["conversation_id"], "ses_a")

    def test_missing_engine_fails_open_without_injection(self) -> None:
        result = self.scenario(
            'h.session("ses_a", PROJECT, [h.assistant("msg_a", "done")])\n'
            'const p = await h.load(PROJECT)\n'
            'await p.prompt("ses_a", "msg_u", "hello")\n'
            'h.out.system = await p.context("ses_a")\n'
            'h.out.compaction = await p.compaction("ses_a")\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await p.toolAfter({ sessionID: "ses_a", messageID: "msg_a", id: "call_1", tool: "shell", input: { command: "ls" }, status: "completed", result: {} })\n'
            'await p.unload()\n',
            MEMORY_BIN=str(self.tmp / "missing" / "memory"),
            SILLY_MEMORY_HOME=str(self.tmp / "no-engine-here"),
        )
        self.assertEqual(result["out"], {"system": [], "compaction": []})
        self.assertEqual(result["calls"], [])

    def test_unreadable_session_never_throws(self) -> None:
        result = self.scenario(
            'const p = await h.load(PROJECT)\n'
            'h.out.system = await p.context("ses_unknown")\n'
            'await p.prompt("ses_unknown", "msg_u", "hello")\n'
            'await p.unload()\n'
        )
        self.assertEqual(result["out"]["system"], [])
        self.assertEqual(result["calls"], [])


class TestCapture(OpenCodePluginTestBase):
    def test_prompt_is_captured_once_after_session_start(self) -> None:
        result = self.scenario(
            'h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            'await p.prompt("ses_a", "msg_u1", "remember that we deploy from main")\n'
            'await p.prompt("ses_a", "msg_u1", "remember that we deploy from main")\n'
            'await p.unload()\n'
        )
        self.assertEqual(self.hooks(result), ["sessionStart", "beforeSubmitPrompt"])
        prompt = result["calls"][1]["payload"]
        self.assertEqual(prompt["prompt"], "remember that we deploy from main")
        self.assertEqual(prompt["message_id"], "msg_u1")
        self.assertEqual(prompt["tool"], "opencode")

    def test_final_reply_is_captured_then_stop_then_process_without_another_prompt(self) -> None:
        resolved = self.tmp / "repo-root"
        self.config["roots"] = {str(self.project): {"workspace_root": str(resolved), "store_path": str(self.store)}}
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT, [h.user("msg_u1", "fix it")])\n'
            'const p = await h.load(PROJECT)\n'
            'await p.prompt("ses_a", "msg_u1", "fix it")\n'
            's.messages.push(h.assistant("msg_a1", "Fixed the parser."))\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await p.unload()\n'
        )
        hooks = self.hooks(result)
        self.assertEqual(hooks[:5], ["sessionStart", "beforeSubmitPrompt", "afterAgentResponse", "stop", f"process --workspace {resolved}"])
        self.assertEqual(hooks.count("afterAgentResponse"), 1, "duplicate delivery must not capture a reply twice")
        reply = result["calls"][2]["payload"]
        self.assertEqual(reply["text"], "Fixed the parser.")
        self.assertEqual(reply["message_id"], "msg_a1")
        self.assertNotIn("sessionEnd", hooks, "unload is not a session end")

    def _replies(self, result: dict) -> list[str]:
        return [
            c["payload"]["message_id"]
            for c in result["calls"]
            if c["payload"] and c["payload"]["hook_event_name"] == "afterAgentResponse"
        ]

    def test_failed_capture_is_retried_and_not_checkpointed(self) -> None:
        self.config["fail"] = {"afterAgentResponse": 1}
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            's.messages.push(h.assistant("msg_a1", "First reply."))\n'
            'await p.context("ses_a")\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await p.unload()\n'
        )
        self.assertEqual(self._replies(result), ["msg_a1", "msg_a1"])

    def test_timed_out_capture_is_not_checkpointed_and_is_retried(self) -> None:
        self.config["slow"] = {"hook:afterAgentResponse": [1, 8]}
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            'await p.context("ses_a")\n'
            's.messages.push(h.assistant("msg_a1", "Slow to save."))\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await p.context("ses_a")\n'
            'await p.unload()\n'
        )
        self.assertEqual(self._replies(result), ["msg_a1", "msg_a1"])
        checkpoints = list((self.store / "opencode").glob("*.json"))
        saved = [json.loads(p.read_text(encoding="utf-8")) for p in checkpoints if not p.name.endswith(".intent.json")]
        self.assertEqual([c["assistants"] for c in saved], [["msg_a1"]])

    def test_a_reply_being_saved_survives_the_signal_that_ends_opencode_run(self) -> None:
        """`opencode run` signals its process group as it exits, which killed the final reply's capture."""
        self.config["slow"] = {"hook:afterAgentResponse": [1, 1.5]}
        result = self.scenario(
            'process.on("SIGTERM", () => {})\n'
            'const s = h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            'await p.context("ses_a")\n'
            's.messages.push(h.assistant("msg_a1", "Final reply."))\n'
            'const ended = h.emit("session.execution.succeeded", "ses_a")\n'
            'await h.sleep(500)\n'
            'process.kill(-process.pid, "SIGTERM")\n'
            'await ended\n'
            'await p.unload()\n',
            new_session=True,
        )
        self.assertEqual(self._replies(result), ["msg_a1"])
        checkpoints = list((self.store / "opencode").glob("*.json"))
        saved = [json.loads(p.read_text(encoding="utf-8")) for p in checkpoints if not p.name.endswith(".intent.json")]
        self.assertEqual([c["assistants"] for c in saved], [["msg_a1"]])

    def test_history_before_first_sight_is_not_replayed(self) -> None:
        result = self.scenario(
            'const old = Date.now() - 60_000\n'
            'const s = h.session("ses_a", PROJECT, [h.assistant("msg_old1", "Old reply one.", old), h.assistant("msg_old2", "Old reply two.", old)])\n'
            'const p = await h.load(PROJECT)\n'
            'await p.context("ses_a")\n'
            's.messages.push(h.assistant("msg_new", "New reply."))\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await p.unload()\n'
        )
        self.assertEqual(self._replies(result), ["msg_new"])

    def test_reload_reconciles_without_recapturing(self) -> None:
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT)\n'
            'let p = await h.load(PROJECT)\n'
            's.messages.push(h.assistant("msg_a1", "Earlier reply."))\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await p.unload()\n'
            's.messages.push(h.assistant("msg_a2", "Missed while unloaded."))\n'
            'p = await h.load(PROJECT)\n'
            'await p.context("ses_a")\n'
            'await p.unload()\n'
        )
        replies = [c["payload"]["message_id"] for c in result["calls"] if c["payload"] and c["payload"]["hook_event_name"] == "afterAgentResponse"]
        self.assertEqual(replies, ["msg_a1", "msg_a2"])

    def test_stream_gap_is_reconciled_on_next_callback(self) -> None:
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            'await p.context("ses_a")\n'
            'h.bus.disconnect()\n'
            's.messages.push(h.assistant("msg_a1", "Finished during the gap."))\n'
            'await h.subscribed(2)\n'
            'await p.context("ses_a")\n'
            'h.out.subscriptions = h.bus.subscriptions\n'
            'await p.unload()\n'
        )
        self.assertGreaterEqual(result["out"]["subscriptions"], 2, "the plugin must resubscribe after the stream ends")
        self.assertIn("afterAgentResponse", self.hooks(result))

    def test_busy_process_is_retried_at_the_next_callback(self) -> None:
        self.config["process_exit"] = [75, 0]
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            's.messages.push(h.assistant("msg_a1", "Reply."))\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await p.context("ses_a")\n'
            'await p.context("ses_a")\n'
            'await p.unload()\n'
        )
        processes = [c for c in result["calls"] if c["argv"][0] == "process"]
        self.assertEqual(len(processes), 2)

    def test_failed_process_is_not_retried_inside_model_calls(self) -> None:
        self.config["process_exit"] = [1]
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            's.messages.push(h.assistant("msg_a1", "Reply."))\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await p.context("ses_a")\n'
            'await p.context("ses_a")\n'
            'await p.unload()\n'
        )
        processes = [c for c in result["calls"] if c["argv"][0] == "process"]
        self.assertEqual(len(processes), 1)

    def test_malformed_events_and_hooks_do_nothing_and_never_throw(self) -> None:
        result = self.scenario(
            'const p = await h.load(PROJECT)\n'
            'await h.bus.emit(null)\n'
            'await h.bus.emit({ type: "session.execution.succeeded" })\n'
            'await h.bus.emit({ type: "session.execution.failed", data: { sessionID: 42 } })\n'
            'await h.bus.emit({ type: "session.unknown.thing", data: { sessionID: "ses_x" } })\n'
            'await h.emit("session.execution.succeeded", "ses_unknown")\n'
            'await p.toolAfter({ status: "completed", tool: "edit" })\n'
            'await p.toolAfter({ sessionID: "ses_unknown", id: "c", tool: "patch", status: "completed", result: null })\n'
            'await p.toolAfter({ sessionID: "ses_unknown", id: "c", tool: "shell", input: { command: 7 }, status: "completed" })\n'
            'await p.raw("session", "context", { sessionID: 5 })\n'
            'await p.raw("session", "prompt", { sessionID: "ses_unknown" })\n'
            'await p.raw("session", "compaction", {})\n'
            'await h.sleep(50)\n'
            'await p.unload()\n'
        )
        self.assertEqual(result["calls"], [])

    def test_tool_results_become_edit_and_shell_observations(self) -> None:
        result = self.scenario(
            'h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            'await p.toolAfter({ sessionID: "ses_a", messageID: "msg_a", id: "call_edit", tool: "edit", input: { path: "src/a.ts", oldString: "a", newString: "b" }, status: "completed", result: { content: [] } })\n'
            'await p.toolAfter({ sessionID: "ses_a", messageID: "msg_a", id: "call_edit", tool: "edit", input: { path: "src/a.ts" }, status: "completed", result: { content: [] } })\n'
            'await p.toolAfter({ sessionID: "ses_a", messageID: "msg_a", id: "call_patch", tool: "patch", input: { patchText: "*** Begin Patch" }, status: "completed", result: { output: { applied: [], files: [{ file: `${PROJECT}/b.ts` }, { file: `${PROJECT}/c.md` }] }, content: [] } })\n'
            'await p.toolAfter({ sessionID: "ses_a", messageID: "msg_a", id: "call_sh", tool: "shell", input: { command: "npm test" }, status: "completed", result: { content: [] } })\n'
            'await p.toolAfter({ sessionID: "ses_a", messageID: "msg_a", id: "call_bad", tool: "write", input: { path: "x" }, status: "error", error: { message: "denied" } })\n'
            'await p.context("ses_a")\n'
            'await p.unload()\n'
        )
        observed = [
            (c["payload"]["hook_event_name"], c["payload"].get("file_path") or c["payload"].get("command"))
            for c in result["calls"]
            if c["payload"] and c["payload"]["hook_event_name"] in ("afterFileEdit", "afterShellExecution")
        ]
        self.assertEqual(
            observed,
            [
                ("afterFileEdit", str(self.project / "src" / "a.ts")),
                ("afterFileEdit", str(self.project / "b.ts")),
                ("afterFileEdit", str(self.project / "c.md")),
                ("afterShellExecution", "npm test"),
            ],
        )


class TestSessionsAndLocations(OpenCodePluginTestBase):
    def test_two_worktree_sessions_stay_in_their_own_plugin_instance_and_store(self) -> None:
        wt1, wt2 = self.tmp / "wt1", self.tmp / "wt2"
        store2 = self.tmp / "store2"
        store2.mkdir()
        (store2 / "context-pack.md").write_text("# Memory Context Pack\n\n- worktree two\n", encoding="utf-8")
        self.config["roots"] = {
            str(wt1): {"workspace_root": str(wt1), "store_path": str(self.store)},
            str(wt2): {"workspace_root": str(wt2), "store_path": str(store2)},
        }
        result = self.scenario(
            f'const s1 = h.session("ses_1", {json.dumps(str(wt1))})\n'
            f'const s2 = h.session("ses_2", {json.dumps(str(wt2))})\n'
            f'const p1 = await h.load({json.dumps(str(wt1))})\n'
            f'const p2 = await h.load({json.dumps(str(wt2))})\n'
            's1.messages.push(h.assistant("msg_1", "one"))\n'
            's2.messages.push(h.assistant("msg_2", "two"))\n'
            'h.out.one = await p1.context("ses_1")\n'
            'h.out.two = await p2.context("ses_2")\n'
            'await h.emit("session.execution.succeeded", "ses_1")\n'
            'await h.emit("session.execution.succeeded", "ses_2")\n'
            'await p1.unload()\n'
            'await p2.unload()\n'
        )
        self.assertIn("- deploy target is zephyr-stage", result["out"]["one"][0])
        self.assertIn("- worktree two", result["out"]["two"][0])
        processes = sorted(" ".join(c["argv"]) for c in result["calls"] if c["argv"][0] == "process")
        self.assertEqual(processes, [f"process --workspace {wt1}", f"process --workspace {wt2}"])
        for session, root in (("ses_1", wt1), ("ses_2", wt2)):
            replies = [c for c in result["calls"] if c["payload"] and c["payload"]["hook_event_name"] == "afterAgentResponse" and c["payload"]["conversation_id"] == session]
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0]["payload"]["workspace_roots"], [str(root)])

    def test_deleted_session_ends_with_cached_attribution_only(self) -> None:
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            'await p.context("ses_a")\n'
            's.deleted = true\n'
            'await h.emit("session.deleted", "ses_a")\n'
            'await h.emit("session.deleted", "ses_never_seen")\n'
            'await p.unload()\n'
        )
        self.assertEqual(self.hooks(result), ["sessionStart", "sessionEnd"])
        self.assertEqual(result["calls"][1]["payload"]["workspace_roots"], [str(self.project)])


class TestCompaction(OpenCodePluginTestBase):
    def test_replies_are_drained_before_compaction_hides_them(self) -> None:
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT)\n'
            'const p = await h.load(PROJECT)\n'
            'await p.prompt("ses_a", "msg_u", "go")\n'
            's.messages = [h.user("msg_u", "go"), h.assistant("msg_A", "Step A done.")]\n'
            'await p.compaction("ses_a")\n'
            's.messages = [h.compactionRecord("cmp_1", "completed"), h.assistant("msg_B", "Step B done.")]\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await h.emit("session.execution.succeeded", "ses_a")\n'
            'await p.unload()\n'
        )
        hooks = self.hooks(result)
        self.assertLess(hooks.index("afterAgentResponse"), hooks.index("preCompact"), "reply A must be saved before compaction")
        replies = [c["payload"]["message_id"] for c in result["calls"] if c["payload"] and c["payload"]["hook_event_name"] == "afterAgentResponse"]
        self.assertEqual(replies, ["msg_A", "msg_B"])

    def test_handoff_restores_once_after_the_recorded_compaction_completes(self) -> None:
        self.config["handoff_text"] = "- open task: ship 3.0"
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT, [h.compactionRecord("cmp_1", "running")])\n'
            'const p = await h.load(PROJECT)\n'
            'h.out.during = await p.compaction("ses_a")\n'
            'h.out.running = await p.context("ses_a")\n'
            's.messages = [h.compactionRecord("cmp_1", "completed")]\n'
            'h.out.after = await p.context("ses_a")\n'
            'h.out.later = await p.context("ses_a")\n'
            'await p.unload()\n'
        )
        restored = "Context restored after compaction\n- open task: ship 3.0"
        self.assertNotIn(restored, result["out"]["during"])
        self.assertNotIn(restored, result["out"]["running"])
        self.assertIn(restored, result["out"]["after"])
        self.assertNotIn(restored, result["out"]["later"])
        precompact = next(c["payload"] for c in result["calls"] if c["payload"] and c["payload"]["hook_event_name"] == "preCompact")
        compact_start = [c["payload"] for c in result["calls"] if c["payload"] and c["payload"].get("session_source") == "compact"]
        self.assertEqual(len(compact_start), 1)
        self.assertEqual(compact_start[0]["handoff_token"], precompact["handoff_token"])

    def test_late_start_compaction_is_bound_to_the_one_new_record(self) -> None:
        self.config["handoff_text"] = "- late start"
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT, [h.compactionRecord("cmp_0", "completed"), h.assistant("msg_a", "x")])\n'
            'const p = await h.load(PROJECT)\n'
            'await p.compaction("ses_a")\n'
            's.messages = [h.compactionRecord("cmp_1", "completed")]\n'
            'h.out.after = await p.context("ses_a")\n'
            'await p.unload()\n'
        )
        self.assertIn("Context restored after compaction\n- late start", result["out"]["after"])

    def test_failed_compaction_keeps_the_note_and_injects_nothing(self) -> None:
        self.config["handoff_text"] = "- keep me"
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT, [h.compactionRecord("cmp_1", "running")])\n'
            'const p = await h.load(PROJECT)\n'
            'await p.compaction("ses_a")\n'
            's.messages = [h.compactionRecord("cmp_1", "failed")]\n'
            'h.out.after = await p.context("ses_a")\n'
            'await h.emit("session.compaction.failed", "ses_a")\n'
            'await p.unload()\n'
        )
        self.assertFalse(any("Context restored" in text for text in result["out"]["after"]))
        self.assertFalse(any(c["payload"] and c["payload"].get("session_source") == "compact" for c in result["calls"]))
        state = json.loads((self.tmp / "fake-state.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(state["note"], "a failed compaction must not consume the note")

    def test_a_retained_old_note_is_not_restored_for_a_newer_attempt(self) -> None:
        self.config["handoff_text"] = "- from attempt one"
        self.config["handoff_writes"] = 1
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT, [h.compactionRecord("cmp_1", "running")])\n'
            'const p = await h.load(PROJECT)\n'
            'await p.compaction("ses_a")\n'
            's.messages = [h.compactionRecord("cmp_1", "failed"), h.compactionRecord("cmp_2", "running")]\n'
            'await p.compaction("ses_a")\n'
            's.messages = [h.compactionRecord("cmp_2", "completed")]\n'
            'h.out.after = await p.context("ses_a")\n'
            'await p.unload()\n'
        )
        self.assertFalse(any("Context restored" in text for text in result["out"]["after"]))

    def test_intent_survives_a_plugin_reload(self) -> None:
        self.config["handoff_text"] = "- after reload"
        result = self.scenario(
            'const s = h.session("ses_a", PROJECT, [h.compactionRecord("cmp_1", "running")])\n'
            'let p = await h.load(PROJECT)\n'
            'await p.compaction("ses_a")\n'
            'await p.unload()\n'
            's.messages = [h.compactionRecord("cmp_1", "completed")]\n'
            'p = await h.load(PROJECT)\n'
            'h.out.after = await p.context("ses_a")\n'
            'await p.unload()\n'
        )
        self.assertIn("Context restored after compaction\n- after reload", result["out"]["after"])


class TestMcpRegistration(OpenCodePluginTestBase):
    def test_the_plugin_registers_no_server_of_its_own(self) -> None:
        """The project's opencode.json carries the server; nothing is registered in memory."""
        (self.home / "bin" / "memory-mcp").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        result = self.scenario('const p = await h.load(PROJECT)\nawait p.unload()\n')
        self.assertEqual(result["mcp"], {})
        self.assertNotIn("mcp.transform", result["registered"])


if __name__ == "__main__":
    unittest.main()
