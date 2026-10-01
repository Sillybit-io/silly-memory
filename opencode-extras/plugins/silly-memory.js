// silly-memory for OpenCode v2: captures each session into the shared silly-memory
// store and injects the context pack before model calls.
//
// The default export is the plain `{ id, setup }` object that `Plugin.define` from
// `@opencode/plugin` returns (it is an identity function). Keeping it a literal means
// this file loads as a direct, dependency-free plugin from ~/.config/opencode/plugins/.
// Every handler fails open: the engine is a local CLI, and nothing here ever throws.
import { spawn } from "node:child_process"
import { createHash, randomUUID } from "node:crypto"
import { existsSync, mkdirSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, isAbsolute, join, resolve } from "node:path"

const CAPTURE_TIMEOUT_MS = 5_000
const LIFECYCLE_TIMEOUT_MS = 30_000
const UNLOAD_DRAIN_MS = 10_000
const BUSY_EXIT = 75
const MAX_REMEMBERED_IDS = 500
const INTENT_TTL_MS = 24 * 60 * 60 * 1000
const RECONNECT_MIN_MS = 1_000
const RECONNECT_MAX_MS = 30_000
const DIAGNOSTIC_INTERVAL_MS = 5 * 60 * 1000
const EXECUTION_END = new Set([
  "session.execution.succeeded",
  "session.execution.failed",
  "session.execution.interrupted",
])

function memoryHome(env) {
  return env.SILLY_MEMORY_HOME || join(homedir(), ".silly-memory")
}

function sessionKey(sessionID) {
  return createHash("sha256").update(`opencode\0${sessionID}`).digest("hex")
}

function atomicWriteJson(path, value) {
  mkdirSync(dirname(path), { recursive: true })
  const tmp = `${path}.${process.pid}.${randomUUID()}.tmp`
  writeFileSync(tmp, JSON.stringify(value) + "\n", { mode: 0o600 })
  renameSync(tmp, path)
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"))
  } catch {
    return undefined
  }
}

function lastJsonLine(text) {
  const lines = text.split("\n").map((line) => line.trim()).filter(Boolean)
  for (let i = lines.length - 1; i >= 0; i--) {
    try {
      return JSON.parse(lines[i])
    } catch {}
  }
  return undefined
}

function stripFrontmatter(text) {
  return text.replace(/^---\n[\s\S]*?\n---\n/, "").trim()
}

function assistantText(message) {
  const content = Array.isArray(message.content) ? message.content : []
  return content
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n")
    .trim()
}

function rememberId(list, id) {
  list.push(id)
  if (list.length > MAX_REMEMBERED_IDS) list.splice(0, list.length - MAX_REMEMBERED_IDS)
}

