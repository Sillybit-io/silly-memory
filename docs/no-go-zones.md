# No-Go Zones

This document describes the **no-go file set**: source files that must never be
modified during the modularization work described in the
`cursor-memory-modularization` plan. They were hardened during the prior
`cursor-memory-overhaul` plan and carry correctness, security, and integrity
guarantees that are fragile to even cosmetic edits.

> **Rule**: zero modifications, zero byte drift. T18 caught a violation and
> reverted it. Any future touch — including whitespace, type annotations, or
> import rewrites — must be pre-approved and bit-verified after the change.

See also: [`docs/architecture.md`](architecture.md) for the overall module map.

---

## Why these files are frozen

The `cursor-memory-overhaul` plan hardened five root-level modules and the
entire `backends/` subtree through:

- **Concurrency fixes** — file locks and atomic writes that survive concurrent
  hook invocations without data loss or corruption.
- **Model-loading correctness** — lazy ML imports and SHA-pinned weight
  verification that prevent silent model substitution or partial loads.
- **SQLite migration safety** — an FTS5 schema and triggers that are
  bit-pinned so existing user databases stay readable across upgrades.
- **Security and redaction** — a secret-pattern set and path deny-list that
  must not gain false positives or lose coverage.
- **Network isolation** — a single gate (`privacy.network_allowed()`) that all
  network-capable code must consult; no direct env-var reads allowed.

Documentation lives here, externally, because the bit-identity rule forbids
adding docstrings or comments to the files themselves.

---

## Root-level no-go files

### `memory/lib/memory_system/safety.py`

**What it does**

Provides two building blocks every other module uses when writing to disk:
`file_lock` stops two processes from writing to the same path at the same
time, and `atomic_write` writes a file so that any concurrent reader always
sees either the old content or the new content — never a half-written result.

**Why it's no-go**

The poll-with-timeout loop in `file_lock` was tuned so that callers on the
hook hot path (short timeout) stay fail-open and never hang the shell, while
background workers (no timeout) block safely. Changing the polling interval,
the lock flag, or the temp-file rename strategy could introduce races or
hangs under concurrent hook invocations.

**Public symbols**

| Symbol | Description |
|--------|-------------|
| `LockTimeout` | Exception raised when the lock cannot be acquired within the timeout |
| `file_lock(path, timeout)` | Context manager; acquires an exclusive advisory lock on `path` |
| `atomic_write(path, content, mode)` | Writes `content` to `path` safely via a temp file and rename |

**Common entry points**

- `file_lock` — called by `index.rebuild_index` before any database write.
- `atomic_write` — called by any module that persists memory data to disk.

**Invariants**

- `atomic_write` always writes to a `.{name}.*.tmp` sibling file first, then
  renames to the target via `os.replace`. The target path is never written
  directly.
- `file_lock(path, timeout=None)` blocks indefinitely; only background workers
  should use this form. Hook-path callers must always pass a positive timeout.
- On lock timeout, `LockTimeout` is raised — hook-path callers must catch it
  and proceed fail-open rather than aborting the shell session.
- The lock file is opened in append mode (`a+`), so `file_lock` works even
  when the target file doesn't exist yet.

---

### `memory/lib/memory_system/privacy.py`

**What it does**

A single authoritative check for whether the memory system may make network
calls. All network-capable code calls `network_allowed()` or
`assert_offline()` from this module rather than reading `MEMORY_ALLOW_NETWORK`
directly from the environment.

**Why it's no-go**

Centralizing the network gate was a deliberate hardening decision. Any module
that reads the env var directly instead of going through this file can drift
out of sync with the gate. The LLM backend is unified under this same gate —
there is no separate enablement flag for it.

**Public symbols**

| Symbol | Description |
|--------|-------------|
| `network_allowed()` | Returns `True` only when `MEMORY_ALLOW_NETWORK` is set to a truthy value |
| `assert_offline(reason)` | Raises `RuntimeError` immediately if network is not allowed |

**Common entry points**

- `network_allowed()` — called by `factory._log_privacy_mode_once` and
  `cursor_agent_backend.is_available`.
- `assert_offline(reason)` — called at the top of every `CursorAgentBackend`
  method before touching the network.

