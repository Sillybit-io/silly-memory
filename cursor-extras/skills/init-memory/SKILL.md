---
name: init-memory
description: Initialize a memory bank in the current workspace. Use when there is no memory store yet, or when the user says "set up memory", "init memory", "start tracking memories here", "bootstrap memory".
---

# Init memory

## When to use
- No silly-memory store exists for this workspace.
- User says "init memory", "set up memory", "start a memory bank", "bootstrap memory".

## Instructions
1. Ensure `~/.silly-memory/` is installed (hooks + CLI). If missing, stop and report.
2. Run `python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory" render --workspace .` to create:
   - `.silly-memory/memory-id` in the project
   - `~/.silly-memory/<workspace-id>/memory-bank/` (workspace store)
   - `.cursor/rules/_memory-context.mdc` (auto-generated injected pack)
3. Bootstrap workspace bank files under `~/.silly-memory/<workspace-id>/memory-bank/` if empty:
   - `README.md`, `projectbrief.md`, `conventions.md`, `progress.md`, `activeContext.md`
4. Seed global hard rules in `~/.silly-memory/_global/memory-bank/learned-memories.md` if empty (see below).
5. Add project `.gitignore` entries if missing: `.cursor/rules/_memory-context.mdc`, `.silly-memory/memory-id`
6. Ask: "Should I scan the workspace to seed initial memories?" If no → stop after bootstrap + render.
7. If yes, walk top-level dirs/files and propose canonical bullets via `/add-memory` routing.
8. Run `python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory" reindex --workspace .` after seeding.

Do **not** recreate legacy in-repo `memory-bank/` content files or `memory-bank.mdc` with `alwaysApply: true`.

## Seeded hard rules (global `learned-memories.md`)

```markdown
## Agent hard rules

- [YYYY-MM-DD] #preference #confluence #hard-rule: AI must **NEVER** create, update, edit, comment on, or post to Confluence directly. **ALWAYS ASK FIRST**.
- [YYYY-MM-DD] #preference #gitlab #hard-rule: AI must **NEVER** create, update, or post to GitLab directly. **ALWAYS ASK FIRST**.
```

Use today's date. Full routing spec: copy structure from any existing `memory-bank/README.md` in git history or `~/.silly-memory/README.md`.
