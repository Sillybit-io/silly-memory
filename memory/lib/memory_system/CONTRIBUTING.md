# Contributing to silly-memory

Welcome. This guide is written for someone who just cloned the repo and wants to
make a change without knowing Python deeply. It takes about 15 minutes to read.

Before you start: [`docs/architecture.md`](../../../docs/architecture.md) has the
full module map and sequence diagrams. This document is the "how do I change X"
companion to that map.

---

## 1. Where are things?

The package lives at `memory/lib/memory_system/`. Every subdirectory is a
self-contained subpackage with its own `__init__.py`.

```
memory/lib/memory_system/
├── index.py          ← the single write entry-point: observe → enqueue
├── safety.py         ← file locking and atomic writes (a no-go file)
├── paths.py          ← where files live on disk
├── privacy.py        ← network gate and PII scrubbing coordination
├── redact.py         ← low-level secret-scrubbing primitives
├── scope.py          ← which repo/workspace am I in?
├── preflight.py      ← startup sanity checks before anything else runs
│
├── adapters/         ← turn raw Cursor events into a standard shape
├── backends/         ← swappable ML backends (embedding, LLM)
├── cli/              ← user-facing CLI sub-commands (memory delete, inspect…)
├── eval/             ← offline recall-quality baseline
├── events/           ← async event pipeline (observe → queue → worker drain)
├── learning/         ← continuous learning signals (corrections, topics…)
├── lifecycle/        ← long-running store health (backup, export/import, decay)
├── recall/           ← hybrid BM25 + vector search + context packet rendering
├── reflection/       ← LLM-based enrichment of raw observations
├── status/           ← diagnostics and health banners shown to the user
├── storage/          ← durable persistence (FAISS vector store + JSONL archive)
└── system/           ← cross-cutting utilities (config, version, normalize)
```

**Plain-English tour of each subpackage:**

- **`events/`** — Every Cursor hook (keystroke, file save, chat) calls
  `events.events.record_event`, which writes a line to `events.jsonl`. The
  `worker.py` drains that queue in the background so the hot path never blocks.

- **`recall/`** — When you ask "what do I know about X?", `recall_hybrid.py`
  combines full-text search (BM25) with dense vector similarity (FAISS) and
  merges them into a ranked list. `context_pack_v2.py` renders that list into
  the snippet injected into your Cursor context.

- **`storage/`** — Two durable stores: `observations_archive.py` (an
  append-only JSONL file) and `vector_store.py` (a FAISS index with a metadata
  sidecar). Both are covered by the no-go-zones schema freeze — see section 4.

- **`learning/`** — Classifies observations, detects contradictions and user
  corrections, scores reinforcement signals, and generates clarifying questions.
  All files here are internal; none are part of the public API.

- **`lifecycle/`** — Backup/restore, export/import bundles, compaction, and
  time-based relevance decay. Add a new lifecycle operation here; wire it
  into the worker post-drain or as a CLI sub-command.

- **`status/`** — `status/main.py` is the entry-point for health diagnostics.
  It caches results in `doctor_cache.py` (TTL-based) and emits human-readable
  banners via `banner.py`. `markers.py` holds the `HealthMarker` dataclasses.

- **`adapters/`** — Two concrete adapters: `coding.py` (git repos) and
  `management.py` (non-git workspaces). Both implement the `WorkStateAdapter`
  ABC from `base.py`. `pick_adapter()` in `base.py` chooses between them.

- **`system/`** — `config.py` merges env vars and `config.json` at startup.
  `version.py` is the single source of truth for the package version.
  `normalize.py` has text-cleaning helpers used across the package.

- **`backends/`** — ML backends are behind `Protocol` interfaces so you can
  swap them without touching callers. All ML imports inside backends are lazy
  (never at the top of the file). The CI import test enforces this.

---

## 2. Common change recipes

### Add a new CLI sub-command

**Subpackage:** `cli/`

1. Create `memory/lib/memory_system/cli/cli_mycommand.py`. Model it on
   `cli/cli_inspect.py`. Export one function, e.g. `run(args)`.
2. Register it in the CLI entry-point (the `memory` binary at `memory/bin/memory`
   — find the `dispatch` dict or `argparse` subparsers block and add your entry).
3. If your command needs tests, add `memory/tests/test_cli_mycommand.py` and
   follow the isolation pattern in `memory/tests/README.md` (set
   `SILLY_MEMORY_HOME` to a temp dir so you never touch the real store).
4. Run: `python3 -m unittest memory.tests.test_cli_mycommand -v`
5. If the command gets a `mem*` zsh helper in `memory/memory.zsh`, list it in
   `memhelp` and `_mem_complete`; `test_zsh_helpers.py` fails otherwise.