**Invariants**

- Default is offline: `network_allowed()` returns `False` when
  `MEMORY_ALLOW_NETWORK` is unset or anything other than `"1"`, `"true"`,
  `"yes"`, or `"on"`.
- Reads through `config.memory_allow_network()`, not raw `os.environ`, so any
  config-layer override is respected.
- `assert_offline` raises before any network I/O happens — it is not a
  post-hoc guard.

---

### `memory/lib/memory_system/redact.py`

**What it does**

Scrubs secrets from text and structured payloads before anything is written to
disk. It catches API keys, auth tokens, Bearer headers, private-key PEM
blocks, and file paths that point at credential files (`.env`, `id_rsa`,
`.pem`, and similar).

**Why it's no-go**

The pattern set and path deny-list were finalized during the overhaul security
review. Adding patterns carelessly can produce false positives that corrupt
stored memories (over-redacting valid content). Removing or weakening patterns
can leak secrets into the memory bank. `sanitize_payload` is the authoritative
entry point for all hook payloads and must not be bypassed.

**Public symbols**

| Symbol | Description |
|--------|-------------|
| `SECRET_PATTERNS` | List of compiled regexes matching API keys, tokens, PEM blocks, etc. |
| `PATH_DENY_FRAGMENTS` | Tuple of path substrings that mark a path as a credentials file |
| `should_redact_path(path)` | Returns `True` if a path looks like a secrets file |
| `redact_text(text)` | Applies all `SECRET_PATTERNS` to a string, replacing matches |
| `sanitize_payload(payload)` | Recursively sanitizes a dict, redacting text fields and secret paths |

**Common entry points**

- `sanitize_payload` — called by the event observer before writing any hook
  event to disk.
- `redact_text` — called directly when sanitizing a plain string such as an AI
  response.

**Invariants**

- `sanitize_payload` truncates `output`, `text`, `prompt`, and `command`
  fields to 8 000 characters **after** redaction, not before.
- Redacted values are replaced with `[REDACTED]` or `[REDACTED_PATH]`, never
  with an empty string, so field presence is preserved.
- Nested dicts and lists inside a payload are recursively sanitized.
- `redact_text` returns its input unchanged when the input is empty or falsy.

---

### `memory/lib/memory_system/scope.py`

**What it does**

Assembles the context pack that Cursor injects at the start of every session.
It reads global bank files, workspace bank files, the current work-state
snapshot, and recent observations, then trims the assembled text to fit within
the configured token cap before returning it.

**Why it's no-go**

The budget arithmetic — reserves for work-state, observations, and rule-file
header overhead — was carefully calibrated to keep the injected rule file
within Cursor's context limit. The bit-identity gate (T18) caught and reverted
a violation where three lines were edited for type-checker cosmetics with zero
runtime effect. Even whitespace or import changes are forbidden.

**Public symbols**

| Symbol | Description |
|--------|-------------|
| `GLOBAL_FILES` | Set of filenames always sourced from the global (cross-workspace) store |
| `read_bank_slice(store, filenames, max_chars)` | Reads and concatenates bank files up to a character budget |
| `merge_context_sources(workspace_root, token_cap)` | Assembles and returns the full context pack string |

**Common entry points**

- `merge_context_sources` — called by the context-pack render pipeline in
  `recall/context_pack.py` during `memory render`.

**Invariants**

- Total rendered output (including rule-file frontmatter) never exceeds
  `token_cap * 4` characters. The `rule_header_reserve` constant (320 chars)
  accounts for the frontmatter that `render_rule_file` prepends.
- When the budget is exhausted mid-section, the output is truncated and a `…`
  marker is appended — it never cuts silently.
- The deep-recall footer is always appended last and is never truncated,
  regardless of budget.
- `read_bank_slice` stops consuming files the moment the running total would
  exceed `max_chars`; partial file content is included only when at least 200
  characters of budget remain.

---

### `memory/lib/memory_system/index.py`

**What it does**

Manages a SQLite database with full-text search (FTS5 virtual table) that
powers `memrecall`. It creates the database schema on first use, re-indexes
all markdown files and observations on demand, and runs search queries that
return highlighted snippets.

**Why it's no-go**

