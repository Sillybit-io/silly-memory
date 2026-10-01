# Changelog

## 1.0.0

First public release. silly-memory is a local memory system for Cursor, Claude
Code, and OpenCode v2 that keeps one store per project, shared by all three.
Licensed under the Apache License 2.0; copyright Sillybit (sillybit.io).

### Tools

- Cursor: a hook shim with entries for all eight hook events in
  `~/.cursor/hooks.json`, a recall rule, skills, and a generated
  `.cursor/rules/_memory-context.mdc` per project.
- Claude Code: a hook shim and six hook handlers in `~/.claude/settings.json`,
  a recall rule, skills, and a generated `.claude/rules/_memory-context.md` per
  project.
- OpenCode v2: a single dependency-free plugin that captures session events and
  injects the context pack and the recall rule. OpenCode v1 is not supported.
- One `memory hook --tool <cursor|claude-code|opencode>` entry point turns every
  tool's events into the same events, each recorded with its tool.
- Memory tools without MCP: the `query-memory` and `add-memory` skills run
  `memory recall`, `memory tasks`, and the new `memory add` (also `memadd`),
  which stores a fact exactly as `memory_add` does. Every CLI command uses the
  project root of the folder it runs in.
- An optional stdio MCP server (`bin/memory-mcp`) with three tools:
  `memory_recall`, `memory_tasks`, and `memory_add`. It is off by default;
  `./install.sh --mcp` turns it on and `--no-mcp` off again, and reruns and
  upgrades keep the setting. It is registered per project, never globally: a
  project's first session adds it to `.mcp.json` (pre-approved in
  `.claude/settings.local.json`), `.cursor/mcp.json`, and `opencode.json`, with
  no machine path, so the files can be committed.

### Memory

- The home is `~/.silly-memory` (override with `SILLY_MEMORY_HOME`); each
  project is marked by `.silly-memory/memory-id`.
- Self-teaching learning loop: correction detection, reinforcement, and
  contradiction flagging.
- Hybrid recall: FTS5 plus dense embeddings (`sentence-transformers` or
  `fastembed`), falling back to FTS5-only when neither library is installed.
  Shipped SHA-pinned `all-MiniLM-L6-v2` weights, so the default install
  downloads nothing.
- Per-entry confidence scores, decay, and user-approved pruning
  (`memprune-review`). Nothing is deleted silently.
- A topic-aware, score-weighted context pack with a "Recent sessions" section,
  and compaction handoff notes for Claude Code and OpenCode.
- `<private>…</private>` spans are never stored; a prompt starting with
  "remember that …" is stored as an explicit fact at full confidence.
- `memrecall --all` searches every project's store.
- `memexport` / `memimport` and `install.sh --import`: portable backup and
  cross-machine move, with a mandatory snapshot before import, `--relink`, and
  `--map-workspace OLD=NEW`.
- A private `profile.md`, seeded on install and synced into the global bank
  files by `memprofile-sync`.

### Install, upgrade, and uninstall

- `install.sh` detects the tools (or takes `--tools LIST`), records what it owns
  in `.installed-artifacts.json`, updates a rule or skill only if you have not
  changed it, and changes only its own entries in shared settings. Malformed
  settings stop it before any change. The default install is fully offline.
- `upgrade.sh` upgrades the home in place behind a verified, numbered backup,
  journals every tool setting it changes, holds a crash-safe lock, and recovers
  an interrupted upgrade on the next run. `--rollback --confirm` restores a copy
  of the backup and the previous tool wiring.
- `uninstall.sh` removes each tool's artifacts and its settings entries, keeps
  anything you changed and all memory data, and removes nothing if a settings
  file is malformed.
- `memdoctor` runs 10 health checks, including each wired tool and the MCP
  setting (with `--mcp`, the projects that carry the server).

### Tooling

- Release tooling: `scripts/prepublish-check.sh`, `scripts/secret_scan.py`, and
  `scripts/publish-clean.sh`.
- `scripts/e2e_real_tools.py` checks install, capture, recall, cross-tool memory,
  the memory skills (or the MCP server with `--mcp`), and uninstall with the
  real Cursor, Claude Code, and OpenCode CLIs in a throwaway home.
