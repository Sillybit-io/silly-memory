---
name: capture-notes-to-memory
description: Analyze a notes/meeting markdown file and distill its durable facts into silly-memory (stakeholders, decisions, domain facts, action items, milestones). Use when the user creates or points at a meeting note, 1:1 note, or any notes .md file and asks to capture, ingest, add, save, distill, or "put it into memory". Always previews the extracted facts for approval before writing.
---

# Capture notes → memory

Turn a raw notes/meeting markdown file into durable memory. The automatic hook
pipeline only records that a file changed (path), never its content — so this
content extraction is done here, on request.

**Always preview, then write only what the user approves.**

## Workflow

```
- [ ] 1. Read the target note file
- [ ] 2. Extract candidate facts, each routed to a bank file
- [ ] 3. Present the preview and get approval/edits
- [ ] 4. Write approved bullets into the bank files
- [ ] 5. Refresh the injected pack (memory render)
- [ ] 6. Report exactly what was added
```

### 1. Read the file
Use the file the user referenced (e.g. `@meetingNotes/.../26-06-09.md`). If none
is given, ask which file. Read the whole file.

### 2. Extract candidate facts
Pull only **durable** facts (things worth remembering beyond this chat). Skip
chit-chat, transient status, and anything already in memory.

**Normalize names first:** see `add-memory` skill for the canonical name-normalization guidance (`~/.silly-memory/name-normalization.md` map, `[skill-only]` entries, conditional rules). If that file is absent, skip.
For each fact, pick ONE category → bank file → scope:

| Fact type | Category tag | Bank file | Scope |
|---|---|---|---|
| Person / role / org / stakeholder | `#stakeholder` | `audienceContext.md` | global |
| Naming / folder / process convention | `#convention` | `conventions.md` | global |
| Preference / hard rule ("always/never") | `#preference` / `#hard-rule` | `learned-memories.md` | global |
| Decision / product / architecture / integration fact | `#decision` / `#domain` | `domainContext.md` | workspace |
| Action item (owner / task / deadline) | `#action` | `actionItems.md` | workspace |
| Milestone / delivered work | `#milestone` | `progress.md` | workspace |
| Current focus / active work | `#active-focus` | `activeContext.md` | workspace |
| Workspace purpose / scope | `#scope` | `projectbrief.md` | workspace |

Store locations:
- Workspace bank: `~/.silly-memory/<workspace-id>/memory-bank/` (resolve id from `.silly-memory/memory-id`; if missing run `python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory" render --workspace .` to create it).
- Global bank: `~/.silly-memory/_global/memory-bank/`.
- If the target bank file doesn't exist, fall back to `learned-memories.md` in the same scope.

### 3. Preview (REQUIRED — do not skip)
Show the user every candidate bullet, grouped by destination file, in final form.
Use today's date `YYYY-MM-DD` and cite the note path as the source. Then ask them
to approve / edit / drop before writing.

```
Proposed memory additions from `<note path>`:

[global] audienceContext.md
  - [TODAY] #stakeholder #<team>: <who> is <role/fact>. Source: `<note path>`.

[workspace] domainContext.md
  - [TODAY] #decision #<area>: <decision/fact>. Source: `<note path>`.

[workspace] actionItems.md (## Open)
  - [ ] [TODAY] #action #<tag>: <task> — owner: <name>; due: <date|TBD>; source: `<note path>`; jira: none; confluence: none.

Approve all / edit / drop any?
```

### 4. Write approved bullets
Insert each approved bullet under the most relevant `##` heading, **sorted by
date descending (newest first)**. Action items go under `## Open` (or `## Done`
if already completed). Deduplicate — skip facts already present. Match the exact
bullet style already used in each file.

### 5. Refresh
Run `python3 "${SILLY_MEMORY_HOME:-$HOME/.silly-memory}/bin/memory" render --workspace .` so the new facts
reach the injected `_memory-context.mdc`.

### 6. Report
List each file, section, and the exact line(s) added. Note anything skipped as a
duplicate or dropped by the user.

## Rules
- Never write before the user approves the preview.
- Never invent facts — only what's in the note. Ask if a date/owner is ambiguous.
- One fact per bullet; route by the FIRST/primary category.
- This skill writes to the memory bank only. External systems (Jira, Confluence,
  GitLab) stay read-only unless the user explicitly approves a write that turn.
- To add a single ad-hoc fact instead, use `/add-memory`; to query, use `/query-memory`.