### Change how recall ranks results

**Subpackage:** `recall/`

The fusion logic lives in `recall/recall_hybrid.py`, in `hybrid_recall()`. It
min-max normalizes the FTS5, dense-vector, and sidecar-score signals (`_minmax`)
and combines them with weights from `_load_weights` (defaults:
`DEFAULT_WEIGHTS`). When the embedding backend is unavailable it falls back to
FTS5-only. To adjust ranking:

1. Open `recall/recall_hybrid.py` and read the inline `# Why:` comments — they
   explain the weighting choices.
2. Edit the scoring or fusion step inside `hybrid_recall()`.
3. Run the recall tests to check you haven't regressed ranking:
   ```bash
   python3 -m unittest discover -s memory/tests -k recall -v
   ```
4. If you're adding a new strategy rather than tweaking weights, wire it into
   the `search()` fusion step and add a fixture to `eval/fixtures/`.

### Add a new event type

**Subpackage:** `events/` + `adapters/`

1. In `events/events.py`, add your new hook name as a string constant (e.g.
   `HOOK_FILE_RENAME = "fileRename"`). The event record format is a plain dict
   written as a JSON line — no class changes needed for new hook names.
2. If the new event needs special payload normalization, add a method to
   `adapters/coding.py` or `adapters/management.py`. Both implement the
   `WorkStateAdapter` ABC, so add the logic in the right concrete class.
3. In `events/observer.py`, handle the new hook name in the dispatch block.
4. Add a test in `memory/tests/` that calls `record_event` with your new hook
   and asserts the JSONL line written to the temp store contains the right keys.

---

## 3. Imports

Import every module by its full path, for example
`from memory_system.system.config import memory_home` or
`from memory_system.events.worker import process_queue`. There are no aliases
for shorter names, and importing `memory_system` itself has no side effects. If
you move a module, update every import of it (engine, `memory/bin/*`, tests).

---

## 4. No-go zones

Full details are in [`docs/no-go-zones.md`](../../../docs/no-go-zones.md).

**Summary — three reasons things get frozen:**

- **Safety and locking.** `safety.py` provides the file-locking and atomic-write
  primitives every writer in the package uses. A subtle change there can cause
  data corruption across concurrent hook invocations. It's frozen so the
  correctness proofs stay valid.

- **On-disk schema.** The observations JSONL format and the FAISS index layout
  are read by every version of the storage layer. Changing them without a
  migration breaks existing user stores silently, so a format change needs a
  migration step that `upgrade.sh` runs. There is none today; add one together
  with the change.

- **Public API surface.** `index.observe`, `recall_hybrid.search`, and
  `status.main.status` are called by external tools. Their signatures are frozen.
  Additive changes (new optional kwargs) are fine; removing or renaming
  parameters is not.

If you think a frozen file genuinely needs to change, open a discussion first.
The rule is: zero byte drift without a migration plan and explicit approval.

---

## 5. Testing your change

### Run the full suite

The suite is stdlib `unittest` only (no pytest). From the repo root:

```bash
SILLY_MEMORY_HOME=$(mktemp -d) python3 -m unittest discover -s memory/tests -p "test_*.py" -v
```

The `SILLY_MEMORY_HOME` env var redirects all file I/O to a throwaway temp
directory so tests never touch your real memory store (each test also sets its
own in `setUp`).

### Run one file

```bash
SILLY_MEMORY_HOME=$(mktemp -d) python3 -m unittest memory.tests.test_memory -v
```

### Run tests matching a keyword

```bash
SILLY_MEMORY_HOME=$(mktemp -d) python3 -m unittest discover -s memory/tests -k recall -v
SILLY_MEMORY_HOME=$(mktemp -d) python3 -m unittest discover -s memory/tests -k backup -v
```

### Type check

```bash
pyright
```

`pyrightconfig.json` covers `memory/lib`. The accepted baseline is 4 errors,
all in `scope.py`, which is frozen by [`docs/no-go-zones.md`](../../../docs/no-go-zones.md);
anything beyond those four is a regression.

### What each test file covers

