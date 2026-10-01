# Private Info Setup

This doc covers the four private surfaces of silly-memory and how to populate
them on a fresh or new machine.

**These files never enter the repo. They live only under `~/.silly-memory/` (or
your `SILLY_MEMORY_HOME`) on your local machine and are never committed, never included in a `git archive`, and
never transmitted anywhere.** The public repo ships the engine; your data stays
with you.

---

## The four private surfaces

### 1. `_global/memory-bank/profile.md` — identity, role, and hard preferences

`~/.silly-memory/_global/memory-bank/profile.md` is your personal profile: who
you are, your role and scope, your company or team, key stakeholders you work
with regularly, and hard preferences the agent must always follow.

`install.sh` seeds this file from a template on the first install and **preserves
it on every reinstall** — you can safely upgrade without losing your edits.

**Important:** `profile.md` is a source file, not a surfaced file. The context
pack injected into every session (Cursor, Claude Code, or OpenCode) reads from a
fixed set of recognized global bank files (`audienceContext.md`,
`learned-memories.md`, `conventions.md`). A raw `profile.md` never appears in the
injected context on its own.

After filling in the sections, run:

```bash
memprofile-sync
```

`profile-sync` parses your profile sections and writes managed blocks into the
recognized global files:

| Profile section | Routes to |
|---|---|
| Identity / Role / Company | `audienceContext.md` |
| Key stakeholders | `audienceContext.md` |
| Hard preferences | `learned-memories.md` |

The managed blocks are delimited by `<!-- profile:start -->` and
`<!-- profile:end -->`. Re-running `profile-sync` replaces only those blocks;
anything you added outside them (or via `/add-memory`) is left untouched. The
command is idempotent: running it twice produces byte-identical output.

### 2. `name-normalization.md` — canonical name mappings

`~/.silly-memory/name-normalization.md` maps raw name variants to a single
canonical form. `memory add` (which the `/add-memory` skill runs) applies it
before storing any fact, so names stay consistent across all memory entries
regardless of how you phrase them in chat.

This file is local-only and excluded from every shareable bundle. It is not a
`_global/memory-bank/` file and is not injected into the context pack directly;
its effect applies at write time, silently normalizing facts as they are stored.

To create or edit it, write entries in `variant -> canonical` form:

```
J -> Alex Kim
AK -> Alex Kim
```

`[skill-only]` entries in the file are applied with judgment by the `/add-memory`
skill for common words where automatic normalization would over-correct. If the
file is absent, the skill skips normalization entirely.

### 3. Routed facts via `/add-memory` and `config.json` routes

Facts you ask the agent to remember during a chat are stored through the
`/add-memory` skill. Global facts (preferences, stakeholders, conventions that
apply across all projects) go into `~/.silly-memory/_global/memory-bank/`.

The routing is driven by `config.json`'s `global_bank_routes` block:

```json
"global_bank_routes": {
  "preference":  "learned-memories.md",
  "hard-rule":   "learned-memories.md",
  "stakeholder": "audienceContext.md",
  "convention":  "conventions.md"
}
```

Say things like "remember that X is my manager" or "always use strict TypeScript"
and the skill picks the right destination automatically. These facts land in
`~/.silly-memory/_global/memory-bank/` and surface in every chat under
**## Global memory**.

### 4. Importing accumulated memory from another machine

The `memexport` helper (`memory export`) creates a portable `.tar.gz` bundle of your entire
`~/.silly-memory/` store, including `_global/`, all workspace stores,
`config.json`, `name-normalization.md`, and a `manifest.json` that records which
workspace root maps to which store ID. Use it as a backup or to move to a new
machine.

**Export (on the source machine):**

```bash
memexport ~/my-memory-backup.tgz
```

**Import via the installer (recommended on a fresh machine):**

```bash
./install.sh --import ~/my-memory-backup.tgz
```

The installer runs the engine install first, then restores your bundle with
`--confirm-import YES-IMPORT --relink` applied internally. The existing store is
snapshotted before extraction; nothing is silently overwritten.

**Import manually (if the engine is already installed):**

```bash
memimport ~/my-memory-backup.tgz --confirm-import YES-IMPORT --relink
```

The `--confirm-import YES-IMPORT` token is required. Without it the command
refuses and writes nothing. The snapshot-first guarantee applies in both cases.

**If a project moved to a different path on the new machine**, add
`--map-workspace` for each moved project:

```bash
memimport ~/my-memory-backup.tgz \
  --confirm-import YES-IMPORT \
  --relink \
  --map-workspace /old/machine/projects/myapp=/new/machine/projects/myapp
```

`--map-workspace OLD_ROOT=NEW_ROOT` is repeatable, one flag per moved project.
For each mapping the command writes the correct `.silly-memory/memory-id` into
the new project directory and updates that store's `.meta.json` so `memws` shows
the current path.
Without a mapping for a moved project the command prints the exact
`mkdir -p <dir>/.silly-memory && echo <hash> > <dir>/.silly-memory/memory-id`
remedy plus a `--map-workspace` hint so you can relink it in a follow-up run.

---

## Fresh-machine checklist

Run these steps in order after setting up a new machine:

```bash
# Step 1 — install the engine
#   Without a bundle (start fresh):
./install.sh

#   With an existing bundle (restore your memory):
./install.sh --import ~/my-memory-backup.tgz

# Step 2 — relink any project whose path changed on this machine
memimport ~/my-memory-backup.tgz \
  --confirm-import YES-IMPORT \
  --relink \
  --map-workspace /old/path/projects/myapp=/new/path/projects/myapp

# Step 3 — fill in your profile
#   Open and edit: ~/.silly-memory/_global/memory-bank/profile.md
#   Add real values under Identity, Role, Company, Key stakeholders,
#   Hard preferences. Delete the example guidance lines.

# Step 4 — sync the profile into the recognized global bank files
memprofile-sync

# Step 5 — verify
memstatus   # dashboard for the current workspace
memws       # list all tracked workspaces
```

After step 5, `memstatus` shows the workspace memory dashboard and `memws`
lists every tracked workspace. A workspace that still shows its old path needs a
`--map-workspace` relink (step 2). A workspace that is absent entirely needs
either a relink or a fresh `memrender` run from inside the
project directory.

---

## What stays out of the repo

To be explicit: the following are local-only and must never be committed to any
repo or shared package:

- `~/.silly-memory/_global/memory-bank/profile.md`
- `~/.silly-memory/name-normalization.md`
- `~/.silly-memory/_global/memory-bank/audienceContext.md` (receives synced
  identity and stakeholder facts from `profile-sync`)
- `~/.silly-memory/_global/memory-bank/learned-memories.md` (receives synced
  hard preferences from `profile-sync`)
- `~/.silly-memory/` in its entirety — all workspace stores, `config.json`,
  `name-normalization.md`, and any imported bundles

The public repo ships only the engine. Running `./install.sh` on any machine
creates a fresh private store. Running `./install.sh --import <bundle>` restores
your accumulated memory. Your data never travels through the repo.
