---
name: add-memory
description: Add a new durable fact to silly-memory at full confidence via its CLI (the same as the MCP tool memory_add). Use when the user asks to remember, save, note, capture, or stash project context (stakeholder, decision, convention, preference).
---

# Add memory

Stores one fact the way the `memory_add` MCP tool does, without needing MCP. If
the `memory_add` tool is connected, you may call it instead; the result is the
same.

## When to use
- User says "remember", "save this", "note that", "for next time", "always", "from now on".
- A new durable fact emerges that should outlive the current chat.

## Instructions
1. Extract one concrete fact from the user's message, as a short self-contained
   sentence. Ask if it is ambiguous; never invent facts. Several facts → one
   command per fact.
2. Leave out anything inside `<private>…</private>`; it is never stored anyway.
2a. **Name Normalization** (canonical guidance — other skills cross-reference
   here): the command applies the plain `variant -> canonical` entries of
   `~/.silly-memory/name-normalization.md` itself. If that file exists, apply
   the entries that need judgment before you run it:
   - **`[skill-only]` entries:** the automatic normalizer skips them because the
     variant is a common word. Normalize only when context clearly identifies
     the entity.
   - **Common-word judgment:** if a word could be the entity or just ordinary
     language, leave it unnormalized rather than over-correcting.
   - **Conditional rules (e.g. `ACME`):** some entries carry conditions (e.g.
     "only normalize ACME when it appears near project context"). Respect them.
   - **Absent file:** skip this step.
3. Pick the scope:
   - `auto` (default): the engine files preferences, people, conventions, and
     hard rules in global memory and everything else in this project.
   - `--scope global`: true for every project ("I always…", "from now on…").
   - `--scope workspace`: only about this project.
4. Run it with the Shell tool from anywhere in the project. Pass the fact on
   stdin through a quoted heredoc, so quotes, `$`, and backticks stay literal:

   ```bash
   python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory" add - <<'FACT'
   We deploy only from main.
   FACT
   ```

   Add `--scope global` or `--scope workspace` before the `-` to force the store,
   and `--workspace <path>` only when the fact belongs to a different project.
5. Read the output and confirm to the user which file it was saved to and
   whether it went to global or project memory.

The command redacts secrets, applies the automatic name-normalization entries,
files the fact in the right bank file, skips a fact already stored, gives it
score 1.0 with the `explicit` tag, and refreshes the search index and project
rules, so it is recallable at once.

## Results
- Exit 0, `saved to <file> (…)`: done.
- Exit 1, `nothing stored`: the text was empty or entirely private.
- Exit 75, `the memory store is busy`: nothing was stored; run it again.
- `~/.silly-memory/bin/memory` missing: tell the user silly-memory is not
  installed in this environment and stop.
