# silly-memory

A local, automatic memory system for Cursor, Claude Code, and OpenCode. It
captures your chats, edits, and shell activity, distills them into durable
markdown "memory bank" files, and injects a bounded context pack into every new
session, so the agent remembers your project's decisions, stakeholders,
conventions, and open action items.

All three tools share one store per project: something learned in an OpenCode
session shows up in the next Cursor or Claude Code session on the same project.

Works for coding **and** non-coding workspaces (product/eng/people management).
Everything runs locally; nothing is uploaded anywhere.

## Features

- **Three tools, one memory.** `./install.sh` finds Cursor, Claude Code, and
  OpenCode and wires each one it finds. Every tool's events go through one
  `memory hook` entry point, so capture, learning, and recall work the same
  everywhere. See [Supported tools](#supported-tools).
- **One home.** The engine and your data live in `~/.silly-memory` (override
  with `SILLY_MEMORY_HOME`). Each project is marked by `.silly-memory/memory-id`.
- **Memory tools for the agent.** Skills let every tool search memory, list
  action items, and store a fact by running the `memory` command, with no MCP
  server to approve. `./install.sh --mcp` adds a local MCP server with the same
  three operations (`memory_recall`, `memory_tasks`, `memory_add`). See
  [Memory tools](#memory-tools).
- **You control what is kept.** Text inside `<private>…</private>` is never
  stored. A prompt that starts with "remember that …" is stored as a fact at full
  confidence.
- **Continuity.** The context pack has a "Recent sessions" section, a
  compaction keeps a handoff note so Claude Code and OpenCode pick up where they
  left off, and `memrecall --all` searches every project.
- **A learning loop.** Corrections, reinforcement, and contradictions adjust
  confidence scores; recall is hybrid FTS5 + embeddings; old entries decay, and
  you approve what gets pruned.

## Supported tools

`./install.sh` wires every tool it finds: a tool counts as found when its config
folder exists (`~/.cursor`, `~/.claude`, `~/.config/opencode`) or its command
(`cursor`, `claude`, `opencode`) is on `PATH`. Pick them yourself with
`--tools`:

```bash
./install.sh --tools claude-code            # only Claude Code
./install.sh --tools cursor,opencode        # any subset of cursor, claude-code, opencode
```

The wired tools are recorded in `~/.silly-memory/config.json` (`"tools"`); the
doctor, the rule renderer, and the uninstaller read that list. Every file below
is recorded in `~/.silly-memory/.installed-artifacts.json`, so a reinstall
updates only what it owns and the uninstaller removes only what it installed.
Shared settings files (`~/.cursor/hooks.json`, `~/.claude/settings.json`) keep
every entry that is not silly-memory's. If one of them is not valid JSON, the
installer stops before changing anything.

The MCP server is off unless you install with `--mcp`, and it is never
registered globally (`~/.claude.json` and `~/.cursor/mcp.json` are not touched).
With `--mcp`, each project gets it in its own config at its first session; see
[Memory tools](#memory-tools).

After installing, check the wiring with `memdoctor`: its `tools` line reads
`<tool> wired` for each tool, and its `mcp` line reads `off` or, with `--mcp`,
counts the projects whose config carries the server.

### Cursor

What gets installed:

- `~/.cursor/hooks/memory-hook.sh`, plus its entries for all eight Cursor hook
  events in `~/.cursor/hooks.json`
- `~/.cursor/rules/memory-recall.mdc` (the deep-recall rule) and the memory
  skills in `~/.cursor/skills/`

Each project gets a generated rule, `.cursor/rules/_memory-context.mdc`, that
carries its context pack. Reload: restart Cursor. With `--mcp`, each project
also gets `.cursor/mcp.json` with the `silly-memory` server: Cursor asks once
per project to approve it (or run `cursor-agent mcp enable silly-memory` in the
project), and the server binds to the project through the folder Cursor reports
as its workspace root.

### Claude Code

What gets installed:

- `~/.claude/hooks/silly-memory-hook.sh`, plus handlers for `SessionStart`,
  `UserPromptSubmit`, `PostToolUse` (edits and `Bash`), `Stop`, `PreCompact`, and
  `SessionEnd` under the `hooks` key of `~/.claude/settings.json`. No other key
  of that file is touched.
- `~/.claude/rules/memory-recall.md` and the memory skills in `~/.claude/skills/`

Each project gets `.claude/rules/_memory-context.md` with its context pack, and
`SessionStart` adds a short note to the session. With `--mcp`, the project also
gets `.mcp.json` with the `silly-memory` server, approved in the project's
`.claude/settings.local.json` (`enabledMcpjsonServers`) so Claude Code does not
ask; the server binds to the project through `CLAUDE_PROJECT_DIR`. The hooks run
synchronously (10 s timeout, 30 s for `SessionEnd`), so the last turn of a
session is captured before Claude Code exits. Reload: start a new Claude Code
session.

### OpenCode

OpenCode **v2** is supported (built and tested against v2.0.18); OpenCode v1 is
not.

What gets installed:

- the plugin `~/.config/opencode/plugins/silly-memory.js` (one file, no
  dependencies; honors `XDG_CONFIG_HOME`)
- the memory skills in `~/.config/opencode/skills/`

The plugin sends OpenCode's session events to `memory hook --tool opencode` and
injects the project's context pack and the recall rule into the session context
(OpenCode has no project rule file). With `--mcp`, each project gets
`opencode.json` with the `silly-memory` server (an existing `opencode.json`
keeps its other settings; a project configured through `opencode.jsonc` is left
alone). Reload: OpenCode reloads plugins on its own; if it does not, run
`opencode service restart`. The plugin is a local file, so `opencode plugin
list` does not show it. To see it working, run a session with `--print-logs`:
the log shows `loading plugin` for `silly-memory.js` and, with `--mcp` from the
project's second session on, `mcp connected server=silly-memory tools=3`.

## Install

### Basic install

Install from a git clone or an extracted release tarball:

```bash
git clone <repo-url> silly-memory    # or: tar -xzf silly-memory.tar.gz
cd silly-memory
./install.sh
```

The installer prints each of its 11 steps with timings, then lists the tools it
wired and how to reload each one.

One-liner bootstrap on a fresh machine:

```bash
./bootstrap.sh                        # default offline; --from must be a local path
./bootstrap.sh --allow-network        # opt in to network clone
./bootstrap.sh --from <url-or-path> --branch <ref> --target <dir>
```

### Offline install (default)

The default installer is fully offline. `MEMORY_ALLOW_NETWORK=0` (the default)
blocks all outbound socket creation during install. The repo ships preseeded
SHA-pinned `all-MiniLM-L6-v2` weights (~91 MB) at
`memory/weights/all-MiniLM-L6-v2/`. Install copies them to
`~/.silly-memory/_embeddings/` and `verify_weights` checks every SHA-256 on
every backend load, so the embedding model is **never downloaded** over the
network.

Shipped weights remove the model *download*, not the embedding *libraries*.
Dense semantic recall additionally requires one of the optional backends to be
importable at runtime — `torch` + `sentence_transformers`, or `fastembed`. If
neither is installed, the embedding backend resolves to `noop` and recall runs
FTS5-only (still fully functional, just lexical rather than semantic). Note a
cosmetic quirk: a fresh offline install may **log `noop`** during backend
selection because that step runs before the weights are copied into the cache;
at runtime, with the weights cached and a backend lib present, recall uses
`sentence-transformers`/`fastembed`.

```bash
./install.sh                          # offline; copies shipped weights, no model download
MEMORY_SKIP_MODEL_DOWNLOAD=1 ./install.sh   # hermetic CI/test: skip the weight copy entirely
```

To refresh the shipped weights, see
`memory/weights/all-MiniLM-L6-v2/README.md`.

### Online install (download weights from Hugging Face)

If you'd rather refetch weights at install time, opt in explicitly:

```bash
MEMORY_ALLOW_NETWORK=1 ./install.sh
```

Only `MEMORY_ALLOW_NETWORK=1` unblocks the weight download path. Any other
value (including unset) preserves the offline default.

### Options

```bash
./install.sh --help
./install.sh --tools claude-code,opencode   # wire only these tools
./install.sh --mcp                          # also give each project the MCP server (off by default)
SILLY_MEMORY_HOME=/path/to/home ./install.sh  # install into another home
```

`install.sh` has no dry-run mode and refuses unknown options and unknown tool
names (exit 2, before anything is written). To preview an install, run it
against a throwaway home: `HOME=$(mktemp -d) ./install.sh`.
`upgrade.sh --dry-run` and `uninstall.sh --dry-run` do exist.

With a custom `SILLY_MEMORY_HOME`, `~/.zshrc` exports it for your shell. The hook shims find the engine through
`SILLY_MEMORY_HOME`, so an app you start outside that shell needs the variable
set too.

### Backends

Choose the recall backend at install time:

- `MEMORY_EMBEDDING_BACKEND=sentence-transformers` — default, highest quality.
- `MEMORY_EMBEDDING_BACKEND=fastembed` — lighter ONNX runtime fallback.
- `MEMORY_EMBEDDING_BACKEND=noop` — disable embeddings; FTS5-only recall.
- `./install.sh --no-torch` — skip the `sentence-transformers`/`torch` backend
  (use `fastembed` or `noop`).

Install-time env vars:

- `MEMORY_ALLOW_NETWORK=0` (default) — block all network access during install.
- `MEMORY_ALLOW_NETWORK=1` — allow Hugging Face weight download.
- `MEMORY_SKIP_MODEL_DOWNLOAD=1` — first-class supported flag: skip embedding
  weight download entirely. Embeddings fall back to the noop backend until
  `MEMORY_ALLOW_NETWORK=1` is set or weights are preseeded into
  `memory/weights/all-MiniLM-L6-v2/`. Use this on machines that will never
  have network access or that are happy with FTS5-only recall.

### Post-install

1. Reload each wired tool (see [Supported tools](#supported-tools)).
2. `source ~/.zshrc` (or open a new terminal).
3. Run `memhelp` to see the commands, and `memdoctor` to check the wiring.

### Reinstall / re-run safety

The installer is safe to re-run for updates: it refreshes the engine but never
deletes your memory stores and keeps your `config.json`. A recall rule or skill
it installed is updated only if you have not changed it. A rule or skill you
edited, or a file of the same name that was never silly-memory's, is left alone
and reported. Hook entries are merged; other entries in the same files stay.

## Memory tools

The agent has three memory operations: search memory, list action items, and
store a fact. Two skills, installed into every wired tool, give it all three
with no MCP server: `query-memory` and `add-memory` run the `memory` command.

| Operation | What the skill runs | MCP tool (with `--mcp`) |
|---|---|---|
| Search | `memory recall "<keywords>"` (this project plus global memory); add `--all` for every project | `memory_recall` (`scope`: `workspace` or `all`) |
| Action items | `memory tasks`, with `--status`, `--tag`, `--owner`, `--all` | `memory_tasks` |
| Store a fact | `memory add "<fact>"`, or `memory add -` to read it from stdin; `--scope auto\|workspace\|global` | `memory_add` |

The command and the tool do the same work; `memory add` and `memory_add` share
one code path (secret redaction, `<private>` removal, routing by category, the
duplicate check, score 1.0, and the index and rule refresh). Every command finds
the project from the folder it runs in, so a subfolder uses the repository's
store. The recall rule tells the agent to use the MCP tools when they are
connected and the commands otherwise. You can run the same commands yourself:
`memrecall`, `memtasks`, and `memadd`.

Skills are the default for two reasons. A skill costs only its short
description until the agent uses it, while an MCP server sends all its tool
schemas with every request. And some teams may not use an MCP server until it
is approved, while a skill is instructions plus a local command. A skill's
commands still go through the tool's normal command approval.

### The MCP server (`--mcp`)

```bash
./install.sh --mcp      # turn it on; reruns and upgrades keep the setting
./install.sh --no-mcp   # turn it off and remove it from every tracked project
```

The setting is `"mcp"` in `~/.silly-memory/config.json`. With it on, each tool
connects to a local MCP server, `~/.silly-memory/bin/memory-mcp`, over stdio (no
port, no network). It offers exactly three tools:

| Tool | Arguments | What it does |
|---|---|---|
| `memory_recall` | `query`, `limit` (default 10, up to 50), `scope`: `workspace` (default) or `all` | Searches memory. `workspace` covers this project plus global memory; `all` searches every project's store. |
| `memory_tasks` | `status`: `open` (default) or `all`, `tag`, `owner`, `all` (every project) | Lists action items. |
| `memory_add` | `text`, `scope`: `auto` (default), `workspace`, or `global` | Stores one fact at full confidence and refreshes the project's rules right away. |

Tool output is capped at 4,000 characters.

**Registration is per project; nothing is global.** At a project's first
session, the session-start hook adds the `silly-memory` server to that project's
own config for each wired tool:

| Tool | Project file | Approval |
|---|---|---|
| Claude Code | `.mcp.json` | pre-approved in `.claude/settings.local.json` (`enabledMcpjsonServers`) |
| Cursor | `.cursor/mcp.json` | Cursor asks once per project (or `cursor-agent mcp enable silly-memory`) |
| OpenCode | `opencode.json` | none needed |

The tools read these files when a session starts, so the memory tools are
available from a project's second session on. The entries are meant to be
committed: they hold no machine path. Each one runs
`sh -c 'h="$SILLY_MEMORY_HOME"; …; exec python3 "$h/bin/memory-mcp" --client <tool>'`,
which finds the memory home when the server starts (`SILLY_MEMORY_HOME`, else
`~/.silly-memory`), so a teammate without silly-memory
just sees a server that does not start. Other settings and servers in these
files stay. A server named `silly-memory` that does not run `memory-mcp` is left
alone, and so is a file that is not valid JSON. `.claude/settings.local.json` is
listed in the generated `.claude/.gitignore`, because approvals are personal.

The server works out the project the same way for every tool: Cursor reports its
workspace root, Claude Code sets `CLAUDE_PROJECT_DIR`, and OpenCode starts the
server in the folder it was launched from. From there it finds the repository
root (the nearest `.git`, else the nearest `.silly-memory/memory-id`), so
starting a tool in `repo/packages/api` still uses `repo`'s store. A linked Git
worktree keeps its own store.

To run the server by hand: `python3 ~/.silly-memory/bin/memory-mcp --workspace
<project>`.

## How memory is captured and used

- **Capture.** Hooks (Cursor, Claude Code) and the plugin (OpenCode) record
  prompts, replies, file edits, and shell commands as events in the project's
  store, with the tool that produced them. Session end and compaction process
  the session and refresh every wired tool's project rule, so the next session,
  in any tool, starts with the updated pack.
- **Private text.** Anything between `<private>` and `</private>` (case
  insensitive; an unclosed `<private>` runs to the end of the text) is removed
  before anything is stored or logged, ahead of the secret filters.
- **"remember that …".** A prompt that starts with `remember that` is stored as an
  explicit fact with full confidence (score 1.0, tag `explicit`) when the
  session's memory is next processed, even in a short session. The same fact is
  stored once, however often it is repeated. `remember that time …` or the phrase
  in the middle of a sentence is ignored. Use `memadd` (or the add-memory skill,
  or `memory_add` with `--mcp`) when the fact must be recallable immediately.
- **Recent sessions.** The context pack has a "Recent sessions" section: the five
  newest sessions with date, tool, duration, the first prompt, and edit and shell
  counts. The last 200 sessions per project are kept.
- **Compaction handoff.** Before Claude Code or OpenCode compacts a session, the
  work state, open tasks, and the last five prompts of that session are written
  to a handoff note. After the compaction, that same session gets the note back
  (at most 9,000 characters) and the note is deleted. A note older than 24 hours
  is never restored. Cursor does not use handoff notes.
- **Recall everywhere.** `memrecall foo` searches this project plus global
  memory; `memrecall --all foo` searches every project's store, with each hit
  labeled by project. `--all` always searches every store; it never stops early
  to save time.

## Upgrade

`upgrade.sh` upgrades the installation in place behind a verified, numbered
backup. If any step fails after the backup, it rolls back automatically: the
home and every tool setting the installer changed return to how they were.

### Basic upgrade

```bash
./upgrade.sh                                  # upgrade in place
```

Which home is upgraded: `SILLY_MEMORY_HOME` if set, otherwise `~/.silly-memory`.
For a custom home, run `SILLY_MEMORY_HOME=/path ./upgrade.sh`, and pass the same
variable to roll back.

The upgrade takes the lock, backs up the home and checks the copy's digest, runs
the installer over the home, runs `memdoctor`, and then writes the upgrade
marker.

### Dry-run

```bash
./upgrade.sh --dry-run                        # print the steps in order, write nothing
./upgrade.sh --from-version VERSION           # override installed-version detection
```

The dry run takes no lock and creates no home, backup, or journal.

### Rollback

```bash
./upgrade.sh --rollback --confirm             # restore the latest backup
./upgrade.sh --rollback --target N --confirm  # restore backup number N
```

Rollback restores the home from a copy of the backup (the backup itself stays
usable), moves the home it replaces aside to `<home>.discard-<time>`, and puts
back each tool file and setting the upgrade changed: hook shims, recall rules,
skills, the OpenCode plugin, the zsh block, and the hook entries. Tool entries
the upgrade added are removed. Settings that are not silly-memory's are never
touched. If you edited one of silly-memory's own entries after the upgrade,
rollback stops before changing anything and names the file. A backup without a
journal is never restored.

### Backups and retention

Backups sit next to the home they copy: `<home>.upgrade-backup-N-<time>/`, for
example `~/.silly-memory.upgrade-backup-1-<time>/`. The five newest are kept.
Each backup holds a `.upgrade-transaction/` folder with the upgrade's journal;
it is never copied back into a restored home. `memstatus` prints the most recent
backup path under the post-upgrade banner.

### Post-upgrade banner

After a successful upgrade, `memstatus` shows a one-time post-upgrade banner
summarizing the from/to versions and pointing at the latest backup directory.
The banner is suppressed automatically on the next `memstatus` invocation.

### Interrupted upgrades

Every upgrade or rollback holds the lock `~/.silly-memory-upgrade.lock` (a
second one waits up to `SILLY_MEMORY_UPGRADE_LOCK_TIMEOUT` seconds, default 30,
then exits 73). The lock is released by the system when the process dies, so a
killed or crashed upgrade never blocks the next one.

If an upgrade or rollback is interrupted (signal, power loss), the next run
finds its journal before doing anything else and finishes the rollback, even if
the home is absent at that moment. `./upgrade.sh` then starts a fresh upgrade;
`./upgrade.sh --rollback --confirm` stops after the rollback. A home that failed
an upgrade is kept as `<home>.failed-upgrade-<time>`.

## Move to a new machine / Backup

`memexport` bundles your **entire** memory home (`~/.silly-memory`) into a
single portable `.tgz`. It is both your **backup** and the way you **move memory
to a new machine**. Nothing leaves your disk unless you copy the bundle
yourself.

> **`upgrade.sh` vs `memexport` — different jobs.** `upgrade.sh` is a
> **same-machine, in-place** upgrade of the installed engine (see [Upgrade](#upgrade)).
> `memexport` + `install.sh --import` / `memimport` is the **cross-machine
> move + backup** path for your memory *data*. They are independent: upgrade in
> place with `upgrade.sh`; relocate or snapshot your data with `memexport`.

### 1. Back up / export (old machine)

```bash
memexport                                   # → ~/silly-memory-export-<date>.tgz
memexport ~/backups/silly-memory.tgz        # …or choose the path
```

The bundle carries `config.json`, `_global/`, every `<workspace-id>/` store, and
each project's `workspace_root → store-id` mapping — enough to restore or
relocate everything. Copy it to the new machine (USB, `scp`, etc.).

### 2. Restore on the new machine

Install the system with the bundle. Any existing store is snapshotted first,
automatically — nothing is silently overwritten:

```bash
./install.sh --import ~/silly-memory-export-<date>.tgz
```

`install.sh --import` restores the bundle and relinks each project in place,
writing its `.silly-memory/memory-id`. It supplies `--confirm-import YES-IMPORT`
internally (safe because the backup is mandatory), so it never blocks on a
prompt.

### 3. Projects whose path changed on the new machine

If a project now lives at a different absolute path (e.g. `~/work/app` →
`~/dev/app`), relink it explicitly so `memws` and recall find it. Pass the
required `--confirm-import YES-IMPORT` token yourself; `--map-workspace OLD=NEW`
is repeatable:

```bash
memimport ~/silly-memory-export-<date>.tgz \
  --confirm-import YES-IMPORT --relink \
  --map-workspace /old/path/app=/new/path/app
```

`--confirm-import YES-IMPORT` is **required** to write and is never injected for
you on this direct path (only the `install.sh --import` wrapper supplies it after
taking the mandatory backup). `memimport` is the zsh shorthand for
`python3 ~/.silly-memory/bin/memory import`.

### 4. Re-seed your private profile

Your local-only `profile.md` never travels in a shared repo. On the new machine,
fill it in and sync it into the recognized global bank files, then verify:

```bash
$EDITOR ~/.silly-memory/_global/memory-bank/profile.md
memprofile-sync
memstatus        # or memws — confirm the projects and stores are present
```

See [`docs/private-setup.md`](docs/private-setup.md) for the full private-info
checklist.

## Uninstall

`uninstall.sh` removes only install artifacts. User memory data is always
preserved.

### Dry-run

```bash
./uninstall.sh --dry-run                      # list what would be removed
```

The dry-run prints every path that would be deleted or edited, for every tool,
and exits without touching the filesystem.

### Execute

```bash
./uninstall.sh --confirm                      # actually remove install artifacts
```

`--confirm` is required; without it `uninstall.sh` stops after listing what it
would remove.

Before deleting anything, the uninstaller reads every shared settings file it
would edit. If one is not valid JSON (for example `~/.claude/settings.json`), it
names the file and exits non-zero without removing anything.

### What gets removed

From the memory home (`SILLY_MEMORY_HOME`, default `~/.silly-memory`):

- `bin/`, `lib/`, `tests/`, `hooks/`, `cursor-extras/`
- `memory.zsh`, `install-zsh.sh`, `README.md`, `VERSION`,
  `.installed-artifacts.json`, `.first-run-seen`, `.upgraded-from`
- `.install.lock`, `.upgrade.lock`

From each tool, only what silly-memory installed and you have not changed:

- Cursor: the hook shim, the recall rule, the skills, and the memory entries in
  `~/.cursor/hooks.json`
- Claude Code: the hook shim, the recall rule, the skills, and the memory
  handlers in `~/.claude/settings.json`
- OpenCode: the plugin and the skills
- Every tracked project: the `silly-memory` server in its `.mcp.json`,
  `.cursor/mcp.json`, and `opencode.json`, and Claude Code's approval of it in
  `.claude/settings.local.json`. A file left with nothing else in it is deleted.

### What is preserved

Every workspace directory, `_global/`, and `config.json` are preserved, and so
is `.silly-memory/memory-id` in every project. A rule or skill you edited stays,
as do other hooks, other MCP servers, and an MCP server named `silly-memory`
that does not run silly-memory. User-data preservation is a hard invariant
enforced by `memory/tests/test_uninstall_residue.py`.

### Optional flags

```bash
./uninstall.sh --confirm --remove-zsh-helper  # also strip the silly-memory block from ~/.zshrc
./uninstall.sh --confirm --remove-backups     # also remove <home>.upgrade-backup-*
./uninstall.sh --confirm --remove-cache       # also remove ~/.silly-memory/_embeddings/
```

These flags are additive and never affect user memory data.

### After uninstall

After uninstall, the `mem*` helper commands disappear from new shells (existing
shells keep the cached functions until restart). Workspace memory directories
remain readable for manual inspection or migration; re-running `./install.sh`
reattaches them without data loss.

## Quick start

```bash
memhelp              # detailed description of every command
memstatus            # dashboard of memory for the current workspace
memlearn-status      # learning loop metrics (corrections, reinforcements, contradictions)
memtasks             # open action items as a task list
memws                # list all workspaces that have memory
memrecall foo        # full-text search across this project's and global memory
memrecall --all foo  # the same across every project
memadd we deploy from main   # store a fact at full confidence right away
meminspect <id>      # score, topic, and recency info for one entry
memwhy               # explain why each entry in the context pack was included
memdelete <id>       # delete one entry (requires --confirm)
memprune-review      # interactive review of low-score decay candidates
memprofile-sync      # sync your private profile.md into the global bank files
memdoctor            # 10 health checks (exit 0 healthy / 1 warn / 2 error)
memtest              # run the regression suite (isolated)
```

## Troubleshooting

- **A tool does not seem to remember anything.** Run `memdoctor`. The `tools`
  line names each wired tool and when it last started a session; an `ERROR`
  there means a hook, handler, or plugin is missing, and rerunning `./install.sh`
  (or `./install.sh --tools <tool>`) repairs it.
- **The memory tools (MCP) are missing in a tool.** The server is off unless
  you installed with `--mcp`; without it the agent uses the add-memory and
  query-memory skills, and `memdoctor`'s `mcp` line reads `off`. With `--mcp`,
  the tools load from a project's second session: the first one writes the
  project's config. Check that the project's `.mcp.json`, `.cursor/mcp.json`, or
  `opencode.json` has the `silly-memory` server (`memdoctor`'s `mcp` line counts
  them). In Cursor, approve the server when asked, or run `cursor-agent mcp
  enable silly-memory`. In OpenCode, a `--print-logs` session logs `mcp connected
  server=silly-memory`.
- **A custom home is not used by a tool.** The hook shims read
  `SILLY_MEMORY_HOME`; start the tool from a shell that exports it.
- **The installer or uninstaller stops on a settings file.** It names the file
  that is not valid JSON; fix or move it and rerun. Nothing was changed.

## Requirements

- macOS/Linux with `python3` (stdlib only — no pip installs)
- `bash`, `rsync`
- zsh for the `mem*` helpers (bash users can `source ~/.silly-memory/memory.zsh` manually)
- any of Cursor, Claude Code, or OpenCode v2

## Security & Privacy

Memory data lives only under `~/.silly-memory/<workspace-id>/` and
`~/.silly-memory/_global/`. It is never transmitted. Install and upgrade set
the memory home to `0700` so other local users cannot read it; `memdoctor`
warns if that drifts. Text inside `<private>…</private>` never reaches the
store.

### Zero-network invariant

The system enforces a zero-network invariant: by default no socket creation is
allowed from any memory-system module. This is asserted by
`memory/tests/test_zero_network.py`, which monkey-patches `socket.socket` and
fails the build if any code path attempts to open a connection. The MCP server
speaks only over stdin/stdout; it opens no port.

### Default behavior

`MEMORY_ALLOW_NETWORK=0` is the default. Under this default every backend,
helper, and CLI path treats network access as a hard error. Hot-path modules
must not import `urllib.request`, `requests`, or `socket`; this is enforced by
`memory/tests/test_no_forbidden_imports.py`.

### Allowed network use (opt-in)

Set `MEMORY_ALLOW_NETWORK=1` only when you explicitly want network-dependent
operations to succeed (Hugging Face weight refresh, cursor-agent delegation).
The opt-in is process-scoped — it does not persist beyond the invoking shell
and never silently re-enables itself across upgrades.

### LLM backend gating

The `cursor-agent` LLM backend is gated by the privacy module, not by a
dedicated env var. Specifically,
`memory/lib/memory_system/backends/llm/cursor_agent_backend.py` calls
`privacy.network_allowed()` (which reads `MEMORY_ALLOW_NETWORK`) from both
`is_available()` and `_send_prompt()`. Whenever `network_allowed()` returns
False — i.e., when `MEMORY_ALLOW_NETWORK=0`, the default — both methods raise
`RuntimeError` and the backend refuses to launch the `cursor-agent` CLI.
There is no separate enablement flag; gating is unified under
`MEMORY_ALLOW_NETWORK`.

### CI enforcement

`memory/tests/test_no_forbidden_imports.py` enforces the import bans on every
commit. `openai`, `anthropic`, `transformers`, and `langchain` are forbidden
anywhere in the source tree. `urllib.request`, `requests`, and `socket` are
forbidden in hot-path modules. The combination guarantees that no future patch
can quietly introduce a network dependency.

### Weights

The repo ships preseeded SHA-pinned all-MiniLM-L6-v2 weights (~91MB) at
`memory/weights/all-MiniLM-L6-v2/`. Install copies them to
`~/.silly-memory/_embeddings/` and verifies SHA-256 on every load via
`weights_manifest.py`. The default offline install (`MEMORY_ALLOW_NETWORK=0`)
downloads no model. Dense semantic recall still needs an embedding library
(`torch` + `sentence_transformers`, or `fastembed`) importable at runtime;
without one the backend is `noop` and recall is FTS5-only.
`MEMORY_ALLOW_NETWORK=1` enables refresh-from-upstream when tampering is
detected. `MEMORY_SKIP_MODEL_DOWNLOAD=1` is a hermetic CI/test toggle for
environments where embedding isn't exercised. The shipped weights are
Apache-2.0 licensed; see `memory/weights/all-MiniLM-L6-v2/README.md`.

## Performance

Latency budgets are asserted by `memory/tests/test_perf_*.py`, which CI runs as
its own `perf` job.

### Methodology

Each measurement takes N = 30 samples after a warmup with fixed seeds, and
records p50, p95, p99, mean, and coefficient of variation (CoV) to the perf
results log:
`$MEMORY_PERF_RESULTS` if set, else
`<system tempdir>/silly-memory-perf/perf-results.jsonl`. p95 is the gating
percentile; p99 is informational. A few tests also bound CoV, noted below.

### Budgets

| Measurement | p95 budget | Test file | On overshoot |
|---|---|---|---|
| Hook, warm (in-process), per event — Cursor events and the Claude Code hooks that map to them | 50 ms; 80 ms `sessionStart`/`preCompact`; 100 ms `sessionEnd` | `test_perf_hook.py` | skip, records measured ms |
| Hook, cold (fresh `python3 memory hook`, and `hook --tool claude-code`) | same as warm | `test_perf_hook.py` | skip, records measured ms |
| `memstatus` warm / cold | 50 / 100 ms | `test_perf_cli.py` | skip |
| `memshow bank` warm / cold | 80 / 150 ms | `test_perf_cli.py` | skip |
| `mempaths` warm / cold | 40 / 80 ms | `test_perf_cli.py` | skip |
| `memrecall` warm / cold | 150 / 250 ms | `test_perf_cli.py` | skip |
| `memdoctor` warm / cold | 200 / 500 ms | `test_perf_cli.py` | skip |
| `memory --help` cold | 100 ms | `test_perf_cli.py` | skip |
| Event append | 50 ms per append, CoV < 20% | `test_perf_write.py` | fail |
| Observation write | 50 ms per cycle | `test_perf_write.py` | fail |
| Distill 100 observations | 1500 ms per batch | `test_perf_write.py` | fail |
| 4 concurrent writers | 100 ms per append, no lost events | `test_perf_write.py` | fail |
| Embedding, short / medium text | 100 / 200 ms, CoV < 20% | `test_perf_embedding.py` | fail; skipped without an embedding library |
| Search over 50k entries: hybrid / FTS5 / vector | 150 / 100 / 120 ms | `test_perf_search.py` | fail; skipped without numpy |
| Lock primitive: bash `mkdir`/`rmdir` x30 / Python `flock` x10 | 80 / 10 ms | `test_perf_locks.py` | fail |

Each Claude Code hook measurement first checks that its event reached the
store, so a hook that fails fast cannot pass as a fast one.

### Cold-start exception

Every cold measurement pays Python interpreter start-up: about 220 ms for
macOS system Python 3.9, and considerably more on a heavily loaded machine.
That alone exceeds several cold budgets, so the hook and CLI tests record
the measured latency and skip instead of failing. The write, lock, search, and
embedding budgets fail outright.

## License

silly-memory is licensed under the [Apache License 2.0](LICENSE).
Copyright 2026 Sillybit ([sillybit.io](https://sillybit.io)); see [NOTICE](NOTICE).

The shipped `all-MiniLM-L6-v2` model weights are the Sentence-Transformers
project's, redistributed unmodified under the Apache License 2.0
([`memory/weights/all-MiniLM-L6-v2/`](memory/weights/all-MiniLM-L6-v2/README.md)).
The agent skills in `.agents/skills/` and `agent/skills/` are Sillybit's
[silly-skills](https://github.com/Sillybit-io/silly-skills) and keep the license
stated at the end of each file (CC BY-ND 4.0).