The FTS5 schema — table definition, virtual table, and the three
INSERT/DELETE/UPDATE triggers that keep them in sync — is effectively a
migration contract. Altering it without a schema-version bump can silently
corrupt existing databases on user machines. The `_sanitize_fts` helper guards
against FTS5 query injection; weakening it creates a crash vector.

**Public symbols**

| Symbol | Description |
|--------|-------------|
| `SCHEMA` | SQL string defining the `documents` table, FTS5 virtual table, and sync triggers |
| `db_path(store)` | Returns the `Path` to `memory.sqlite` inside a store directory |
| `connect(store)` | Opens (and idempotently initializes) the SQLite database |
| `rebuild_index(store, scope)` | Re-indexes all markdown sections and observations for one store |
| `recall(store, query, limit)` | FTS5 search within one store; returns a list of snippet dicts |
| `recall_all(workspace_root, query, limit)` | Searches both global and workspace stores, merged and capped |

**Common entry points**

- `recall_all` — called by the `memory recall` CLI subcommand.
- `rebuild_index` — called by `memory reindex` and auto-triggered by `recall`
  when the database file is absent.

**Invariants**

- `rebuild_index` holds the store's file lock (via `safety.file_lock`) for the
  entire duration of the rebuild; no partial index is ever visible to readers.
- `_sanitize_fts` wraps every query token in FTS5 double-quoted string
  literals before passing to `MATCH`, preventing special characters
  (`-`, `+`, `(`, `:`) from being interpreted as FTS5 operators.
- `recall` catches `sqlite3.OperationalError`, triggers `rebuild_index`, then
  retries — callers always receive a result or a clean error, never a crash.
- `SCHEMA` uses `CREATE TABLE IF NOT EXISTS` and `CREATE VIRTUAL TABLE IF NOT
  EXISTS`, so `connect` is safe to call on an already-initialized database.

---

## `backends/` subtree

The entire `backends/` directory is no-go. It contains the protocol
definitions, factory selection logic, both embedding implementations, the LLM
backend, and the SHA-pinned weight verification system. The shared no-go
rationale: ML imports are deliberately lazy (never at module top-level), the
fallback chain is correctness-critical, and the weight-integrity check is a
security boundary.

---

### `backends/__init__.py` and `backends/factory.py`

**What they do**

`__init__.py` re-exports the four types and two factory functions that callers
need. `factory.py` contains `get_embedding_backend` and `get_llm_backend`,
which inspect the environment, try candidate backends in priority order, and
return whatever is available — falling back to noop if nothing works. It also
logs the privacy mode (online/offline + backend name) once per process.

**Why they're no-go**

The backend selection chain (`sentence-transformers` → `fastembed` → `noop`)
and the privacy-mode log are correctness-critical. An incorrect chain silently
downgrades semantic recall to noop with no user-visible signal.

**Public symbols** (re-exported via `backends/__init__.py`)

| Symbol | Description |
|--------|-------------|
| `EmbeddingBackend` | Protocol: the interface every embedding backend must satisfy |
| `LLMBackend` | Protocol: the interface every LLM backend must satisfy |
| `NoopEmbeddingBackend` | Safe fallback — always available, returns zero vectors |
| `NoopLLMBackend` | Safe fallback — always unavailable, raises `NotImplementedError` |
| `get_embedding_backend(config)` | Selects and returns the best available embedding backend |
| `get_llm_backend(config)` | Selects and returns the LLM backend (currently always noop) |

**Common entry points**

- `get_embedding_backend(config)` — called once at startup by the vector store.
- `get_llm_backend(config)` — called once at startup by the reflector.

**Invariants**

- `get_*_backend` always calls `is_available()` before returning; it never
  returns an unavailable backend.
- `MEMORY_EMBEDDING_BACKEND=noop` short-circuits all probing and returns
  `NoopEmbeddingBackend` immediately.
- The privacy-mode line is written to stderr exactly once per process
  regardless of how many times `get_*_backend` is called.

---

### `backends/embedding/` — Embedding backends

#### `base.py`

Defines the `EmbeddingBackend` Protocol. Any class with `name: str`,
`dim: int`, `encode(texts) -> list[list[float]]`, and
`is_available() -> bool` satisfies it.

