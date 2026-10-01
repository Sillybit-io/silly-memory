# silly-memory

Local, all-workspaces memory for Cursor, Claude Code, and OpenCode. Combines
Sanity-style distillation, Mastra-style observational memory, and each tool's
hooks. All three tools share one store per project.

## Layout

```
~/.silly-memory/                # or SILLY_MEMORY_HOME
  config.json                   # settings, including the wired "tools" and "mcp" (off unless --mcp)
  .installed-artifacts.json     # files and settings entries the installer owns
  bin/memory                    # CLI
  bin/memory-mcp                # stdio MCP server (memory_recall, memory_tasks, memory_add)
  lib/memory_system/            # Python package
  cursor-extras/rules/          # recall rule the OpenCode plugin injects
  _global/memory-bank/          # preferences, people, hard rules
  <workspace-id>/               # per-workspace store
    events.jsonl
    sessions.jsonl              # recent sessions, per tool
    observations.md
    memory-bank/
    work-state.md
    context-pack.md
    handoffs/                   # compaction handoff notes
    memory.sqlite
<project>/.silly-memory/memory-id   # which store a project uses

<project>/.mcp.json, .cursor/mcp.json, opencode.json   # the MCP server, added at the first session with --mcp

~/.cursor/hooks.json, ~/.cursor/hooks/memory-hook.sh
~/.claude/settings.json, ~/.claude/hooks/silly-memory-hook.sh
~/.config/opencode/plugins/silly-memory.js
```

Only the tools you installed for are wired; see the repository README.

## Commands

A command that takes `--workspace` defaults to the current folder and uses its
project root (the nearest `.git`, else the nearest `.silly-memory/memory-id`), so
any subfolder of a project works.

```bash
python3 ~/.silly-memory/bin/memory render --workspace /path/to/project
python3 ~/.silly-memory/bin/memory recall "deploy checklist"
python3 ~/.silly-memory/bin/memory recall "deploy checklist" --all        # every project
python3 ~/.silly-memory/bin/memory add "we deploy only from main"         # one fact at full confidence (as memory_add)
python3 ~/.silly-memory/bin/memory observe --workspace /path/to/project
python3 ~/.silly-memory/bin/memory reflect --workspace /path/to/project
python3 ~/.silly-memory/bin/memory process --workspace /path/to/project
python3 ~/.silly-memory/bin/memory reindex --workspace /path/to/project
python3 ~/.silly-memory/bin/memory status --workspace /path/to/project
python3 ~/.silly-memory/bin/memory show staging --workspace /path/to/project
python3 ~/.silly-memory/bin/memory tasks --status open --tag release     # action items as a task list
python3 ~/.silly-memory/bin/memory workspaces                            # list tracked workspaces to pick from
python3 ~/.silly-memory/bin/memory paths --scope all                     # all memory markdown files with absolute paths
python3 ~/.silly-memory/bin/memory doctor                                # health checks, per wired tool
```

## Tests

Run the regression suite after any change to the memory code. It is stdlib-only
and runs against a throwaway `SILLY_MEMORY_HOME`, so it never touches your real
store:

```bash
python3 ~/.silly-memory/bin/memory selftest
```

Each test pins a bug we already fixed (unbounded staging growth, FTS recall
crashes, observer dedup/resume, context-pack token cap, no subprocess spawning,
fail-open locks). Add a new test in `tests/test_memory.py` whenever you fix a new
bug so it can't regress.

**Before editing any memory code, read `tests/README.md`** — it documents the
isolation mechanism (`SILLY_MEMORY_HOME`), test conventions, known gotchas, and
the full bug-history table.

## Sharing (code only, never your data)

To give the system to someone else, share the repo checkout. There is no
packaging script — install runs directly from a checkout:

```bash
git clone <repo-url> silly-memory
cd silly-memory
./install.sh
```

`install.sh` is safe to re-run for updates — it never deletes existing stores,
keeps `config.json`, and keeps a recall rule or skill the recipient changed. It
merges its hook entries into the recipient's settings (preserving their
other entries).

No personal memory data is included in a repo checkout. All user data lives
under `~/.silly-memory/<workspace-id>/` and `~/.silly-memory/_global/` on the
recipient's own machine, never in the repo.

## Hooks (automatic)

Cursor and Claude Code hooks, and the OpenCode plugin, capture prompts,
responses, file edits, shell output, compaction, and session boundaries through
`memory hook --tool <tool>`. Session end and compaction refresh each project's
generated rule (`.cursor/rules/_memory-context.mdc`,
`.claude/rules/_memory-context.md`); the OpenCode plugin injects the stored pack
itself.

## Privacy

All data is local and private. Secrets are redacted at write time, and text in
`<private>…</private>` is never stored. External system writes (Jira,
Confluence, GitLab) remain always-ask.