function createSillyMemory(ctx, env = process.env) {
  const home = memoryHome(env)
  const cli = env.MEMORY_BIN || join(home, "bin", "memory")
  const python = env.MEMORY_PY || "python3"
  const sessions = new Map()
  const fileCache = new Map()
  const seenToolFiles = new Set()
  const diagnostics = new Map()
  const registrations = []
  const controller = new AbortController()
  const loadedAt = Date.now()
  let unloading = false

  function report(key, detail) {
    const now = Date.now()
    if (now - (diagnostics.get(key) ?? 0) < DIAGNOSTIC_INTERVAL_MS) return
    diagnostics.set(key, now)
    const text = detail instanceof Error ? detail.message : String(detail ?? "")
    console.error(`[silly-memory] ${key}${text ? `: ${text.slice(0, 300)}` : ""}`)
  }

  function runCli(args, input, timeoutMs) {
    return new Promise((done) => {
      if (!existsSync(cli)) {
        report("memory CLI not found", cli)
        done({ code: null, stdout: "" })
        return
      }
      let child
      try {
        // Its own process group: `opencode run` signals its whole group as it exits, which
        // would kill the capture of the final reply while it is still being saved.
        child = spawn(python, [cli, ...args], { stdio: ["pipe", "pipe", "pipe"], env, detached: true })
      } catch (error) {
        report("could not start the memory CLI", error)
        done({ code: null, stdout: "" })
        return
      }
      let stdout = ""
      let stderr = ""
      const timer = setTimeout(() => {
        report(`memory ${args[0]} timed out`, `${timeoutMs} ms`)
        child.kill("SIGKILL")
      }, timeoutMs)
      child.stdout.on("data", (chunk) => {
        stdout += chunk
      })
      child.stderr.on("data", (chunk) => {
        if (stderr.length < 2_000) stderr += chunk
      })
      child.stdin.on("error", () => {})
      child.on("error", (error) => {
        clearTimeout(timer)
        report("memory CLI failed", error)
        done({ code: null, stdout })
      })
      child.on("close", (code) => {
        clearTimeout(timer)
        if (code !== 0 && code !== BUSY_EXIT) report(`memory ${args[0]} exited ${code}`, stderr)
        done({ code, stdout })
      })
      child.stdin.end(input === undefined ? "" : JSON.stringify(input))
    })
  }

  function state(sessionID) {
    let s = sessions.get(sessionID)
    if (!s) {
      s = {
        id: sessionID,
        queue: Promise.resolve(),
        hooked: false,
        directory: undefined,
        root: undefined,
        store: undefined,
        started: false,
        needsReconcile: true,
        pendingProcess: false,
        checkpoint: { since: 0, prompts: [], assistants: [] },
        intent: undefined,
      }
      sessions.set(sessionID, s)
    }
    return s
  }

  // Per-session serialization: every capture, process run, and restoration for one
  // session runs in order, so replies are persisted before the stop that ends a turn.
  function enqueue(s, work) {
    const run = s.queue.then(work).catch((error) => report("session task failed", error))
    s.queue = run
    return run
  }

  async function attribute(s) {
    if (s.directory) return s.directory
    try {
      const info = await ctx.session.get({ sessionID: s.id })
      const directory = info?.location?.directory
      if (typeof directory === "string" && directory) s.directory = directory
    } catch (error) {
      report("could not read session location", error)
    }
    return s.directory
  }

  async function owned(s) {
    if (s.hooked) return true
    const directory = await attribute(s)
    return directory !== undefined && directory === ctx.location?.directory
  }

  function checkpointPath(s) {
    return join(s.store, "opencode", `${sessionKey(s.id)}.json`)
  }

  function intentPath(s) {
    return join(s.store, "opencode", `${sessionKey(s.id)}.intent.json`)
  }

  function saveCheckpoint(s) {
    try {
      atomicWriteJson(checkpointPath(s), { session_id: s.id, ...s.checkpoint })
    } catch (error) {
      report("could not save the capture checkpoint", error)
    }
  }

  function saveIntent(s) {
    try {
      if (s.intent) atomicWriteJson(intentPath(s), s.intent)
      else if (existsSync(intentPath(s))) unlinkSync(intentPath(s))
    } catch (error) {
      report("could not save the compaction intent", error)
    }
  }

  function acknowledged(result) {
    const reply = lastJsonLine(result.stdout)
    const ack = reply?.memory_capture
    if (result.code !== 0 || !ack || ack.status !== "ok") return undefined
    return { reply, ack }
  }

  async function send(s, hook, fields, timeoutMs = CAPTURE_TIMEOUT_MS) {
    const payload = {
      hook_event_name: hook,
      tool: "opencode",
      workspace_roots: [s.directory],
      conversation_id: s.id,
      ...fields,
    }
    const done = acknowledged(await runCli(["hook", "--tool", "opencode"], payload, timeoutMs))
    if (!done) return undefined
    const { ack } = done
    if (typeof ack.workspace_root === "string" && ack.workspace_root) s.root = ack.workspace_root
    if (typeof ack.store_path === "string" && ack.store_path) s.store = ack.store_path
    return done
  }

  // The engine must acknowledge a sessionStart before this plugin captures, checkpoints,
  // or records a compaction intent; its acknowledgment names the resolved store.
  async function ensureStarted(s) {
    if (s.started) return true
    if (!(await attribute(s))) return false
    const done = await send(s, "sessionStart", { session_source: "startup" })
    if (!done || !s.store) return false
    s.started = true
    const saved = readJson(checkpointPath(s))
    // A session first seen now starts its capture at plugin load, so installing into a
    // long-running session does not replay its whole history inside a model call.
    s.checkpoint = {
      since: saved ? (Number.isFinite(saved.since) ? saved.since : 0) : loadedAt,
      prompts: Array.isArray(saved?.prompts) ? saved.prompts : [],
      assistants: Array.isArray(saved?.assistants) ? saved.assistants : [],
    }
    if (!saved) saveCheckpoint(s)
    const intent = readJson(intentPath(s))
    s.intent = intent && typeof intent.token === "string" ? intent : undefined
    return true
  }

  async function readContext(s) {
    try {
      const messages = await ctx.session.context({ sessionID: s.id })
      return Array.isArray(messages) ? messages : undefined
    } catch (error) {
      report("could not read session context", error)
      return undefined
    }
  }

  // Captures completed assistant replies not yet acknowledged, oldest first. Stops at the
  // first failure so a reply is never checkpointed ahead of an earlier, unsaved one.
  async function drainReplies(s, messages) {
    const list = messages ?? (await readContext(s))
    if (!list) return false
    for (const message of list) {
      if (message?.type !== "assistant" || !message.time?.completed) continue
      if (message.time.completed < s.checkpoint.since) continue
      if (typeof message.id !== "string" || s.checkpoint.assistants.includes(message.id)) continue
      const text = assistantText(message)
      if (!text) continue
      const done = await send(s, "afterAgentResponse", { text, message_id: message.id })
      if (!done) return false
      rememberId(s.checkpoint.assistants, message.id)
      saveCheckpoint(s)
    }
    return true
  }

  async function processMemory(s) {
    const result = await runCli(["process", "--workspace", s.root ?? s.directory], undefined, LIFECYCLE_TIMEOUT_MS)
    // Only a busy worker (75) is retried at the next callback; that check returns at once,
    // while retrying a slow or failing run would stall a model call. The next turn end
    // processes everything still queued.
    s.pendingProcess = result.code === BUSY_EXIT
    return result.code === 0
  }

  async function catchUp(s) {
    if (s.needsReconcile && (await drainReplies(s))) s.needsReconcile = false
    if (s.pendingProcess) await processMemory(s)
  }

  function compactionRecords(messages) {
    return (messages ?? []).filter((message) => message?.type === "compaction" && typeof message.id === "string")
  }

  // Restores the handoff note only for the compaction attempt this plugin recorded, and
  // only once OpenCode's own record of that attempt says it completed.
  async function restoreHandoff(s) {
    const intent = s.intent
    if (!intent) return undefined
    if (Date.now() - (intent.created ?? 0) > INTENT_TTL_MS) {
      s.intent = undefined
      saveIntent(s)
      return undefined
    }
    const records = compactionRecords(await readContext(s))
    if (!intent.compaction_id) {
      const baseline = new Set(intent.baseline ?? [])
      const fresh = records.filter((record) => !baseline.has(record.id))
      if (fresh.length !== 1) return undefined
      intent.compaction_id = fresh[0].id
      saveIntent(s)
    }
    const record = records.find((candidate) => candidate.id === intent.compaction_id)
    if (!record) return undefined
    if (record.status === "failed") {
      s.intent = undefined
      saveIntent(s)
      return undefined
    }
    if (record.status !== "completed") return undefined
    const done = await send(s, "sessionStart", { session_source: "compact", handoff_token: intent.token })
    if (!done) return undefined
    s.intent = undefined
    saveIntent(s)
    const restored = done.reply.additional_context
    return typeof restored === "string" && restored.trim() ? restored : undefined
  }

  function readCached(path) {
    try {
      const { mtimeMs, size } = statSync(path)
      const hit = fileCache.get(path)
      if (hit && hit.mtimeMs === mtimeMs && hit.size === size) return hit.text
      const text = readFileSync(path, "utf8")
      fileCache.set(path, { mtimeMs, size, text })
      return text
    } catch {
      return undefined
    }
  }

  function memoryParts(s) {
    const parts = []
    const pack = s.store ? readCached(join(s.store, "context-pack.md")) : undefined
    if (pack?.trim()) parts.push(pack.trim())
    const rule = readCached(join(home, "cursor-extras", "rules", "memory-recall.mdc"))
    if (rule) {
      const body = stripFrontmatter(rule)
      if (body) parts.push(body)
    }
    return parts
  }

  async function onPrompt(event) {
    const s = state(event.sessionID)
    s.hooked = true
    await enqueue(s, async () => {
      if (!(await ensureStarted(s))) return
      await catchUp(s)
      const text = event.prompt?.text
      const messageID = event.messageID
      if (typeof text !== "string" || !text.trim()) return
      if (messageID && s.checkpoint.prompts.includes(messageID)) return
      const done = await send(s, "beforeSubmitPrompt", { prompt: text, message_id: messageID })
      if (done && messageID) {
        rememberId(s.checkpoint.prompts, messageID)
        saveCheckpoint(s)
      }
    })
  }

  async function onContext(event) {
    const s = state(event.sessionID)
    s.hooked = true
    let restored
    await enqueue(s, async () => {
      if (!(await ensureStarted(s))) return
      await catchUp(s)
      restored = await restoreHandoff(s)
    })
    if (!s.started) return
    for (const text of memoryParts(s)) event.system.push({ type: "text", text })
    if (restored) event.system.push({ type: "text", text: restored })
  }

  async function onCompaction(event) {
    const s = state(event.sessionID)
    s.hooked = true
    await enqueue(s, async () => {
      if (!(await ensureStarted(s))) return
      // Compaction hides earlier replies from later context reads, so capture them now.
      const messages = await readContext(s)
      if (await drainReplies(s, messages)) s.needsReconcile = false
      const records = compactionRecords(messages)
      const running = records.find((record) => record.status === "running")
      s.intent = {
        token: randomUUID(),
        compaction_id: running?.id ?? null,
        baseline: running ? [] : records.map((record) => record.id),
        created: Date.now(),
      }
      saveIntent(s)
      await send(s, "preCompact", { handoff_token: s.intent.token }, LIFECYCLE_TIMEOUT_MS)
    })
    if (!s.started) return
    for (const text of memoryParts(s)) event.system.push({ type: "text", text })
  }

  function toolObservations(event, directory) {
    const input = event.input ?? {}
    if (event.tool === "shell") {
      return typeof input.command === "string" && input.command ? [{ hook: "afterShellExecution", command: input.command }] : []
    }
    let files = []
    if (event.tool === "edit" || event.tool === "write") files = [input.path]
    else if (event.tool === "patch") {
      const listed = event.result?.output?.files ?? event.result?.metadata?.files ?? []
      files = Array.isArray(listed) ? listed.map((entry) => entry?.file) : []
    }
    return files
      .filter((file) => typeof file === "string" && file)
      .map((file) => ({ hook: "afterFileEdit", file_path: isAbsolute(file) ? file : resolve(directory, file) }))
  }

  async function onToolAfter(event) {
    if (event.status !== "completed" || typeof event.sessionID !== "string") return
    const s = state(event.sessionID)
    s.hooked = true
    void enqueue(s, async () => {
      if (!(await ensureStarted(s))) return
      for (const { hook, ...fields } of toolObservations(event, s.directory)) {
        const key = `${event.id}\0${fields.file_path ?? fields.command}`
        if (seenToolFiles.has(key)) continue
        if (await send(s, hook, { ...fields, message_id: event.messageID })) {
          seenToolFiles.add(key)
          if (seenToolFiles.size > MAX_REMEMBERED_IDS * 4) seenToolFiles.delete(seenToolFiles.values().next().value)
        }
      }
    })
  }

  async function onExecutionEnd(s) {
    if (!(await ensureStarted(s))) return
    s.needsReconcile = !(await drainReplies(s))
    await send(s, "stop", {})
    await processMemory(s)
  }

  async function onDeleted(s) {
    if (!s.started || !s.directory) return
    await send(s, "sessionEnd", {}, LIFECYCLE_TIMEOUT_MS)
    sessions.delete(s.id)
  }

  function onEvent(event) {
    const type = event?.type
    const sessionID = event?.data?.sessionID
    if (typeof type !== "string" || typeof sessionID !== "string") return
    if (type === "session.moved") {
      const s = sessions.get(sessionID)
      if (s) void enqueue(s, async () => {
        s.directory = s.root = s.store = undefined
        s.started = false
      })
      return
    }
    const handler = EXECUTION_END.has(type) ? onExecutionEnd : type === "session.deleted" ? onDeleted : undefined
    if (!handler) return
    const s = state(sessionID)
    void enqueue(s, async () => {
      if (await owned(s)) await handler(s)
    })
  }

  function sleep(ms) {
    return new Promise((done) => {
      const timer = setTimeout(done, ms)
      controller.signal.addEventListener("abort", () => {
        clearTimeout(timer)
        done()
      }, { once: true })
    })
  }

  // The server stream is live-only and does not reconnect by itself. After any gap the
  // next callback for each session re-reads its context so a missed turn end is recovered.
  async function consumeEvents() {
    let delay = RECONNECT_MIN_MS
    while (!controller.signal.aborted) {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          delay = RECONNECT_MIN_MS
          onEvent(event)
        }
      } catch (error) {
        if (!controller.signal.aborted) report("event stream failed", error)
      }
      if (controller.signal.aborted) return
      for (const s of sessions.values()) s.needsReconcile = true
      await sleep(delay)
      delay = Math.min(delay * 2, RECONNECT_MAX_MS)
    }
  }

  function guard(name, handler) {
    return async (event) => {
      if (unloading) return
      try {
        await handler(event)
      } catch (error) {
        report(`${name} hook failed`, error)
      }
    }
  }

  async function setup() {
    const hooks = [
      ctx.session.hook("prompt", guard("prompt", onPrompt)),
      ctx.session.hook("context", guard("context", onContext)),
      ctx.session.hook("compaction", guard("compaction", onCompaction)),
      ctx.tool.hook("execute.after", guard("execute.after", onToolAfter)),
    ]
    for (const result of await Promise.allSettled(hooks)) {
      if (result.status === "fulfilled") registrations.push(result.value)
      else report("hook registration failed", result.reason)
    }
    void consumeEvents()
  }

  // Unload is not the end of any session: stop listening and let queued work finish.
  async function cleanup() {
    unloading = true
    controller.abort()
    await Promise.allSettled(registrations.map((registration) => registration?.dispose?.()))
    const pending = Promise.allSettled([...sessions.values()].map((s) => s.queue))
    let timer
    const deadline = new Promise((done) => {
      timer = setTimeout(done, UNLOAD_DRAIN_MS)
    })
    await Promise.race([pending, deadline])
    clearTimeout(timer)
  }

  return { setup, cleanup }
}

export default {
  id: "silly-memory",
  async setup(ctx) {
    const plugin = createSillyMemory(ctx)
    try {
      await plugin.setup()
    } catch (error) {
      console.error(`[silly-memory] setup failed: ${error instanceof Error ? error.message : error}`)
    }
    return () => plugin.cleanup()
  },
}
