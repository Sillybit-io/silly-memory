---
name: remove-memory
description: Delete an entry from the project memory bank. Use when the user asks to forget, remove, delete, retract, or correct a previously stored memory.
---

# Remove memory

## When to use
- User says "forget", "remove that note", "delete the memory about X", "I was wrong about Y".
- User references a tag ("delete all #cadence entries") or a date ("remove memories from 2026-05-26").

## Instructions
1. Resolve stores under `~/.silly-memory/<workspace-id>/memory-bank/` and `~/.silly-memory/_global/memory-bank/`. If missing, nothing to remove.
2. Determine the match criterion:
   - Free-text → substring / semantic match against the fact portion of bullets.
   - Tag filter → match bullets whose tag list contains the named tag.
   - Date filter → match bullets whose `[date]` matches.
   - Combine criteria when the user gives both (e.g., "all #pm entries from 2026-05").
3. Search all canonical-format bullets across `memory-bank/` (skip `README.md`).
4. Zero matches → tell the user, stop.
5. One match → show it (including its date/tags) and ask "delete this?" before removing.
6. Many matches → list numbered with date + tags + fact, ask which (or all). Delete only confirmed entries.
7. Remove the bullet line entirely (including any indented detail lines under it). If the section becomes empty, leave the heading.
8. Confirm to the user: file, section, exact line(s) removed.
9. Run `python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory" render --workspace .` after removals.