#### `noop_backend.py`

`NoopEmbeddingBackend` — `is_available()` always returns `True` and `encode`
returns zero vectors of length `dim=384`. Used as the safe fallback when no
ML package is installed. Never raises.

#### `sentence_transformers_backend.py`

`SentenceTransformersBackend` — loads `all-MiniLM-L6-v2` from a local disk
path using `sentence-transformers` + `torch`. All ML imports happen inside
`__init__` (not at module top), so importing this file never pulls in torch.
Calls `weights_manifest.verify_or_warn` before loading the model.

Public symbols: `SentenceTransformersBackend` with `encode`, `is_available`,
`download_model`.

#### `fastembed_backend.py`

`FastembedBackend` — loads `BAAI/bge-small-en-v1.5` via the `fastembed` ONNX
runtime; lighter than the sentence-transformers backend. ML import is lazy.
Calls `weights_manifest.verify_or_warn` before loading.

Public symbols: `FastembedBackend` with `encode`, `is_available`,
`download_model`, `default_model_dir`.

---

#### `weights_manifest.py`

**What it does**

Holds SHA-256 pins for every file in the shipped model bundles. Before any ML
backend loads a model, it calls `verify_or_warn`, which checks file presence,
minimum size, and SHA-256 digest. On failure it either raises (offline) or
logs a warning and continues (online).

**Why it's no-go**

The SHA-256 pins and `min_size_bytes` floors are the integrity boundary
between the shipped weights and the runtime. The `_repo_root()` function
encodes the exact depth of this file in the tree (`parents[5]`) — moving the
file breaks the preseed path lookup without any error at import time. The
module must stay stdlib-only; any ML import here would break the loading order
both backends depend on.

**Public symbols**

| Symbol | Description |
|--------|-------------|
| `WEIGHTS_MANIFEST` | Dict mapping model name to file specs with SHA-256 pins and size floors |
| `verify_weights(model_name, cache_dir)` | Verifies all pinned files; raises `RuntimeError` on any failure |
| `verify_or_warn(model_name, cache_dir, logger)` | Calls `verify_weights`; raises offline, warns and continues online |
| `generate_manifest(model_name, dir_path)` | One-time utility: computes and prints SHA-256s for a model directory |
| `network_allowed()` | Local copy of the offline check (stdlib-only; must not import `privacy.py`) |

**Common entry points**

- `verify_or_warn` — called by `SentenceTransformersBackend._load()` and
  `FastembedBackend._load()` before the ML model loader is instantiated.

**Invariants**

- `weights_manifest.py` is stdlib-only. It must never import `torch`,
  `fastembed`, `sentence_transformers`, or `privacy`. CI enforces this via
  `test_no_forbidden_imports.py`.
- `verify_weights` always raises on failure when offline. `verify_or_warn`
  degrades to a log warning only when `MEMORY_ALLOW_NETWORK=1`.
- `_repo_root()` walks exactly `parents[5]` from `__file__` — hardcoded to
  the file's current position in the tree; moving the file breaks it.
- SHA entries with an empty `sha256` field (`""`) are treated as size-floor
  checks only. This is intentional for the fastembed model whose ONNX layout
  varies by release.

---

### `backends/llm/` — LLM backends

#### `base.py`

Defines the `LLMBackend` Protocol. Any class implementing `classify`,
`condense`, `extract_facts`, and `is_available` satisfies it.

| Method | What it does |
|--------|--------------|
| `classify(text, categories)` | Returns `(category: str, confidence: float)` |
| `condense(observations)` | Returns a condensed string from a list of observation strings |
| `extract_facts(prompt, response)` | Returns a list of fact dicts from a prompt/response pair |
| `is_available()` | Returns `True` if this backend is ready to use |

#### `noop_backend.py`

`NoopLLMBackend` — `is_available()` returns `False`; all other methods raise
`NotImplementedError`. This is the default backend; the LLM feature is
intentionally disabled until policy allows it.

#### `cursor_agent_backend.py`

**What it does**

`CursorAgentBackend` — the only real LLM backend. It shells out to the
`cursor-agent` CLI for text classification, observation condensing, and fact
extraction. Every call checks network permission before launching the
subprocess.

