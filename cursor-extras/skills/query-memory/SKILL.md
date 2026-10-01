---
name: query-memory
description: Query and inspect local silly-memory via its CLI (the same as the MCP tools memory_recall and memory_tasks) — list action items / tasks / to-dos, show memory status, search (recall) durable facts in this project or every project, list tracked workspaces, and locate memory markdown files. Use when the user asks about their open tasks, action items, to-dos, what's on their plate, memory status, what's stored in memory, which workspaces have memory, or to find/search/recall a stored fact, decision, person, or convention.
---

# Query memory

Read-only access to silly-memory, without needing MCP: `recall` does what the
`memory_recall` tool does and `tasks` what `memory_tasks` does. The injected
context pack is token-capped and often omits action items and older facts, so
for anything that must be complete or authoritative, run the CLI instead of
relying on the pack.

CLI entry point (works from anywhere in a project; defaults to the current
directory's project):

```bash
python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory" <command> [--workspace .]
```

## Pick the command by intent

| User asks about… | Run |
|---|---|
| Open tasks / action items / to-dos / "what's on my plate" | `memory tasks` |
| Tasks for a tag/person, or done items, or all workspaces | `memory tasks --status all\|done --tag <t> --owner <name> --all` |
| Overall memory state (counts, bank sizes, pack status) | `memory status` |
| Finding a specific fact / person / decision / convention | `memory recall "<keywords>"` |
| The same, in every project ("which project…", "anywhere") | `memory recall "<keywords>" --all` |
| Raw contents of one layer | `memory show <pack\|observations\|staging\|bank\|events>` |
| Which projects have memory | `memory workspaces` |
| Where memory markdown files live (absolute paths) | `memory paths --scope all` |

## Commands

```bash
MEMORY_CLI="${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory"

# Action items as a task list (DEFAULT: open items in this workspace)
python3 "$MEMORY_CLI" tasks
python3 "$MEMORY_CLI" tasks --status all          # include done
python3 "$MEMORY_CLI" tasks --tag release         # filter by tag
python3 "$MEMORY_CLI" tasks --owner "Alex"        # filter by owner
python3 "$MEMORY_CLI" tasks --all                 # every workspace
python3 "$MEMORY_CLI" tasks --json                # structured JSON (parse this when you need fields)

# Dashboard / search / inspect
python3 "$MEMORY_CLI" status
python3 "$MEMORY_CLI" recall "deploy checklist"
python3 "$MEMORY_CLI" recall "deploy checklist" --all   # every project, hits labeled by project
python3 "$MEMORY_CLI" show bank --limit 40

# Discovery
python3 "$MEMORY_CLI" workspaces
python3 "$MEMORY_CLI" paths --scope all
```

## Instructions

1. Choose the single command matching the user's intent (table above). Prefer
   `tasks` for anything about action items / to-dos; prefer `recall` for "where
   did we say…" / "who is…" / "what did we decide about…".
2. Run it with the Shell tool from anywhere in the project. Pass `--workspace <path>`
   only when the user targets a different project (find it via `memory workspaces`).
3. Base the answer strictly on the command output — do not invent items. If the
   output is empty, say so plainly.
4. Summarize for the user; surface owner, due date, and Jira ref for tasks when
   present. Offer the relevant filter flags if the list is long.

## Notes

- This skill is READ-ONLY. To *add* a durable fact use `/add-memory`; to remove
  use `/remove-memory`. Do not hand-edit bank files here.
- If the CLI is missing (`~/.silly-memory/bin/memory` not found), tell the user
  the memory system isn't installed in this environment and stop.