| File | Covers |
|------|--------|
| `test_memory.py` | End-to-end write → observe → recall round-trips |
| `test_observations_archive.py` | Storage layer: append, fetch, rotate |
| `test_cli_delete.py` | `memory delete` command |
| `test_cli_inspect.py` | `memory inspect` command |
| `test_cli_learn_status.py` | `memory learn-status` command |
| `test_no_forbidden_imports.py` | Enforces lazy ML imports; bans `openai`, `anthropic`, `socket` in hot paths |
| `test_zero_network.py` | Monkey-patches `socket.socket`; fails if any code path opens a connection |
| `test_backup.py` | Lifecycle: snapshot and restore |
| `test_perf_*.py` | Performance budgets (p50/p95/p99 latencies); skip on cold macOS Python 3.9.6 |
| `test_context_pack_v2.py` | Recall context packet rendering |
| `test_embedding_backends.py` | Embedding backend selection and fallback |

### Interpreting failures

- **`ModuleNotFoundError`** — usually an import of a module that moved. Import
  it by its new full path (section 3).
- **`LockTimeout`** — a test left a stale lock file in the temp dir. The temp
  dir is per-run (`mktemp -d`) so this shouldn't happen; if it does, delete
  that run's temp directory and retry.
- **`AssertionError` in `test_no_forbidden_imports`** — you imported a banned
  module at the top of a hot-path file. Move the import inside the function that
  needs it (lazy import).
- **Failing perf test** — your change added latency. Check whether you
  introduced a top-level import or a synchronous network call in a hot path.

---

## 6. Python idioms you'll encounter

This is not a Python tutorial — the [official docs](https://docs.python.org/3/)
cover everything here in depth. These are just the patterns that appear most
often in this codebase so you can recognize them quickly.

### `@contextmanager` — context managers

```python
from contextlib import contextmanager

@contextmanager
def file_lock(path, timeout=None):
    # setup
    lock_file = path.open("a+")
    try:
        acquire(lock_file)
        yield           # ← code inside the `with` block runs here
    finally:
        release(lock_file)   # always runs, even if an exception is raised
```

You'll see this in `safety.py` (`file_lock`) and in test helpers. The `with`
statement calls `__enter__` on entry and `__exit__` on exit, guaranteeing
cleanup. `@contextmanager` lets you write that as a generator function instead of
a class.

### `@abstractmethod` + `ABC` — abstract base classes

```python
from abc import ABC, abstractmethod

class WorkStateAdapter(ABC):
    @abstractmethod
    def snapshot(self, workspace_root):
        ...   # no body needed — subclasses MUST override this
```

`ABC` means "this class can't be instantiated directly." `@abstractmethod` means
"any concrete subclass must provide this method." If you forget to implement it,
Python raises `TypeError` at construction time, not silently at first call. You
see this in `adapters/base.py`.

### `@dataclass` — dataclasses

```python
from dataclasses import dataclass

@dataclass
class HealthMarker:
    name: str
    ok: bool
    message: str = ""
```

`@dataclass` auto-generates `__init__`, `__repr__`, and `__eq__` from the field
annotations. You get a plain struct with type hints for free. Used in
`status/markers.py` for health check results.

### `Protocol` — structural typing (duck typing with types)

```python
from typing import Protocol

class EmbeddingBackend(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...
```

`Protocol` means "anything with an `embed` method that matches this signature
counts as an `EmbeddingBackend`" — no inheritance required. It's how
`backends/factory.py` can swap embedding implementations without a class
hierarchy. Unlike `ABC`, `Protocol` is checked by type-checkers (Pyright) not
at runtime.

### `from __future__ import annotations` — lazy annotations

You'll see this at the top of almost every file. It makes all type annotations
strings evaluated lazily, which avoids circular-import errors when two modules
annotate types from each other. It has no runtime effect on Python 3.9+; it's
purely a type-checker hint.

### Walrus operator `:=` — assign and test in one step

```python
if last_id := meta.get("last_event_id"):
    process(last_id)
```

This assigns `meta.get("last_event_id")` to `last_id` and tests it in one
expression. Equivalent to `last_id = meta.get("last_event_id"); if last_id:`.
You'll see it in recall and event-processing loops where reading and checking a
value should be one operation.

### `# Why:` comments — the codebase convention

T23 added `# Why:` comments throughout the codebase. These explain the *reason*
for a non-obvious choice, not what the code does (the code already says that).
When you add code that has a hidden constraint, a workaround, or a tricky
invariant, add a `# Why:` line. Skip it for code that's self-explanatory.

```python
# Why: capture whatever the doctor command would print so we can parse it as JSON without leaking text to the user's terminal.
with contextlib.redirect_stdout(buf):
    _ = doctor.run_doctor(json_output=True)
```

Module-level docstrings (added in T22) follow the same spirit: the first
paragraph says what the module does and why it exists; the `Read this first`
block gives new contributors the fastest path to understanding the file. If a
file has a docstring, read it before diving into the code.