**Why it's no-go**

Every method calls `privacy.assert_offline()` before touching the network.
This gating is the sole mechanism keeping the LLM backend disabled by default.
Altering the call order or the condition would silently enable outbound
network calls.

**Public symbols**

| Symbol | Description |
|--------|-------------|
| `CursorAgentBackend` | LLM backend that delegates to the `cursor-agent` CLI subprocess |

Methods: `classify`, `condense`, `extract_facts`, `is_available`.

**Invariants**

- `privacy.assert_offline()` is the first call in every method that could
  touch the network — not a conditional lower in the method body.
- `is_available()` returns `False` when `MEMORY_ALLOW_NETWORK=0`, when
  `cursor-agent` is not on `PATH`, or when `cursor-agent about --format json`
  reports no signed-in user (`userEmail: null`) and no `CURSOR_API_KEY` is set.
  It probes with `about` because `cursor-agent status` blocks on a logged-out
  CLI or a locked keychain.
- `_send_prompt` runs `cursor-agent --print --trust --mode ask --output-format
  text` with the prompt on stdin. `ask` mode keeps the agent read-only; plain
  `--print` would grant it write and shell tools on text built from captured
  chats. (`agent send --prompt` was rejected by current CLI builds.)
- `_send_prompt` raises `RuntimeError` (not empty string) when the subprocess
  exits non-zero.
- The backend is not wired into production today: `get_llm_backend` returns
  noop and `maybe_active_question` has no caller outside its tests.

Edited once, with explicit approval, on the `release-readiness` branch to
replace the stale CLI invocation above.

---

## `weights/` subdirectory

`memory/weights/all-MiniLM-L6-v2/` is a no-go directory. It contains the
preseeded SHA-pinned model files (~91 MB) that ship with the repo and are
verified on every backend load via `WEIGHTS_MANIFEST`.

Treat every file under `memory/weights/` as read-only. To update the weights:

1. Replace the files under `memory/weights/all-MiniLM-L6-v2/`.
2. Re-run `generate_manifest` (see `weights_manifest.py:298`).
3. Update the SHA-256 pins in `WEIGHTS_MANIFEST`.
4. Verify: `MEMORY_ALLOW_NETWORK=0 python3 -m memory.lib.memory_system.backends.embedding.weights_manifest`.

---

## Quick reference

| File | No-go reason | Key entry point(s) |
|------|--------------|--------------------|
| `safety.py` | Lock/atomic-write race-condition hardening | `file_lock`, `atomic_write` |
| `privacy.py` | Unified network gate (overhaul hardening) | `network_allowed`, `assert_offline` |
| `redact.py` | Secret pattern set finalized in security review | `sanitize_payload`, `redact_text` |
| `scope.py` | Token-budget arithmetic is context-limit critical | `merge_context_sources` |
| `index.py` | SQLite FTS5 schema is bit-pinned migration contract | `recall_all`, `rebuild_index` |
| `backends/__init__.py` | Re-exports backend public surface | (import from here) |
| `backends/factory.py` | Backend selection chain is correctness-critical | `get_embedding_backend`, `get_llm_backend` |
| `backends/embedding/base.py` | Protocol definition | `EmbeddingBackend` |
| `backends/embedding/noop_backend.py` | Safe fallback; must never raise | `NoopEmbeddingBackend` |
| `backends/embedding/sentence_transformers_backend.py` | Lazy ML import + SHA-verify on load | `SentenceTransformersBackend` |
| `backends/embedding/fastembed_backend.py` | Lazy ML import + SHA-verify on load | `FastembedBackend` |
| `backends/embedding/weights_manifest.py` | SHA-256 security boundary; stdlib-only | `verify_or_warn`, `WEIGHTS_MANIFEST` |
| `backends/llm/base.py` | Protocol definition | `LLMBackend` |
| `backends/llm/noop_backend.py` | Default disabled state | `NoopLLMBackend` |
| `backends/llm/cursor_agent_backend.py` | Network gate must fire before any subprocess | `CursorAgentBackend` |
| `weights/` (directory) | SHA-pinned model files; see `weights_manifest.py` | (read-only) |
