---
name: prune-memory
description: Bulk-remove stale or tag-matched entries from the project memory bank. Use when the user says "prune memory", "clean up old memories", "remove all #cadence", "drop entries older than 90 days", or when the bank has grown large.
---

# Prune memory

## When to use
- Bank has grown large enough that read-every-turn cost matters.
- User says "prune", "clean up old memories", or names a tag/age threshold.

## Instructions
1. Resolve stores under `~/.silly-memory/`. If missing, nothing to prune.
2. Determine the prune criterion from the user's request:
   - Age threshold (e.g., "older than 90 days") → match bullets whose `[date]` is older than today minus the threshold.
   - Tag filter (e.g., "all #cadence") → match bullets whose tag list contains the named tag.
   - Combined (e.g., "all #milestone older than 6 months").
   - If unclear, ask once for the criterion.
3. Search all canonical-format bullets across `memory-bank/` (skip `README.md`).
4. List matches numbered, grouped by file, showing date + tags + fact for each.
5. Ask the user: "Remove all N? Or pick (e.g., 1,3,5)?"
6. Delete only confirmed entries (full bullet line plus any indented detail). Leave empty headings.
7. Summarize: bullets removed, files touched.
8. Run `python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory" render --workspace .` after pruning.
